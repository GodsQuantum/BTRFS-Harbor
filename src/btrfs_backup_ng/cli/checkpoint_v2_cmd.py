"""Explicit, opt-in checkpointed raw v2 commands.

No system-wide installation or automatic remapping of legacy raw targets.
Without --experimental the new send and resume code cannot write backups;
without --allow-local, start refuses non-network target filesystems.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import cast

from .. import __version__
from ..core.checkpoint_control_v2 import ControlJournal
from ..core.checkpoint_v2 import (
    ResumeManifest,
    commit_manifest,
    read_manifest,
    serialize_manifest,
)
from ..core.verify_v2 import verify_checkpoint_index
from ..core.checkpoint_v2_runner import SendProcess, execute_checkpoint_job
from ..core.native_send_v2 import (
    destination_fingerprint,
    inspect_readonly_btrfs_source,
    spawn_btrfs_send,
)
from ..core.performance_v2 import apply_resource_policy, resolve_performance_profile
from ..core.replay_v2 import DestinationFingerprint, SourceFingerprint
from ..endpoint.mount_guard_v2 import (
    MountGuard,
    REMOTE_TYPES,
    capture_mount_identity,
)
from ..endpoint.resumable_raw import load_existing
from ..snapper.pin import LiveSnapshot, PinManager
from ..snapper.scanner import SnapperScanner
from ..snapper.source_policy import (
    resolve_source_identity,
    select_source,
)
from ..endpoint.raw_metadata import discover_raw_snapshots

STATES_RESUMABLE = frozenset(
    {
        "preparing",
        "uploading",
        "pause_requested",
        "paused",
        "replaying",
        "failed_resumable",
        "finalizing",
    }
)


def _safe_id(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("invalid canonical transfer UUID")
    return value


def _state_root(specified: str | None = None) -> Path:
    if specified is not None:
        state = Path(specified)
    else:
        xdg = os.getenv("XDG_STATE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "state"
        state = base / "btrfs-harbor" / "checkpoint-v2"
    if not state.is_absolute():
        raise ValueError("control state directory must be absolute")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or not state.is_dir():
        raise ValueError("unsafe checkpoint control state directory")
    return state


def _target_root(target: str) -> Path:
    path = Path(target)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(
            "checkpoint destination must already exist as an absolute directory"
        )
    return path


def _open_guard(
    root: Path, *, allow_local: bool, expected: str | None = None
) -> tuple[MountGuard, str]:
    current = capture_mount_identity(root)
    if current.fstype not in REMOTE_TYPES and not allow_local:
        raise ValueError(
            "checkpoint destination is not NFS/SMB; --allow-local is required "
            "to avoid writing to an underlying path after a lost mount"
        )
    stable = destination_fingerprint(current, root)
    if expected is not None and stable != expected:
        raise ValueError(
            "destination mount/export fingerprint differs from original job"
        )
    return MountGuard(root, current, required_remote=not allow_local), stable


def _manifest_path(root: Path, transfer_id: str) -> Path:
    return root / f".harbor-resume-{_safe_id(transfer_id)}.json"


def _source_check(path: str):
    return inspect_readonly_btrfs_source(Path(path))


def _snapper_query(config: str, number: int) -> LiveSnapshot | None:
    scanner = SnapperScanner()
    candidate = next(
        (s for s in scanner.get_snapshots(config) if s.number == number), None
    )
    if candidate is None:
        return None
    resolved = resolve_source_identity(candidate)
    if not resolved.exists or not resolved.uuid:
        return None
    return LiveSnapshot(resolved.uuid, candidate.cleanup)


def _pin_manager(state_dir: Path) -> PinManager:
    return PinManager(state_dir / "snapper-pins.json", query=_snapper_query)


def _validate_requested_snapper(
    *,
    snapshot_path: Path,
    config: str | None,
    number: int | None,
    source_uuid: str,
) -> None:
    if config is None and number is None:
        return
    if not config or type(number) is not int or number <= 0:
        raise ValueError("both --snapper-config and --snapper-number must be set")
    scanner = SnapperScanner()
    snap = next((s for s in scanner.get_snapshots(config) if s.number == number), None)
    if snap is None or snap.subvolume_path.resolve() != snapshot_path.resolve():
        raise ValueError("selected Snapper config/number does not match source path")
    resolved = resolve_source_identity(snap)
    if not resolved.exists or not resolved.readonly or resolved.uuid != source_uuid:
        raise ValueError("selected Snapper source changed or is not read-only")


def _resolve_source_choice(args: argparse.Namespace, target_root: Path) -> None:
    """Resolve the source once, by Snapper UUID and explicit policy.

    Only reads Snapper state for latest/selected. Create is a native Snapper
    command, executed solely after --experimental and destination preflight.
    """
    mode = args.source_mode
    if mode == "path":
        if not args.source:
            raise ValueError("source --source is required for source-mode path")
        if args.snapper_config is None and args.snapper_number is not None:
            raise ValueError("Snapper number requires a config")
        return
    config = args.snapper_config
    if not isinstance(config, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", config
    ):
        raise ValueError("Snapper mode requires a valid --snapper-config")
    if args.source is not None:
        raise ValueError("Snapper source mode must not also specify --source")
    if mode == "selected-snapper" and (
        type(args.snapper_number) is not int or args.snapper_number <= 0
    ):
        raise ValueError("selected Snapper mode requires --snapper-number")
    if mode == "create-snapper":
        if args.snapper_number is not None:
            raise ValueError("create Snapper does not accept an existing number")
        result = subprocess.run(
            [
                "snapper",
                "-c",
                config,
                "create",
                "--type",
                "single",
                "--print-number",
                "--cleanup-algorithm",
                "number",
                "--description",
                "Btrfs Harbor checkpointed backup",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Snapper snapshot creation failed: {result.stderr.strip()[:240]}"
            )
        try:
            args.snapper_number = int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(
                "Snapper did not report the new snapshot number"
            ) from exc
    scanner = SnapperScanner()
    all_snapshots = scanner.get_snapshots(config)
    remote = {
        snapshot.source_uuid
        for snapshot in discover_raw_snapshots(target_root)
        if snapshot.source_uuid
    }
    selected = select_source(
        all_snapshots,
        mode="selected" if mode != "latest-snapper" else "latest",
        resolve=resolve_source_identity,
        remote_uuids=set() if mode == "latest-snapper" else remote,
        selected_number=args.snapper_number,
        selected_config=config,
    )
    if selected is None or selected.snapshot is None:
        raise ValueError(
            "no eligible new Snapper snapshot: latest may already be backed up"
        )
    if mode == "latest-snapper" and selected.source_uuid in remote:
        raise ValueError(
            "latest stable Snapper snapshot already backed up; "
            "do not send older snapshots for a daily-latest policy"
        )
    source_path = getattr(selected.snapshot, "subvolume_path", None)
    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise ValueError("Snapper snapshot has no absolute subvolume path")
    args.source = str(source_path)
    args.snapper_number = selected.snapshot.number


def _saved_parent_name(root: Path, uuid_string: str) -> str:
    """Require a completed authoritative incremental base on this same target."""
    for snapshot in discover_raw_snapshots(root):
        if (
            snapshot.source_uuid == uuid_string
            and snapshot.provenance_origin != "filename-inferred"
            and snapshot.stream_completeness == "complete"
            and isinstance(snapshot.checksum_value, str)
            and len(snapshot.checksum_value) == 64
            and all(ch in "0123456789abcdef" for ch in snapshot.checksum_value)
            and snapshot.stream_path.is_file()
            and snapshot.metadata_path.is_file()
        ):
            return snapshot.name
    raise ValueError(
        "incremental parent is not an authoritative complete backup at this destination; "
        "save the parent or send a full backup"
    )


def _new_manifest(
    args: argparse.Namespace, stable: str
) -> tuple[ResumeManifest, SourceFingerprint, Path | None]:
    source = _source_check(args.source)
    if not source.readonly:
        raise ValueError("Btrfs source is not read-only")
    parent_path = Path(args.parent) if args.parent else None
    if parent_path is not None:
        parent = _source_check(str(parent_path))
        if not parent.readonly:
            raise ValueError("incremental parent is not read-only")
        source = replace(source, parent_uuid=parent.uuid)
        if args.source == args.parent or parent.uuid == source.uuid:
            raise ValueError("Btrfs incremental parent cannot equal source")
        saved_parent_name = _saved_parent_name(Path(args.target), parent.uuid)
    else:
        saved_parent_name = None
    size = args.checkpoint_size_mib
    if type(size) is not int or not 1 <= size <= 1024:
        raise ValueError("checkpoint size must be 1..1024 MiB")
    profile = resolve_performance_profile(args.performance)
    identity: dict[str, str | int | None] = {
        "profile_id": args.profile_id,
        "source_volume": args.source,
        "source_uuid": source.uuid,
        "source_path": source.path,
        "parent_uuid": source.parent_uuid,
        "send_fingerprint": source.send_fingerprint,
        "compression": "zstd",
        "compression_level": profile.level,
        "encryption": "none",
        "destination_type": "raw",
        "destination_fingerprint": stable,
        "kernel_release": os.uname().release,
        "btrfs_progs_version": _btrfs_version(),
        "harbor_version": "0.2.6",
        "engine_version": __version__,
    }
    if parent_path is not None:
        identity["parent_path"] = str(parent_path)
        identity["parent_backup_name"] = saved_parent_name
    if args.snapper_config is not None:
        identity["snapper_config"] = args.snapper_config
        identity["snapper_number"] = args.snapper_number
    m = ResumeManifest(
        schema_version=2,
        transfer_id=str(uuid.uuid4()),
        identity=identity,
        checkpoint_size=size * 1024 * 1024,
        state="preparing",
        checkpoints=(),
    )
    serialize_manifest(m)
    return m, source, parent_path


def _btrfs_version() -> str:
    result = subprocess.run(
        ["btrfs", "--version"], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("btrfs-progs version not available")
    return result.stdout.strip()[:255]


def _send_worker(
    *,
    root: Path,
    name: str,
    manifest: ResumeManifest,
    source: SourceFingerprint,
    parent: Path | None,
    state_dir: Path,
    allow_local: bool,
    resume: bool,
) -> dict:
    guard, fingerprint = _open_guard(
        root,
        allow_local=allow_local,
        expected=str(manifest.identity["destination_fingerprint"]),
    )
    with guard:
        control = ControlJournal(state_dir, manifest.transfer_id)
        control.request("run")
        dest = DestinationFingerprint(type="raw", fingerprint=fingerprint)
        recorded_level = manifest.identity["compression_level"]
        if type(recorded_level) is not int:
            raise ValueError("manifest compression level is not an integer")
        settings = resolve_performance_profile(
            "custom", overrides={"threads": 2, "level": recorded_level}
        )

        def spawn() -> SendProcess:
            process = spawn_btrfs_send(
                Path(str(manifest.identity["source_path"])), parent
            )
            if process.stdout is None:
                process.kill()
                process.wait()
                raise RuntimeError("btrfs send did not expose a stdout stream")
            pid = getattr(process, "pid", None)
            if type(pid) is int and pid > 1:
                try:
                    control.register_send(pid)
                except BaseException:
                    process.terminate()
                    process.wait()
                    stderr_log = getattr(process, "_harbor_stderr_log", None)
                    if stderr_log is not None:
                        stderr_log.close()
                    raise
                warnings = apply_resource_policy(pid, settings)
                for warning in warnings:
                    print(
                        f"checkpoint-v2 resource policy warning: {warning}",
                        file=sys.stderr,
                    )
            return cast(SendProcess, process)

        print(
            json.dumps(
                {
                    "event": "started",
                    "transfer_id": manifest.transfer_id,
                    "resume": resume,
                    "snapshot": name,
                }
            ),
            flush=True,
        )
        try:
            result = execute_checkpoint_job(
                target_root=root,
                snapshot_name=name,
                manifest=manifest,
                source=source,
                destination=dest,
                guard=guard,
                spawn=spawn,
                action=control.action,
                resume=resume,
                chunk_size=manifest.checkpoint_size,
                level=settings.level,
                threads=settings.threads,
            )
        finally:
            control.clear_active_send()
        return {
            "transfer_id": result.transfer_id,
            "status": result.status,
            "new_raw_bytes": result.new_raw_bytes,
            "new_checkpoints": result.new_checkpoints,
        }


def execute_checkpoint_v2(args: argparse.Namespace) -> int:
    """Public dispatcher for opt-in commands; error means no fake completion."""
    try:
        return _execute(args)
    except (OSError, RuntimeError, ValueError, PermissionError) as exc:
        print(f"checkpoint-v2: {exc}", file=sys.stderr)
        return 2


def _execute(args: argparse.Namespace) -> int:
    action = args.checkpoint_action
    if action == "status":
        root = _target_root(args.target)
        m = read_manifest(_manifest_path(root, args.transfer_id))
        payload = json.loads(serialize_manifest(m))
        payload["resumable"] = m.state in STATES_RESUMABLE
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if action in ("pause", "stop"):
        control = ControlJournal(
            _state_root(args.state_dir), _safe_id(args.transfer_id)
        )
        control.request(action)
        signal_sent = control.signal_registered_send() if action == "stop" else False
        print(
            json.dumps(
                {
                    "requested": action,
                    "transfer_id": args.transfer_id,
                    "status": "signalled active btrfs send"
                    if signal_sent
                    else "queued for checkpoint boundary",
                    "signal_sent": signal_sent,
                }
            )
        )
        return 0
    if action == "discard":
        if not args.confirm:
            print(
                "checkpoint-v2: --confirm is mandatory to discard a partial",
                file=sys.stderr,
            )
            return 2
        root = _target_root(args.target)
        manifest = read_manifest(_manifest_path(root, args.transfer_id))
        guard, _ = _open_guard(
            root,
            allow_local=args.allow_local,
            expected=str(manifest.identity["destination_fingerprint"]),
        )
        with guard:
            if manifest.state == "completed":
                raise ValueError("completed restore points cannot be discarded")
            sink = load_existing(
                root, args.name, args.transfer_id, guard, expected_manifest=manifest
            )
            with sink:
                sink.discard(confirmed=True)
        config = manifest.identity.get("snapper_config")
        number = manifest.identity.get("snapper_number")
        if isinstance(config, str) and type(number) is int:
            # Never unpin before durable part deletion; otherwise a cleanup
            # timer could delete the source while an unfinished transfer still
            # exists. Failure here leaves a conservative orphan pin that can
            # be reconciled, never an unprotected source with a partial.
            _pin_manager(_state_root(args.state_dir)).release(
                config,
                number,
                str(manifest.identity["source_uuid"]),
                manifest.transfer_id,
            )
        print(json.dumps({"discarded": args.transfer_id}))
        return 0
    if action not in ("start", "resume"):
        raise ValueError("unknown checkpoint action")
    if not args.experimental:
        print(
            "checkpoint-v2: explicit --experimental required; real Btrfs/NFS restore "
            "validation not yet completed",
            file=sys.stderr,
        )
        return 2
    root = _target_root(args.target)
    guard, stable = _open_guard(root, allow_local=args.allow_local)
    guard.close()
    state = _state_root(args.state_dir)
    if action == "start":
        _resolve_source_choice(args, root)
        manifest, source, parent = _new_manifest(args, stable)
        _validate_requested_snapper(
            snapshot_path=Path(str(manifest.identity["source_path"])),
            config=args.snapper_config,
            number=args.snapper_number,
            source_uuid=source.uuid,
        )
        pinned = args.snapper_config is not None
        if pinned:
            _pin_manager(state).acquire(
                args.snapper_config,
                args.snapper_number,
                source.uuid,
                manifest.transfer_id,
            )
        try:
            result = _send_worker(
                root=root,
                name=args.name,
                manifest=manifest,
                source=source,
                parent=parent,
                state_dir=state,
                allow_local=args.allow_local,
                resume=False,
            )
        except BaseException:
            # Pin stays in place if an interruption/crash may have left a partial.
            raise
    else:
        manifest = read_manifest(_manifest_path(root, args.transfer_id))
        # A crash may occur after the final stream and .meta are durable but
        # before the journal could be switched to completed. Never regenerate
        # a huge btrfs send merely to acknowledge a finished archive.
        final = root / f"{args.name}.btrfs.zst"
        metadata = root / f"{args.name}.btrfs.zst.meta"
        if final.exists():
            guard, _ = _open_guard(
                root,
                allow_local=args.allow_local,
                expected=str(manifest.identity["destination_fingerprint"]),
            )
            with guard:
                proof = verify_checkpoint_index(metadata, final, full=True)
                if proof.status != "ok":
                    raise ValueError(
                        "a final stream already exists but did not pass full v2 verification: "
                        + proof.detail
                    )
                completed = replace(manifest, state="completed")
                commit_manifest(
                    _manifest_path(root, manifest.transfer_id),
                    completed,
                    validate_destination=lambda: guard.validate_fd(guard.directory_fd),
                )
                config = manifest.identity.get("snapper_config")
                number = manifest.identity.get("snapper_number")
                if isinstance(config, str) and type(number) is int:
                    _pin_manager(state).release(
                        config,
                        number,
                        str(manifest.identity["source_uuid"]),
                        manifest.transfer_id,
                    )
            print(
                json.dumps(
                    {
                        "transfer_id": manifest.transfer_id,
                        "status": "completed",
                        "recovered_final_publish": True,
                        "new_raw_bytes": 0,
                        "new_checkpoints": 0,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if manifest.state not in STATES_RESUMABLE:
            raise ValueError("this checkpoint transaction is not resumable")
        source = _source_check(str(manifest.identity["source_path"]))
        parent_path = manifest.identity.get("parent_path")
        parent = Path(parent_path) if isinstance(parent_path, str) else None
        if parent is not None:
            parent_probe = _source_check(str(parent))
            source = replace(source, parent_uuid=parent_probe.uuid)
            recorded_name = manifest.identity.get("parent_backup_name")
            if (
                recorded_name is not None
                and _saved_parent_name(root, parent_probe.uuid) != recorded_name
            ):
                raise ValueError("incremental parent archive identity changed")
        result = _send_worker(
            root=root,
            name=args.name,
            manifest=manifest,
            source=source,
            parent=parent,
            state_dir=state,
            allow_local=args.allow_local,
            resume=True,
        )
    if result["status"] == "completed":
        config = manifest.identity.get("snapper_config")
        number = manifest.identity.get("snapper_number")
        if isinstance(config, str) and isinstance(number, int):
            _pin_manager(state).release(
                config,
                number,
                str(manifest.identity["source_uuid"]),
                manifest.transfer_id,
            )
    print(json.dumps(result, sort_keys=True))
    return 0
