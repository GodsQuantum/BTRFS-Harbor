"""Experimental machine-level Btrfs backup set, over the EXISTING v2 engine.

No second stream engine: each member is a durable checkpoint-v2 transaction.
The destination holds a portable set catalog with explicit missing EFI/non-Btrfs
coverage. Set completion means all *included Btrfs* members, never bootability.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import stat
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .. import __util__
from ..core.machine_inventory_v2 import inspect_live_machine
from ..core.native_snapshots import NATIVE_FOLDER, NAME_PATTERN, create_native_snapshot
from ..core.native_send_v2 import inspect_readonly_btrfs_source
from ..core.verify_v2 import verify_checkpoint_index
from .checkpoint_v2_cmd import (
    _list_v2,
    _open_guard,
    _target_root,
    execute_checkpoint_v2,
)

MAX_SET_BYTES = 1024 * 1024


def _canonical_uuid(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("invalid set identity")
    return value


def _filename(root: Path, set_id: str) -> Path:
    return root / f".harbor-machine-set-{_canonical_uuid(set_id)}.json"


def _read(root: Path, set_id: str) -> dict:
    path = _filename(root, set_id)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("machine set must be a regular file")
        if os.fstat(fd).st_size > MAX_SET_BYTES:
            raise ValueError("machine set exceeds size limit")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = json.loads(stream.read(MAX_SET_BYTES + 1))
    finally:
        os.close(fd)
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("set_id") != set_id
        or data.get("coverage") != "btrfs-only"
        or not isinstance(data.get("members"), list)
        or not data["members"]
        or len(data["members"]) > 1024
    ):
        raise ValueError("invalid machine-set catalog")
    _canonical_uuid(data.get("profile_id", ""))
    for index, member in enumerate(data["members"]):
        if not isinstance(member, dict) or not isinstance(member.get("archive"), str):
            raise ValueError("invalid machine-set member")
        archive = member["archive"]
        if (
            not archive.isascii()
            or not archive.replace("-", "").replace("_", "").isalnum()
            or f"-{set_id[:8]}-{index:03d}" not in archive
        ):
            raise ValueError("unsafe machine-set archive name")
        original = member.get("original_mount")
        snapshot = member.get("snapshot_path")
        if not isinstance(original, str) or not isinstance(snapshot, str):
            raise ValueError("invalid recorded Btrfs source")
        source_root = Path(original)
        snap_path = Path(snapshot)
        if (
            not source_root.is_absolute()
            or not snap_path.is_absolute()
            or source_root.is_symlink()
            or snap_path.is_symlink()
            or snap_path.parent.is_symlink()
            or snap_path.parent != source_root / NATIVE_FOLDER
            or not NAME_PATTERN.fullmatch(snap_path.name)
        ):
            raise ValueError(
                "machine set source is not a Harbor-owned readonly snapshot"
            )
        if member.get("status") not in ("pending", "sending", "completed"):
            raise ValueError("invalid machine-set member state")
    return data


@contextmanager
def _exclusive_set_lock(root: Path, set_id: str) -> Iterator[None]:
    path = root / f".harbor-machine-set-{_canonical_uuid(set_id)}.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe set lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _write(root: Path, data: dict, *, allow_local: bool) -> None:
    raw = (
        json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode()
    if len(raw) > MAX_SET_BYTES:
        raise ValueError("machine set exceeds size limit")
    path = _filename(root, data["set_id"])
    guard, _ = _open_guard(
        root, allow_local=allow_local, expected=data["destination_fingerprint"]
    )
    with guard:
        directory_fd = guard.directory_fd
        tmp_name = f".harbor-machine-set-{data['set_id']}.{uuid.uuid4().hex}.tmp"
        guard.validate_fd(directory_fd)
        fd = os.open(
            tmp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory_fd,
        )
        try:
            with os.fdopen(fd, "wb", closefd=False) as output:
                output.write(raw)
                output.flush()
                os.fsync(fd)
            guard.validate_fd(directory_fd)
            os.replace(
                tmp_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd
            )
            os.fsync(directory_fd)
        finally:
            os.close(fd)
            try:
                os.unlink(tmp_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _archive_valid(root: Path, archive: str) -> bool:
    stream = root / f"{archive}.btrfs.zst"
    meta = root / f"{archive}.btrfs.zst.meta"
    if not stream.exists() or not meta.exists():
        return False
    return verify_checkpoint_index(meta, stream, full=True).status == "ok"


def _drive(root: Path, data: dict, *, allow_local: bool, state_dir: str | None) -> dict:
    for member in data["members"]:
        archive = member["archive"]
        snapshot_path = Path(member["snapshot_path"])
        if (
            snapshot_path.is_symlink()
            or snapshot_path.parent.is_symlink()
            or not snapshot_path.is_dir()
        ):
            raise ValueError("machine set source snapshot changed")
        details = inspect_readonly_btrfs_source(snapshot_path)
        if not details.readonly or details.uuid != member["snapshot_uuid"]:
            raise ValueError("machine set readonly source identity changed")
        if member["status"] == "completed":
            if not _archive_valid(root, archive):
                raise ValueError(
                    f"completed member has invalid/missing archive: {archive}"
                )
            continue
        candidates = [entry for entry in _list_v2(root) if entry["name"] == archive]
        if len(candidates) > 1:
            raise ValueError("ambiguous checkpoint transactions for machine member")
        if candidates:
            entry = candidates[0]
            if entry["state"] == "completed":
                if not _archive_valid(root, archive):
                    raise ValueError("completed member failed checksum")
                member["status"] = "completed"
                member["transfer_id"] = entry["transfer_id"]
                _write(root, data, allow_local=allow_local)
                continue
            action = "resume"
            args = argparse.Namespace(
                checkpoint_action=action,
                target=str(root),
                name=archive,
                transfer_id=entry["transfer_id"],
                state_dir=state_dir,
                allow_local=allow_local,
                experimental=True,
            )
        else:
            if (root / f"{archive}.btrfs.zst").exists():
                raise ValueError(
                    "archive name already exists without verified checkpoint state"
                )
            args = argparse.Namespace(
                checkpoint_action="start",
                target=str(root),
                name=archive,
                profile_id=data["profile_id"],
                source=member["snapshot_path"],
                source_mode="path",
                parent=None,
                checkpoint_size_mib=128,
                performance="balanced",
                snapper_config=None,
                snapper_number=None,
                native_name=None,
                allow_local=allow_local,
                experimental=True,
                state_dir=state_dir,
            )
        member["status"] = "sending"
        _write(root, data, allow_local=allow_local)
        if execute_checkpoint_v2(args) != 0:
            raise RuntimeError(f"checkpoint-v2 failed on member: {archive}")
        entries = [entry for entry in _list_v2(root) if entry["name"] == archive]
        if len(entries) != 1 or entries[0]["state"] != "completed":
            raise RuntimeError(f"member incomplete, resumable later: {archive}")
        if not _archive_valid(root, archive):
            raise RuntimeError(f"completed member has invalid archive: {archive}")
        member["status"] = "completed"
        member["transfer_id"] = entries[0]["transfer_id"]
        _write(root, data, allow_local=allow_local)
    data["status"] = "completed_btrfs_only"
    _write(root, data, allow_local=allow_local)
    return data


def _sources_from_inventory() -> tuple[list[str], dict]:
    inventory = inspect_live_machine()
    if not inventory["inventory_complete"]:
        raise ValueError(
            "Btrfs enumeration incomplete; cannot start an automatic machine set"
        )
    detected = inventory["sources"]
    if not isinstance(detected, list):
        raise ValueError("invalid Btrfs machine inventory")
    rows: list[str] = []
    for row in detected:
        if isinstance(row, dict) and row.get("included_by_default"):
            path = row.get("mount_point")
            if not isinstance(path, str) or not path.startswith("/"):
                raise ValueError("invalid included Btrfs mount point")
            rows.append(path)
    if not rows:
        raise ValueError("no auto-included mounted Btrfs source")
    return rows, inventory


def execute_machine_set(args: argparse.Namespace) -> int:
    if (
        not getattr(args, "experimental", False)
        and args.checkpoint_action != "set-status"
    ):
        raise ValueError(
            "--experimental required; full machine boot restore is not supported"
        )
    root = _target_root(args.target)
    action = args.checkpoint_action
    if action == "set-start":
        requested = getattr(args, "source", None) or []
        sources, inventory = (
            (requested, None) if requested else _sources_from_inventory()
        )
        if len(sources) > 1024:
            raise ValueError("too many machine-set source subvolumes")
        if not sources or len(set(sources)) != len(sources):
            raise ValueError("machine set needs distinct nonempty sources")
        guard, stable = _open_guard(root, allow_local=args.allow_local)
        guard.close()
        set_id = str(uuid.uuid4())
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        members = []
        # Do not send a single byte until every explicit source is validated.
        for source in sources:
            live = Path(source)
            if not live.is_absolute() or live.is_symlink():
                raise ValueError("machine source must be a real absolute subvolume")
            result = subprocess.run(
                ["btrfs", "subvolume", "show", str(live)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 0:
                raise ValueError(f"not a Btrfs subvolume root: {source}")
        for i, source in enumerate(sources):
            live = Path(source)
            snapshot = create_native_snapshot(live)
            members.append(
                {
                    "archive": f"machine{timestamp}-{set_id[:8]}-{i:03d}",
                    "original_mount": source,
                    "snapshot_path": str(snapshot.path),
                    "snapshot_uuid": snapshot.uuid,
                    "status": "pending",
                    "transfer_id": None,
                }
            )
        data = {
            "schema_version": 1,
            "set_id": set_id,
            "profile_id": args.profile_id,
            "destination_fingerprint": stable,
            "coverage": "btrfs-only",
            "bootable": False,
            "status": "pending",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "inventory": inventory,
            "members": members,
        }
        with _exclusive_set_lock(root, set_id):
            _write(root, data, allow_local=args.allow_local)
            return _result(
                _drive(
                    root, data, allow_local=args.allow_local, state_dir=args.state_dir
                )
            )
    set_id = _canonical_uuid(args.set_id)
    with _exclusive_set_lock(root, set_id):
        data = _read(root, set_id)
        guard, _ = _open_guard(
            root,
            allow_local=args.allow_local,
            expected=data["destination_fingerprint"],
        )
        guard.close()
        if action == "set-status":
            return _result(data)
        if action == "set-resume":
            return _result(
                _drive(
                    root, data, allow_local=args.allow_local, state_dir=args.state_dir
                )
            )
        if action == "set-restore":
            return _restore(root, data, args)
    raise ValueError("unknown machine-set action")


def _result(data: dict) -> int:
    print(
        json.dumps(
            {
                "set_id": data["set_id"],
                "status": data["status"],
                "coverage": data["coverage"],
                "bootable": False,
                "members": [
                    {
                        "archive": m["archive"],
                        "mount": m["original_mount"],
                        "status": m["status"],
                    }
                    for m in data["members"]
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def _restore(root: Path, data: dict, args: argparse.Namespace) -> int:
    if data["status"] != "completed_btrfs_only":
        raise ValueError("refusing incomplete machine-set recovery")
    staging = Path(args.staging)
    if not staging.is_absolute() or staging.is_symlink() or not staging.is_dir():
        raise ValueError("staging must be an existing real absolute directory")
    if any(staging.iterdir()):
        raise ValueError("staging must be empty, never overwrite existing user data")
    # Btrfs subvolumes may expose synthetic st_dev values distinct from
    # mountinfo's device; ask the mount utility for the filesystem type
    # rather than treating the destination write guard as a source probe.
    probe = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", str(staging)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode or probe.stdout.strip() != "btrfs":
        raise ValueError("staging filesystem must be Btrfs")
    for member in data["members"]:
        if not _archive_valid(root, member["archive"]):
            raise ValueError(f"invalid/missing archive: {member['archive']}")
    plan = [
        {
            "archive": member["archive"],
            "original_mount": member["original_mount"],
            "staging": str(staging / f"volume-{index:03d}"),
        }
        for index, member in enumerate(data["members"])
    ]
    if not args.confirm:
        print(json.dumps({"dry_run": True, "bootable": False, "plan": plan}))
        return 0
    for entry in plan:
        destination = Path(entry["staging"])
        destination = __util__.create_below(
            staging, destination.name, mode=0o700, what="Restore staging"
        )
        outcome = subprocess.run(
            [
                sys.executable,
                "-m",
                "btrfs_backup_ng",
                "restore",
                f"raw://{root}",
                str(destination),
                "--snapshot",
                entry["archive"],
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=1800,
            check=False,
        )
        if outcome.returncode:
            raise RuntimeError(
                f"restore failed for {entry['archive']}: {outcome.stderr[-1000:]}"
            )
    print(json.dumps({"restored": True, "bootable": False, "plan": plan}))
    return 0
