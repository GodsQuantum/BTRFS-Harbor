"""Installed-profile checkpoint coordinator: one transaction, never a new engine.

An interrupted job is resumed by immutable (profile, source, destination)
identity before a new snapshot can be selected. Invoked by Harbor's existing
systemd profile service, not by an extra daemon/timer.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from ..core.checkpoint_v2 import read_manifest
from .machine_set_v2 import _read, execute_machine_set
from .checkpoint_v2_cmd import (
    STATES_RESUMABLE,
    _list_v2,
    _manifest_path,
    _open_guard,
    _target_root,
    _resolve_source_choice,
    execute_checkpoint_v2,
)


def _canonical_profile(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("invalid canonical profile UUID")
    return value


def _valid_source(args: argparse.Namespace) -> None:
    if args.snapper_config:
        if args.source:
            raise ValueError(
                "Snapper scheduled jobs use config identity, not an arbitrary source"
            )
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", args.snapper_config):
            raise ValueError("invalid Snapper config")
    else:
        path = Path(args.source or "")
        if not path.is_absolute() or not path.is_dir() or path.is_symlink():
            raise ValueError(
                "native scheduled source must be a real absolute subvolume"
            )


def execute_scheduled_checkpoint_v2(args: argparse.Namespace) -> int:
    """Fail closed when an ambiguous partial exists; never send a duplicate."""
    if not args.experimental:
        raise ValueError("--experimental required for scheduled v2 migration")
    profile = _canonical_profile(args.profile_id)
    _valid_source(args)
    root = _target_root(args.target)
    guard, _ = _open_guard(root, allow_local=args.allow_local)
    with guard:
        # Native scheduling reuses the machine-set write-ahead snapshot journal.
        # This closes the SIGKILL window between native snapshot ioctl and the
        # initial v2 checkpoint journal, without adding a second engine.
        if not args.snapper_config:
            matched: list[str] = []
            for filename in os.listdir(root):
                if not (
                    filename.startswith(".harbor-machine-set-")
                    and filename.endswith(".json")
                ):
                    continue
                identifier = filename[len(".harbor-machine-set-") : -len(".json")]
                try:
                    record = _read(root, identifier)
                except (OSError, ValueError, TypeError, KeyError):
                    continue
                if (
                    record.get("profile_id") == profile
                    and record.get("status") != "completed_btrfs_only"
                    and any(
                        member.get("original_mount") == args.source
                        for member in record["members"]
                    )
                ):
                    matched.append(identifier)
            if len(matched) > 1:
                raise ValueError("multiple unfinished Btrfs sets for scheduled source")
            guard.validate_fd(guard.directory_fd)
            # Do not retain an NFS directory fd while calling the nested
            # transfer engine, which opens its own mount-pinned transaction.
            upcoming = matched[0] if matched else None
        else:
            upcoming = None
        # The all-in-memory inventory never writes or repairs backup folders.
        # Too many files are an ambiguity, not permission to start another job.
        if len(os.listdir(root)) > 8192:
            raise ValueError("backup target exceeds safe scheduled scan limit")
        inventory = _list_v2(root, max_entries=8192)
        candidates: list[dict[str, str]] = []
        for entry in inventory:
            if entry["state"] not in STATES_RESUMABLE:
                continue
            manifest = read_manifest(_manifest_path(root, str(entry["transfer_id"])))
            if manifest.identity.get("profile_id") != profile:
                continue
            if args.snapper_config:
                matches = manifest.identity.get("snapper_config") == args.snapper_config
            else:
                matches = (
                    manifest.identity.get("source_volume") == args.source
                    and manifest.identity.get("snapper_config") is None
                )
            if not matches:
                continue
            archive = entry.get("name")
            if not isinstance(archive, str) or not archive:
                raise ValueError(
                    "unfinished profile transaction has no recoverable archive name"
                )
            candidates.append({"name": archive, "transfer_id": manifest.transfer_id})
        if len(candidates) > 1:
            raise ValueError(
                "multiple unfinished transactions for this profile/source; refusing duplicate"
            )
        guard.validate_fd(guard.directory_fd)

    # Resume an existing machine-set as a UNIT, not an orphaned member stream.
    if not args.snapper_config and upcoming:
        if candidates and len(candidates) > 1:
            raise ValueError("ambiguous active native scheduled transfer")
        return execute_machine_set(
            argparse.Namespace(
                checkpoint_action="set-resume",
                target=str(root),
                profile_id=profile,
                source=None,
                set_id=upcoming,
                state_dir=args.state_dir,
                allow_local=args.allow_local,
                experimental=True,
            )
        )

    if candidates:
        prior = candidates[0]
        resume = argparse.Namespace(
            checkpoint_action="resume",
            target=str(root),
            name=prior["name"],
            transfer_id=prior["transfer_id"],
            state_dir=args.state_dir,
            allow_local=args.allow_local,
            experimental=True,
        )
        return execute_checkpoint_v2(resume)

    if not args.snapper_config:
        # Native one-member sets use the crash-safe write-ahead reservation,
        # and the same checkpoint writer as interactive multi-volume sends.
        return execute_machine_set(
            argparse.Namespace(
                checkpoint_action="set-start",
                target=str(root),
                profile_id=profile,
                source=[args.source],
                set_id=None,
                state_dir=args.state_dir,
                allow_local=args.allow_local,
                experimental=True,
            )
        )

    # No resumable transaction: reserve a unique archive name before sending.
    # Snapper schedules select the newest eligible stable source snapshot.
    prefix = args.name_prefix or re.sub(r"[^A-Za-z0-9_-]+", "-", args.snapper_config)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", prefix):
        raise ValueError("scheduled archive prefix must be safe ASCII")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = f"{prefix}-{stamp}-{uuid.uuid4().hex[:12]}"
    start = argparse.Namespace(
        checkpoint_action="start",
        target=str(root),
        name=archive,
        profile_id=profile,
        source=None,
        source_mode="latest-snapper",
        parent=None,
        checkpoint_size_mib=args.checkpoint_size_mib,
        performance=args.performance,
        snapper_config=args.snapper_config,
        snapper_number=None,
        native_name=None,
        allow_local=args.allow_local,
        experimental=True,
        state_dir=args.state_dir,
    )
    # A scheduled job with no new Snapper snapshot is a successful no-op,
    # never an alarming failed systemd service. This preflight never creates
    # snapshots or writes to the backup target.
    candidate = copy.copy(start)
    try:
        _resolve_source_choice(candidate, root)
    except ValueError as exc:
        if str(exc).startswith("latest stable Snapper snapshot already backed up"):
            print(
                json.dumps(
                    {
                        "status": "no-change",
                        "profile_id": profile,
                        "snapper_config": args.snapper_config,
                    },
                    sort_keys=True,
                )
            )
            return 0
        raise
    return execute_checkpoint_v2(start)
