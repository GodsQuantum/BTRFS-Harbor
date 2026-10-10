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
from ..config import RetentionConfig
from ..core.machine_retention_v2 import SetPoint, calculate_set_retention
from ..core.boot_files_v2 import (
    assert_configured_boot_mounts_present,
    capture_boot_member,
    discover_boot_mounts,
    snapshot_boot_layout,
    stage_boot_members,
    verify_boot_member,
)
from ..core.machine_inventory_v2 import _mount_rows
from ..endpoint.raw_metadata import discover_raw_snapshots
from ..core.machine_inventory_v2 import inspect_live_machine
from ..core.native_snapshots import (
    NATIVE_FOLDER,
    NAME_PATTERN,
    create_native_snapshot,
    find_native_snapshot,
    reserve_native_snapshot_name,
)
from ..core.rear_bridge_v2 import apply_rear_restore, plan_rear_restore
from ..core.rear_iso_v2 import build_rescue_iso, _check_catalog
from ..core.native_send_v2 import inspect_readonly_btrfs_source
from ..core.verify_v2 import verify_checkpoint_index
from ..endpoint.mount_guard_v2 import MountGuard
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
        or data.get("coverage") not in ("btrfs-only", "btrfs-and-boot-files")
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
    boot_members = data.get("boot_members", [])
    if not isinstance(boot_members, list) or len(boot_members) > 8:
        raise ValueError("invalid boot partition inventory")
    if data["coverage"] == "btrfs-and-boot-files" and not boot_members:
        raise ValueError("boot coverage promised with no archived boot partitions")
    for index, member in enumerate(boot_members):
        if not isinstance(member, dict):
            raise ValueError("invalid boot partition member")
        if member.get("archive") != f".harbor-boot-{set_id}-{index:03d}.tar.gz":
            raise ValueError("unreserved boot partition archive name")
        if member.get("mount_point") not in (
            "/boot",
            "/boot/efi",
            "/boot/firmware",
            "/efi",
        ):
            raise ValueError("invalid boot partition mount")
        if not isinstance(member.get("source"), str) or not member["source"].startswith(
            "/dev/"
        ):
            raise ValueError("invalid boot partition source")
        if member.get("filesystem") not in (
            "vfat",
            "ext2",
            "ext3",
            "ext4",
            "xfs",
            "f2fs",
        ):
            raise ValueError("unsupported boot filesystem type")
        if member.get("status") not in ("pending", "completed"):
            raise ValueError("invalid boot archive state")
    return data


@contextmanager
def _exclusive_set_lock(guard: MountGuard, set_id: str) -> Iterator[None]:
    """Never create a lock beneath a disappeared or replaced NFS mount."""
    directory_fd = guard.directory_fd
    guard.validate_fd(directory_fd)
    filename = f".harbor-machine-set-{_canonical_uuid(set_id)}.lock"
    fd = os.open(
        filename,
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        guard.validate_fd(directory_fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("unsafe set lock")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        guard.validate_fd(directory_fd)
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
        if member["status"] == "completed":
            # A completed, verified backup must not depend on the original
            # computer or a Snapper/native snapshot still being present.
            if not _archive_valid(root, archive):
                raise ValueError(
                    f"completed member has invalid/missing archive: {archive}"
                )
            continue
        snapshot_path = Path(member["snapshot_path"])
        if (
            snapshot_path.is_symlink()
            or snapshot_path.parent.is_symlink()
            or (
                (
                    member.get("snapshot_uuid") is not None
                    or member["status"] != "pending"
                )
                and not snapshot_path.is_dir()
            )
        ):
            raise ValueError("machine set source snapshot changed")
        recorded_uuid = member.get("snapshot_uuid")
        if recorded_uuid is None and member["status"] == "pending":
            # A process may have died before, during, or after the snapshot
            # ioctl. The manifest already names its only permitted target.
            # Reuse that exact readonly snapshot or create it under that name.
            if snapshot_path.exists() or snapshot_path.is_symlink():
                found = find_native_snapshot(
                    Path(member["original_mount"]), snapshot_path.name
                )
            else:
                found = create_native_snapshot(
                    Path(member["original_mount"]),
                    reserved_name=snapshot_path.name,
                )
            if found.path != snapshot_path:
                raise ValueError("recovered native snapshot path changed")
            member["snapshot_uuid"] = found.uuid
            _write(root, data, allow_local=allow_local)
            recorded_uuid = found.uuid
        elif not isinstance(recorded_uuid, str) or not recorded_uuid:
            raise ValueError("invalid machine-set snapshot UUID")
        details = inspect_readonly_btrfs_source(snapshot_path)
        if not details.readonly or details.uuid != recorded_uuid:
            raise ValueError("machine set readonly source identity changed")
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
                # Harbor-owned readonly native snapshots can reuse verified
                # remote parent chains, instead of sending fulls forever.
                native_root=member["original_mount"],
                parent=None,
                checkpoint_size_mib=128,
                performance="balanced",
                max_incremental_depth=data.get("max_incremental_depth", 7),
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
    # A second, source-readonly archive class shares the SAME machine-set
    # transaction/catalog and destination mount guard. EFI/boot are not
    # Btrfs subvolumes; they must never be passed to btrfs send.
    boot_members = data.get("boot_members", [])
    if boot_members:
        guard, _ = _open_guard(
            root,
            allow_local=allow_local,
            expected=data["destination_fingerprint"],
        )
        with guard:
            for index, member in enumerate(boot_members):
                if member["status"] == "completed":
                    if not verify_boot_member(root, member):
                        raise ValueError(
                            f"completed boot archive failed checksum: {index}"
                        )
                    continue
                recorded = capture_boot_member(root, guard, member)
                boot_members[index] = recorded
                _write(root, data, allow_local=allow_local)
        data["status"] = "completed_btrfs_and_boot_files"
    else:
        data["status"] = "completed_btrfs_only"
    _write(root, data, allow_local=allow_local)
    return data


def _retention_plan(root: Path, args: argparse.Namespace) -> int:
    """Read-only, fail-closed policy preview; NEVER unlinks a backup."""
    profile = _canonical_uuid(args.profile_id)
    counts = [
        args.hourly,
        args.daily,
        args.weekly,
        args.monthly,
        args.yearly,
        args.keep,
    ]
    if any(type(value) is not int or value < 0 or value > 10000 for value in counts):
        raise ValueError("retention bucket values must be between 0 and 10000")
    retention = RetentionConfig(
        min=args.min,
        hourly=args.hourly,
        daily=args.daily,
        weekly=args.weekly,
        monthly=args.monthly,
        yearly=args.yearly,
        keep=args.keep,
    )
    guard, _ = _open_guard(root, allow_local=args.allow_local)
    with guard:
        guard.validate_fd(guard.directory_fd)
        names = sorted(os.listdir(guard.directory_fd))
        if len(names) > 8192:
            raise ValueError(
                "too many destination entries for safe retention inventory"
            )
        catalog_names = [
            name
            for name in names
            if name.startswith(".harbor-machine-set-") and name.endswith(".json")
        ]
        records: list[dict] = []
        for name in catalog_names:
            identifier = name[len(".harbor-machine-set-") : -len(".json")]
            record = _read(root, _canonical_uuid(identifier))
            if record["profile_id"] != profile:
                raise ValueError(
                    "another backup profile shares this destination; refusing plan"
                )
            records.append(record)
        snapshots = discover_raw_snapshots(root)
        # Strict metadata: never issue a deletion proposal for incomplete
        # or legacy sidecars, unknown parents, symlinks, or missing streams.
        graph: dict[str, str | None] = {}
        for row in snapshots:
            if (
                not row.name
                or not row.stream_path.is_file()
                or row.stream_path.is_symlink()
                or row.metadata_path.is_symlink()
                or not row.metadata_path.is_file()
                or row.stream_completeness != "complete"
                or row.provenance_origin != "native-write"
                or row.checksum_algorithm != "sha256"
                or not isinstance(row.checksum_value, str)
                or len(row.checksum_value) != 64
                or (row.parent_uuid is not None and not row.parent_name)
            ):
                raise ValueError("unverified raw archive in retention inventory")
            graph[row.name] = row.parent_name
        # Boot-file attachments have the SAME lifetime as their Btrfs set.
        # They are not Btrfs streams and must not appear in the incremental
        # graph, but cannot be retained/deleted separately from their owner.
        for record in records:
            boot = record.get("boot_members", [])
            if any(
                member.get("status") == "completed"
                and not verify_boot_member(root, member)
                for member in boot
            ):
                raise ValueError("boot archive is corrupt or missing")
            if record.get("status") == "completed_btrfs_and_boot_files" and (
                not boot or any(member.get("status") != "completed" for member in boot)
            ):
                raise ValueError("boot-bearing machine set has incomplete archives")
        points: list[SetPoint] = []
        for record in records:
            stamp = datetime.fromisoformat(record["created_utc"])
            points.append(
                SetPoint(
                    record["set_id"],
                    stamp,
                    tuple(member["archive"] for member in record["members"]),
                    record["status"]
                    in ("completed_btrfs_only", "completed_btrfs_and_boot_files"),
                )
            )
        plan = calculate_set_retention(
            points, graph, retention, now=datetime.now(timezone.utc)
        )
        guard.validate_fd(guard.directory_fd)
    print(
        json.dumps(
            {
                "dry_run": True,
                "deletion_performed": False,
                "keep_sets": sorted(plan.keep),
                "protected_incremental_sets": sorted(plan.protected_dependencies),
                "retention_eligible_sets": sorted(plan.eligible),
                "keep_archives": sorted(plan.kept_archives),
                "eligible_boot_archives": sorted(
                    member["archive"]
                    for record in records
                    if record["set_id"] in plan.eligible
                    for member in record.get("boot_members", [])
                ),
                "note": "Preview only; automatic pruning is not enabled.",
            },
            sort_keys=True,
        )
    )
    return 0


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
    if not getattr(args, "experimental", False) and args.checkpoint_action not in (
        "set-status",
        "set-list",
    ):
        raise ValueError(
            "--experimental required; full machine boot restore is not supported"
        )
    root = _target_root(args.target)
    action = args.checkpoint_action
    if action == "set-retention-plan":
        return _retention_plan(root, args)
    if action == "set-list":
        rows: list[dict[str, object]] = []
        for name in sorted(os.listdir(root))[:4096]:
            if not (name.startswith(".harbor-machine-set-") and name.endswith(".json")):
                continue
            identifier = name[len(".harbor-machine-set-") : -len(".json")]
            try:
                data = _read(root, _canonical_uuid(identifier))
            except (ValueError, OSError, TypeError, KeyError, json.JSONDecodeError):
                continue
            rows.append(
                {
                    "set_id": data["set_id"],
                    "status": data["status"],
                    "coverage": data["coverage"],
                    "bootable": False,
                    "created_utc": data.get("created_utc"),
                    "members": len(data["members"]),
                    "boot_partitions": len(data.get("boot_members", [])),
                }
            )
            if len(rows) >= 100:
                break
        print(json.dumps(rows, sort_keys=True))
        return 0
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
        # Durable write-ahead reservation BEFORE creating any snapshot.
        # A killed process leaves only a known, recoverable pending member,
        # never an untracked native Btrfs snapshot.
        for i, source in enumerate(sources):
            reserved_name = reserve_native_snapshot_name()
            members.append(
                {
                    "archive": f"machine{timestamp}-{set_id[:8]}-{i:03d}",
                    "original_mount": source,
                    "snapshot_path": str(Path(source) / NATIVE_FOLDER / reserved_name),
                    "snapshot_uuid": None,
                    "status": "pending",
                    "transfer_id": None,
                }
            )
        max_depth = getattr(args, "max_incremental_depth", 7)
        if type(max_depth) is not int or not 1 <= max_depth <= 256:
            raise ValueError("max incremental depth must be 1..256")
        # Machine-wide mode captures separate EFI and /boot partitions.
        # Explicit --source mode (used for selecting data volumes and CI
        # fixtures) deliberately does not quietly archive the host ESP.
        boot_rows = []
        layout = None
        if not requested:
            mount_rows = _mount_rows(Path("/proc/mounts").read_text(encoding="utf-8"))
            configured = Path("/etc/fstab")
            if configured.exists():
                assert_configured_boot_mounts_present(
                    mount_rows, configured.read_text(encoding="utf-8")
                )
            boot_rows = discover_boot_mounts(mount_rows)
            layout = snapshot_boot_layout()
        boot_members = [
            {
                "archive": f".harbor-boot-{set_id}-{index:03d}.tar.gz",
                "mount_point": item["mount_point"],
                "source": item["source"],
                "filesystem": item["filesystem"],
                "status": "pending",
            }
            for index, item in enumerate(boot_rows)
        ]
        data = {
            "schema_version": 1,
            "max_incremental_depth": max_depth,
            "set_id": set_id,
            "profile_id": args.profile_id,
            "destination_fingerprint": stable,
            "coverage": "btrfs-and-boot-files" if boot_members else "btrfs-only",
            "bootable": False,
            "status": "pending",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "inventory": inventory,
            "members": members,
            "boot_members": boot_members,
            "disk_layout": layout,
        }
        with guard, _exclusive_set_lock(guard, set_id):
            _write(root, data, allow_local=args.allow_local)
            return _result(
                _drive(
                    root, data, allow_local=args.allow_local, state_dir=args.state_dir
                )
            )
    set_id = _canonical_uuid(args.set_id)
    guard, stable = _open_guard(root, allow_local=args.allow_local)
    with guard:
        data = _read(root, set_id)
        # Destination-only restore may be run on a DIFFERENT computer with
        # the backup export mounted at a new path. No destination lock file,
        # no write, and no dependence on the original path/fingerprint.
        # The current mount is pinned for the duration of the read.
        if action in (
            "set-status",
            "set-restore",
            "set-rear-recover",
            "set-rear-copy",
        ):
            readonly_result = _read_only_machine_action(root, data, args)
            guard.validate_fd(guard.directory_fd)
            return readonly_result
        # Mutating operations (send, ISO publish) still require the original
        # exact export. A changed/missing mount is never eligible for writes.
        if stable != data.get("destination_fingerprint"):
            raise ValueError("machine-set destination fingerprint changed")
        with _exclusive_set_lock(guard, set_id):
            if action == "set-resume":
                return _result(
                    _drive(
                        root,
                        data,
                        allow_local=args.allow_local,
                        state_dir=args.state_dir,
                    )
                )
            if action == "set-rescue-iso":
                _check_catalog(data)
                if not args.confirm:
                    print(
                        json.dumps(
                            {
                                "dry_run": True,
                                "iso_created": False,
                                "boot_tested": False,
                                "set_id": data["set_id"],
                                "note": "ReaR ISO is built locally and published atomically to this destination.",
                            }
                        )
                    )
                    return 0
                for member in data["members"]:
                    if not _archive_valid(root, member["archive"]):
                        raise ValueError(
                            "Btrfs archive failed verification before rescue ISO"
                        )
                for member in data.get("boot_members", []):
                    if not verify_boot_member(root, member):
                        raise ValueError(
                            "EFI archive failed verification before rescue ISO"
                        )
                pinned, _ = _open_guard(
                    root,
                    allow_local=args.allow_local,
                    expected=data["destination_fingerprint"],
                )
                with pinned:
                    iso = build_rescue_iso(root, data, pinned)
                print(json.dumps(iso, sort_keys=True))
                return 0

    raise ValueError("unknown machine-set action")


def _read_only_machine_action(root: Path, data: dict, args: argparse.Namespace) -> int:
    action = args.checkpoint_action
    if action == "set-status":
        return _result(data)
    if action == "set-restore":
        return _restore(root, data, args)
    if action == "set-rear-recover":
        return _rear_recover(root, data, args)
    if action == "set-rear-copy":
        # Nothing partitions/formats a disk in Harbor. ReaR must
        # already have recreated filesystems and mounted /mnt/local.
        # The rescue marker and strict mount checks are inside the
        # bridge, so a healthy running OS can never trigger writes.
        staging = Path(args.staging)
        if not args.confirm:
            plan = plan_rear_restore(data, staging, Path("/mnt/local"))
            print(
                json.dumps(
                    {
                        "dry_run": True,
                        "bootable": False,
                        "plan": [
                            {
                                "source": str(item.source),
                                "target": str(item.target),
                            }
                            for item in plan
                        ],
                        "note": "ReaR must finalize the initramfs and bootloader afterwards.",
                    }
                )
            )
            return 0
        restored = apply_rear_restore(data, staging)
        print(
            json.dumps(
                {
                    "copied_to_rear": True,
                    "bootable": False,
                    "mounts": [item.original_mount for item in restored],
                    "note": "ReaR bootloader finalization and QEMU boot test still required.",
                }
            )
        )
        return 0
    raise ValueError("unknown read-only machine-set action")


def _rear_recover(root: Path, data: dict, args: argparse.Namespace) -> int:
    """Single ReaR EXTERNAL restore action after ReaR recreated/mounted disks.

    Never formats partitions. The rescue ISO must provide the static wrapper
    and dependencies; this function only runs in the ReaR rescue system.
    """
    rescue_root = Path("/mnt/local")
    if (
        os.geteuid() != 0
        or not Path("/etc/rear/rescue.conf").is_file()
        or not rescue_root.is_mount()
    ):
        raise PermissionError("ReaR rescue environment and mounted root required")
    if data["status"] not in ("completed_btrfs_only", "completed_btrfs_and_boot_files"):
        raise ValueError("cannot restore an incomplete machine set")
    if not args.confirm:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "bootable": False,
                    "machine_set": data["set_id"],
                    "staging": str(rescue_root / f".harbor-rear-{data['set_id'][:8]}"),
                    "warning": "This recovery will copy onto partitions ALREADY recreated by ReaR.",
                }
            )
        )
        return 0
    # Preflight the target before ANY stage creation; no host mount changes.
    probe = subprocess.run(
        [
            "findmnt",
            "--noheadings",
            "--output",
            "FSTYPE",
            "--mountpoint",
            str(rescue_root),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if probe.returncode or probe.stdout.strip() != "btrfs":
        raise ValueError("ReaR system root must be mounted Btrfs")
    staging = __util__.create_below(
        rescue_root,
        f".harbor-rear-{data['set_id'][:8]}",
        mode=0o700,
        what="ReaR recovery staging",
    )
    step = argparse.Namespace(
        confirm=True,
        staging=str(staging),
    )
    _restore(root, data, step)
    restored = apply_rear_restore(data, staging)
    print(
        json.dumps(
            {
                "copied_to_rear": True,
                "bootable": False,
                "staging": str(staging),
                "mounts": [job.original_mount for job in restored],
                "next": "ReaR must finalize bootloader/initramfs; do not reboot until it succeeds.",
            }
        )
    )
    return 0


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
                "boot_partitions": [
                    {
                        "mount": m["mount_point"],
                        "filesystem": m["filesystem"],
                        "status": m["status"],
                    }
                    for m in data.get("boot_members", [])
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def _restore(root: Path, data: dict, args: argparse.Namespace) -> int:
    if data["status"] not in ("completed_btrfs_only", "completed_btrfs_and_boot_files"):
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
    for member in data.get("boot_members", []):
        if not verify_boot_member(root, member):
            raise ValueError(f"invalid/missing boot archive: {member['archive']}")
    plan = [
        {
            "archive": member["archive"],
            "original_mount": member["original_mount"],
            "staging": str(staging / f"volume-{index:03d}"),
        }
        for index, member in enumerate(data["members"])
    ]
    boot_plan = [
        {
            "original_mount": m["mount_point"],
            "archive": m["archive"],
            "staging": str(staging / "boot-files" / f"boot-{i:03d}"),
        }
        for i, m in enumerate(data.get("boot_members", []))
    ]
    if not args.confirm:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "bootable": False,
                    "plan": plan,
                    "boot_plan": boot_plan,
                }
            )
        )
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
    if data.get("boot_members"):
        boot_destination = __util__.create_below(
            staging, "boot-files", mode=0o700, what="Boot-file restore staging"
        )
        stage_boot_members(root, data["boot_members"], boot_destination)
    print(
        json.dumps(
            {"restored": True, "bootable": False, "plan": plan, "boot_plan": boot_plan}
        )
    )
    return 0
