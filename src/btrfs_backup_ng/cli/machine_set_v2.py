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
import re
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .. import __util__
from ..config import RetentionConfig
from ..core.machine_retention_v2 import SetPoint, calculate_set_retention
from ..core.machine_eta_v2 import estimate_source_bytes, forecast_machine
from ..core.machine_retention_gc_v2 import destination_gc_lock
from ..endpoint.mount_guard_v2 import capture_mount_identity
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
from ..core.machine_inventory_v2 import inspect_live_machine, _is_real_subvolume_root
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
from ..snapper.scanner import SnapperScanner
from ..snapper.source_policy import select_source
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
        provider = member.get("snapshot_provider", "harbor")
        if provider not in ("harbor", "snapper"):
            raise ValueError("unsupported machine-set snapshot provider")
        if not source_root.is_absolute() or not snap_path.is_absolute():
            raise ValueError("machine set source must be absolute")
        if provider == "harbor":
            if (
                source_root.is_symlink()
                or snap_path.is_symlink()
                or snap_path.parent.is_symlink()
                or snap_path.parent != source_root / NATIVE_FOLDER
                or not NAME_PATTERN.fullmatch(snap_path.name)
            ):
                raise ValueError(
                    "machine set source is not a Harbor-owned readonly snapshot"
                )
        else:
            config = member.get("snapper_config")
            number = member.get("snapper_number")
            if (
                not isinstance(config, str)
                or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", config)
                or type(number) is not int
                or number <= 0
                or snap_path != source_root / ".snapshots" / str(number) / "snapshot"
            ):
                raise ValueError("unsafe Snapper machine-set snapshot identity")
            if member.get("snapshot_uuid") is None:
                raise ValueError("Snapper machine-set requires immutable source UUID")
        if member.get("status") not in ("pending", "sending", "completed"):
            raise ValueError("invalid machine-set member state")
        # Advisory source measures may be persisted alongside ordinary
        # catalog writes; never trust unbounded or fractional values.
        reference = member.get("source_reference_bytes")
        if reference is not None and (
            type(reference) is not int or not 0 < reference < (1 << 62)
        ):
            raise ValueError("invalid advisory source reference size")
        retired = member.get("replay_retired", [])
        restarts = member.get("replay_restarts", 0)
        if (
            type(restarts) is not int
            or restarts not in (0, 1)
            or not isinstance(retired, list)
            or len(retired) != restarts
            or any(
                not isinstance(row, dict)
                or row.get("reason") != "raw_sha256_mismatch"
                or not isinstance(row.get("archive"), str)
                or not row["archive"].isascii()
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", row["archive"])
                or f"-{set_id[:8]}-{index:03d}" not in row["archive"]
                or not isinstance(row.get("transfer_id"), str)
                or not re.fullmatch(
                    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                    row["transfer_id"],
                )
                for row in retired
            )
        ):
            raise ValueError("invalid replay retirement history")
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


def _machine_progress_measure(
    root: Path,
    data: dict,
    *,
    forecast_rate: float | None = None,
    source_references: dict[str, int] | None = None,
) -> dict[str, object]:
    """One destination readdir per sample + optional rough source-size model.

    Only final archives and strict UUID .part files are counted. Existing
    telemetry never walks source trees or blocks to gather an estimate.
    """
    members = data["members"]
    total = len(members)
    completed = sum(m.get("status") == "completed" for m in members)
    current = next((m for m in members if m.get("status") != "completed"), None)
    archives = {member["archive"] for member in members}
    written: int | None = 0
    observed: dict[str, int] = {}
    parts: dict[str, int] = {}
    finals: dict[str, int] = {}
    try:
        with os.scandir(root) as directory:
            for entry in directory:
                archive, separator, suffix = entry.name.partition(".btrfs.zst")
                if not separator or archive not in archives:
                    continue
                if suffix:
                    if not (suffix.startswith(".") and suffix.endswith(".part")):
                        continue
                    transfer_id = suffix[1:-5]
                    try:
                        if str(uuid.UUID(transfer_id)) != transfer_id:
                            continue
                    except (ValueError, AttributeError):
                        continue
                info = entry.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                if suffix:
                    parts[archive] = max(parts.get(archive, 0), info.st_size)
                else:
                    finals[archive] = info.st_size
        # A completed stream may briefly coexist with a checkpoint part:
        # never double-count it or multiple retired transfers.
        observed = dict(parts)
        observed.update(finals)
        written = sum(observed.values())
    except OSError:
        # The mount guard, not the telemetry sampler, handles a lost export.
        written = None
        observed = {}

    # Advisory samples are kept outside the durable set catalog. The source
    # meter must never mutate shared data while _write serializes its journal.
    model_members = [
        {**member, "source_reference_bytes": source_references[member["archive"]]}
        if source_references is not None and member["archive"] in source_references
        else member
        for member in members
    ]
    forecast = (
        forecast_machine(model_members, observed, rate_bps=forecast_rate)
        if written is not None
        else forecast_machine([], {}, rate_bps=None)
    )
    return {
        "event": "machine_progress",
        "completed_members": completed,
        "total_members": total,
        "current_mount": current["original_mount"] if current else None,
        "bytes_written": written,
        "bytes_per_second": None,
        **forecast,
        # Legacy field remains null: the estimated % is a distinct and
        # explicitly approximate statistic.
        "percent": None,
        "eta_min_seconds": None,
        "eta_max_seconds": None,
        "eta_stalled": False,
    }


@contextmanager
def _live_machine_progress(
    root: Path, data: dict, *, estimate_sources: bool = False
) -> Iterator[None]:
    """Sample asynchronously; completion must never wait on stalled NFS stat.

    A blocking NFS syscall cannot be interrupted by joining its thread.
    The final update uses the most recent real sample, if any, and the latest
    in-memory member statuses, without performing another destination I/O.
    """
    stop = threading.Event()
    emit_lock = threading.Lock()
    started = time.monotonic()
    latest: dict[str, object] | None = None
    reference_samples: dict[str, int] = {}

    def source_meter() -> None:
        """Best-effort bounded FIEMAP samples while normal sending continues.

        No snapshot creation, quota changes, mount operations or destination
        metadata writes. A slow FIEMAP never holds the final JSON.
        """
        for member in data["members"]:
            if stop.is_set():
                break
            name = member["archive"]
            if (
                name in reference_samples
                or member.get("source_reference_bytes") is not None
            ):
                continue
            snap = Path(member.get("snapshot_path", "/path/not-created"))
            original = Path(member["original_mount"])
            path = snap if snap.is_dir() and not snap.is_symlink() else original
            estimated = estimate_source_bytes(path)
            if estimated is not None and not stop.is_set():
                reference_samples[name] = estimated

    def monitor() -> None:
        nonlocal latest
        last_time: float | None = None
        last_bytes: int | None = None
        smoothed_bps: float | None = None
        last_growth: float | None = None
        while not stop.is_set():
            payload = _machine_progress_measure(
                root,
                data,
                forecast_rate=smoothed_bps,
                source_references=reference_samples,
            )
            now = time.monotonic()
            current = payload["bytes_written"]
            if (
                isinstance(current, int)
                and last_bytes is not None
                and last_time is not None
                and now > last_time
                and current >= last_bytes
            ):
                instant = (current - last_bytes) / (now - last_time)
                payload["bytes_per_second"] = round(instant)
                if instant > 0:
                    smoothed_bps = (
                        instant
                        if smoothed_bps is None
                        else 0.65 * smoothed_bps + 0.35 * instant
                    )
                    last_growth = now
                if last_growth is not None and now - last_growth > 25:
                    # A 0 B/s stall is not a promise of progress.
                    smoothed_bps = None
                    payload["eta_stalled"] = True
                # The sample's observed file sizes are already known in the
                # model; recompute with current smoothed rate in the next
                # event instead of a second destination scan.
                if smoothed_bps and isinstance(current, int):
                    total_estimate = payload["estimated_total_bytes"]
                    minimum = payload["estimated_min_bytes"]
                    maximum = payload["estimated_max_bytes"]
                    if isinstance(total_estimate, int):
                        remaining = max(0, total_estimate - current)
                        payload["eta_seconds"] = round(remaining / smoothed_bps)
                        if isinstance(minimum, int) and isinstance(maximum, int):
                            payload["eta_min_seconds"] = round(
                                max(0, minimum - current) / smoothed_bps
                            )
                            payload["eta_max_seconds"] = round(
                                max(0, maximum - current) / smoothed_bps
                            )
                elif smoothed_bps is None:
                    payload["eta_seconds"] = None
            payload["elapsed_seconds"] = round(now - started)
            # Serialize stop-check and print so a stalled stat cannot emit
            # after the final record. No locks held during filesystem I/O.
            with emit_lock:
                if stop.is_set():
                    break
                print(json.dumps(payload, sort_keys=True), flush=True)
                latest = payload
            last_bytes = current if isinstance(current, int) else None
            last_time = now
            if stop.wait(2):
                break

    worker = threading.Thread(
        target=monitor, name="harbor-machine-progress", daemon=True
    )
    meter = (
        threading.Thread(
            target=source_meter, name="harbor-machine-source-meter", daemon=True
        )
        if estimate_sources
        else None
    )
    worker.start()
    if meter is not None:
        meter.start()
    try:
        yield
    finally:
        stop.set()
        # If NFS is stalled, the daemon sampler exits when its I/O returns;
        # it must not hold completion or block the final machine-set result.
        worker.join(timeout=0.1)
        if meter is not None:
            meter.join(timeout=0.1)
        with emit_lock:
            members = data["members"]
            current = next((m for m in members if m.get("status") != "completed"), None)
            payload = {
                "event": "machine_progress",
                "completed_members": sum(
                    m.get("status") == "completed" for m in members
                ),
                "total_members": len(members),
                "current_mount": current["original_mount"] if current else None,
                "bytes_written": latest["bytes_written"] if latest else None,
                "bytes_per_second": None,
                "elapsed_seconds": round(time.monotonic() - started),
                "eta_seconds": 0 if current is None else None,
                "eta_min_seconds": 0 if current is None else None,
                "eta_max_seconds": 0 if current is None else None,
                "eta_stalled": False,
                "percent": None,
                "estimated_total_bytes": (
                    latest.get("estimated_total_bytes") if latest else None
                ),
                "estimated_min_bytes": (
                    latest.get("estimated_min_bytes") if latest else None
                ),
                "estimated_max_bytes": (
                    latest.get("estimated_max_bytes") if latest else None
                ),
                "estimated_percent": (
                    100
                    if current is None
                    else latest.get("estimated_percent")
                    if latest
                    else None
                ),
                "estimate_quality": (
                    latest.get("estimate_quality") if latest else "unavailable"
                ),
                "estimate_method": (latest.get("estimate_method") if latest else None),
                "estimate_covered_members": (
                    latest.get("estimate_covered_members") if latest else 0
                ),
            }
            print(json.dumps(payload, sort_keys=True), flush=True)


def _rebase_divergent_member(member: dict, entry: dict) -> None:
    """Retire one incompatible send without removing its .part or journal.

    The caller holds the machine-set lock, has checked snapshot UUID and uses
    an exact failed_unrecoverable entry verified by checkpoint-v2. At most one
    automatic fresh send per member to prevent unbounded retries and disk use.
    """
    original = member["archive"]
    if (
        entry.get("name") != original
        or entry.get("state") != "failed_unrecoverable"
        or not isinstance(entry.get("transfer_id"), str)
        or str(uuid.UUID(entry["transfer_id"])) != entry["transfer_id"]
    ):
        raise ValueError("replay failure is not verified for the selected member")
    if member.get("replay_restarts", 0) != 0:
        raise ValueError("replay already restarted once; manual inspection required")
    member["replay_restarts"] = 1
    member["replay_retired"] = [
        {
            "archive": original,
            "transfer_id": entry["transfer_id"],
            "reason": "raw_sha256_mismatch",
        }
    ]
    member["archive"] = original + "-retry1"
    member["transfer_id"] = None
    member["status"] = "pending"


def _drive(root: Path, data: dict, *, allow_local: bool, state_dir: str | None) -> dict:
    for member in data["members"]:
        while True:
            archive = member["archive"]
            if member["status"] == "completed":
                # A completed, verified backup must not depend on the original
                # computer or a Snapper/native snapshot still being present.
                if not _archive_valid(root, archive):
                    raise ValueError(
                        f"completed member has invalid/missing archive: {archive}"
                    )
                break
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
            provider = member.get("snapshot_provider", "harbor")
            if provider == "snapper":
                config = member["snapper_config"]
                number = member["snapper_number"]
                scanner = SnapperScanner()
                snap = next(
                    (
                        candidate
                        for candidate in scanner.get_snapshots(config)
                        if candidate.number == number
                    ),
                    None,
                )
                if snap is None or snap.subvolume_path != snapshot_path:
                    raise ValueError("Snapper snapshot disappeared or was replaced")
            recorded_uuid = member.get("snapshot_uuid")
            if (
                recorded_uuid is None
                and member["status"] == "pending"
                and provider == "harbor"
            ):
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
                if entry["state"] == "failed_unrecoverable":
                    _rebase_divergent_member(member, entry)
                    _write(root, data, allow_local=allow_local)
                    continue
                if entry["state"] == "completed":
                    if not _archive_valid(root, archive):
                        raise ValueError("completed member failed checksum")
                    member["status"] = "completed"
                    member["transfer_id"] = entry["transfer_id"]
                    _write(root, data, allow_local=allow_local)
                    break
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
                    source=None if provider == "snapper" else member["snapshot_path"],
                    source_mode="selected-snapper" if provider == "snapper" else "path",
                    # Native snapshots use the verified native parent chain.
                    # Snapper transfers use existing checkpoint-v2 Snapper pins.
                    native_root=member["original_mount"]
                    if provider == "harbor"
                    else None,
                    parent=None,
                    checkpoint_size_mib=128,
                    performance="balanced",
                    max_incremental_depth=data.get("max_incremental_depth", 7),
                    snapper_config=member.get("snapper_config")
                    if provider == "snapper"
                    else None,
                    snapper_number=member.get("snapper_number")
                    if provider == "snapper"
                    else None,
                    native_name=None,
                    allow_local=allow_local,
                    experimental=True,
                    state_dir=state_dir,
                )
            member["status"] = "sending"
            _write(root, data, allow_local=allow_local)
            code = execute_checkpoint_v2(args)
            if code == 3 and args.checkpoint_action == "resume":
                mismatches = [
                    row
                    for row in _list_v2(root)
                    if row["name"] == archive and row["state"] == "failed_unrecoverable"
                ]
                if len(mismatches) != 1:
                    raise RuntimeError(
                        "raw replay failure could not be durably verified"
                    )
                _rebase_divergent_member(member, mismatches[0])
                _write(root, data, allow_local=allow_local)
                continue
            if code != 0:
                raise RuntimeError(f"checkpoint-v2 failed on member: {archive}")
            entries = [entry for entry in _list_v2(root) if entry["name"] == archive]
            if len(entries) != 1 or entries[0]["state"] != "completed":
                raise RuntimeError(f"member incomplete, resumable later: {archive}")
            if not _archive_valid(root, archive):
                raise RuntimeError(f"completed member has invalid archive: {archive}")
            member["status"] = "completed"
            member["transfer_id"] = entries[0]["transfer_id"]
            _write(root, data, allow_local=allow_local)
            break
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
    # Retired failed replays must not pin Snapper snapshots forever. Keep the
    # old lease until ALL replacements are durably completed; release then.
    # A pin cleanup error cannot invalidate already verified backup data.
    for member in data["members"]:
        for retired in member.get("replay_retired", []):
            try:
                from ..core.checkpoint_v2 import read_manifest
                from .checkpoint_v2_cmd import (
                    _manifest_path,
                    _release_snapper_pins,
                    _state_root,
                )

                old = read_manifest(_manifest_path(root, retired["transfer_id"]))
                if old.state != "failed_unrecoverable":
                    raise ValueError("retired replay journal changed")
                _release_snapper_pins(_state_root(state_dir), old)
            except (OSError, ValueError, RuntimeError) as exc:
                print(
                    f"machine-set: retired Snapper pin cleanup pending: {exc}",
                    file=sys.stderr,
                )
    return data


def _retention_inventory(root: Path, args: argparse.Namespace):
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
    return plan, records, snapshots


def _retention_plan_digest(root: Path, plan) -> str:
    from hashlib import sha256
    from ..core.native_send_v2 import destination_fingerprint
    from ..endpoint.mount_guard_v2 import capture_mount_identity

    # The operator must confirm the EXACT current plan AND destination.
    material = {
        "version": 1,
        "target": destination_fingerprint(capture_mount_identity(root), root),
        "eligible_sets": sorted(plan.eligible),
        "kept_sets": sorted(plan.keep),
    }
    return sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def _retention_plan(root: Path, args: argparse.Namespace) -> int:
    plan, records, _snapshots = _retention_inventory(root, args)
    print(
        json.dumps(
            {
                "dry_run": True,
                "deletion_performed": False,
                "plan_sha256": _retention_plan_digest(root, plan),
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


def _retention_apply(root: Path, args: argparse.Namespace) -> int:
    """Explicit confirmed GC, never part of a scheduled backup.

    The caller holds the destination-wide exclusive lock before planning.
    An unfinished remote partial, rescue ISO, checksum mismatch, unknown
    parent or profile fails closed before a single destructive rename.
    """
    from ..core.machine_retention_gc_v2 import collect_verified_files
    from ..core.checkpoint_v2 import read_manifest
    from .checkpoint_v2_cmd import _manifest_path

    if not args.confirm:
        raise ValueError("retention deletion requires explicit --confirm")
    token = getattr(args, "expected_plan_sha256", None)
    if not isinstance(token, str) or len(token) != 64:
        raise ValueError("retention deletion requires preview --expected-plan-sha256")
    plan, records, snapshots = _retention_inventory(root, args)
    if _retention_plan_digest(root, plan) != token:
        raise ValueError("retention plan/destination changed since preview")
    if not plan.eligible:
        print(json.dumps({"deleted": [], "note": "nothing eligible"}, sort_keys=True))
        return 0
    snapshot_by_name = {row.name: row for row in snapshots}
    if len(snapshot_by_name) != len(snapshots):
        raise ValueError("ambiguous snapshot names in retention inventory")
    candidate_names: set[str] = set()
    with MountGuard(root, capture_mount_identity(root)) as guard:
        existing = set(os.listdir(guard.directory_fd))
        if len(existing) > 8192:
            raise ValueError("oversized retention inventory")
        # Partials or unfinished atomic publications cannot be safely
        # assigned to an eligible machine-set. Refuse all deletions.
        if any(
            name.endswith((".part", ".tmp")) or name.startswith(".transaction-")
            for name in existing
        ):
            raise ValueError("uncommitted backup transaction blocks retention")
        for record in records:
            if record["set_id"] not in plan.eligible:
                continue
            sid = record["set_id"]
            if any(name.startswith(f"harbor-rescue-{sid}-") for name in existing):
                raise ValueError("eligible machine has rescue ISO, preserve it")
            if record["status"] not in (
                "completed_btrfs_only",
                "completed_btrfs_and_boot_files",
            ):
                raise ValueError("retention attempted incomplete machine set")
            catalog = f".harbor-machine-set-{sid}.json"
            if catalog not in existing:
                raise ValueError("eligible machine catalog absent")
            candidate_names.add(catalog)
            for member in record["members"]:
                archive = member["archive"]
                row = snapshot_by_name.get(archive)
                if (
                    member["status"] != "completed"
                    or row is None
                    or not _archive_valid(root, archive)
                ):
                    raise ValueError("candidate is not a fully verified archive")
                for suffix in (".btrfs.zst", ".btrfs.zst.meta"):
                    filename = archive + suffix
                    if filename not in existing:
                        raise ValueError("missing sealed archive file")
                    candidate_names.add(filename)
                transfer = member.get("transfer_id")
                if not isinstance(transfer, str):
                    raise ValueError(
                        "eligible machine lacks completed checkpoint journal"
                    )
                manifest = read_manifest(_manifest_path(root, transfer))
                if (
                    manifest.state != "completed"
                    or manifest.identity.get("profile_id") != args.profile_id
                    or manifest.identity.get("archive_name") != archive
                ):
                    raise ValueError("checkpoint journal mismatches eligible archive")
                journal_name = f".harbor-resume-{transfer}.json"
                if journal_name not in existing:
                    raise ValueError("completed checkpoint journal missing")
                candidate_names.add(journal_name)
            for member in record.get("boot_members", []):
                if member.get("status") != "completed" or not verify_boot_member(
                    root, member
                ):
                    raise ValueError("boot archive failed verification")
                candidate_names.add(member["archive"])
        if len(candidate_names) > 4096:
            raise ValueError("too many files for one retention transaction")
        guard.validate_fd(guard.directory_fd)
        # A second mount guard remains inside the already-held destination
        # lease; only the controlled quarantine can rename/unlink these files.
        result = collect_verified_files(root, guard, sorted(candidate_names))
        print(json.dumps(result, sort_keys=True))
        return 0


def _retention_resume(root: Path, args: argparse.Namespace) -> int:
    from ..core.machine_retention_gc_v2 import collect_verified_files

    if not args.confirm:
        raise ValueError("resume of interrupted retention requires --confirm")
    with MountGuard(root, capture_mount_identity(root)) as guard:
        result = collect_verified_files(root, guard, None, resume=True)
    print(json.dumps(result, sort_keys=True))
    return 0


def _snapper_latest_for_machine_sources(sources: list[str]) -> list[dict]:
    """Prefer immutable existing Snapper snapshots, preserving all data mounts.

    Snapper is configured per subvolume; a system with /swap, /var/log,
    /home or other Btrfs subvolumes may have only a single Snapper config.
    Reuse Snapper where available, and explicitly create Harbor snapshots
    only for otherwise uncovered *data* subvolumes. Never misrepresent a
    new Harbor snapshot as an existing Snapper snapshot in the set catalog.
    """
    scanner = SnapperScanner()
    configs = scanner.list_configs()
    chosen: list[dict] = []
    for source in sources:
        eligible = [
            config
            for config in configs
            if config.subvolume == Path(source) and config.fstype == "btrfs"
        ]
        if len(eligible) > 1:
            raise ValueError(
                f"Ambiguous Snapper configurations for {source}; "
                "cannot choose a source safely."
            )
        if not eligible:
            chosen.append({"source": source, "snapshot_provider": "harbor"})
            continue
        config = eligible[0]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", config.name):
            raise ValueError("invalid Snapper configuration name")
        decision = select_source(scanner.get_snapshots(config), mode="latest")
        if decision is None or decision.snapshot is None or not decision.source_uuid:
            chosen.append({"source": source, "snapshot_provider": "harbor"})
            continue
        candidate = decision.snapshot
        path = getattr(candidate, "subvolume_path", None)
        expected = Path(source) / ".snapshots" / str(candidate.number) / "snapshot"
        if (
            not isinstance(path, Path)
            or path != expected
            or path.is_symlink()
            or not path.is_dir()
        ):
            raise ValueError(f"Snapper snapshot path changed for {source}")
        details = inspect_readonly_btrfs_source(path)
        if details.uuid != decision.source_uuid:
            raise ValueError(f"Snapper readonly UUID changed for {source}")
        chosen.append(
            {
                "source": source,
                "snapshot_provider": "snapper",
                "snapshot_path": str(path),
                "snapshot_uuid": decision.source_uuid,
                "snapper_config": config.name,
                "snapper_number": candidate.number,
                "snapshot_created_utc": candidate.date.astimezone(
                    timezone.utc
                ).isoformat(),
            }
        )
    if len(chosen) != len(sources):
        raise ValueError("Snapper coverage is incomplete")
    return chosen


def _snapper_fresh_for_machine_sources(sources: list[str]) -> list[dict]:
    """Create fresh readonly Snapper snapshots on configured volumes.

    Missing Snapper configurations fall back explicitly to Harbor. A failure
    to create a snapshot in an existing config is fatal, never silently
    replaced with an older snapshot or live writable data.
    """
    from ..snapper.scanner import SnapperNotFoundError

    scanner = SnapperScanner()
    try:
        configs = scanner.list_configs()
    except SnapperNotFoundError:
        configs = []
    selected: list[dict] = []
    for source in sources:
        eligible = [
            config
            for config in configs
            if config.subvolume == Path(source) and config.fstype == "btrfs"
        ]
        if len(eligible) > 1:
            raise ValueError(f"Ambiguous Snapper configurations for {source}")
        if not eligible:
            selected.append({"source": source, "snapshot_provider": "harbor"})
            continue
        config = eligible[0]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", config.name):
            raise ValueError("invalid Snapper configuration name")
        try:
            result = subprocess.run(
                [
                    "snapper",
                    "-c",
                    config.name,
                    "create",
                    "--type",
                    "single",
                    "--read-only",
                    "--print-number",
                    "--cleanup-algorithm",
                    "number",
                    "--description",
                    "Btrfs Harbor whole-computer backup",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=90,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(
                f"Snapper snapshot creation failed for {config.name}: {exc}"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"Snapper snapshot creation failed for {config.name}: "
                + result.stderr.strip()[:256]
            )
        try:
            number = int(result.stdout.strip())
        except ValueError as exc:
            raise ValueError("Snapper did not report a snapshot number") from exc
        if number <= 0:
            raise ValueError("invalid new Snapper snapshot number")
        snapshot = next(
            (item for item in scanner.get_snapshots(config) if item.number == number),
            None,
        )
        if snapshot is None:
            raise ValueError("new Snapper snapshot is not visible in inventory")
        path = Path(source) / ".snapshots" / str(number) / "snapshot"
        if snapshot.subvolume_path != path or path.is_symlink() or not path.is_dir():
            raise ValueError("new Snapper snapshot path is unsafe")
        identity = inspect_readonly_btrfs_source(path)
        if not identity.readonly or not identity.uuid:
            raise ValueError("new Snapper snapshot is not immutable")
        selected.append(
            {
                "source": source,
                "snapshot_provider": "snapper",
                "snapshot_path": str(path),
                "snapshot_uuid": identity.uuid,
                "snapper_config": config.name,
                "snapper_number": number,
                "snapshot_created_utc": snapshot.date.astimezone(
                    timezone.utc
                ).isoformat(),
            }
        )
    return selected


def _exclude_verified_volatile_swap(
    sources: list[str],
    *,
    swaps_path: Path = Path("/proc/swaps"),
    fstab_path: Path = Path("/etc/fstab"),
    swap_mounts: tuple[str, ...] = ("/swap",),
) -> tuple[list[str], list[dict[str, object]]]:
    """Exclude ONLY proven swap-only subvolumes from data streams.

    Btrfs cannot snapshot an active swapfile subvolume; its contents are
    volatile. NEVER deactivate swap and NEVER ignore unrecognized data.
    """
    declared: set[str] = set()
    for raw in swaps_path.read_text(encoding="utf-8").splitlines()[1:]:
        columns = raw.rsplit(None, 4)
        if len(columns) == 5 and columns[1].lower() == "file":
            declared.add(__util__.unescape_mount_field(columns[0]))
    if fstab_path.exists():
        for raw in fstab_path.read_text(encoding="utf-8").splitlines():
            columns = raw.split("#", 1)[0].split()
            if len(columns) >= 3 and columns[2] == "swap":
                declared.add(__util__.unescape_mount_field(columns[0]))

    kept: list[str] = []
    excluded: list[dict[str, object]] = []
    for source in sources:
        root = Path(source)
        # Ordinary home/system volumes are not even traversed. An unusual
        # swap mount is eligible only if a known swapfile lives immediately
        # inside it; arbitrary data must never be silently omitted.
        if source not in swap_mounts and not any(
            Path(file).parent == root for file in declared
        ):
            kept.append(source)
            continue
        with os.scandir(root) as iterator:
            entries = list(iterator)
        if (not entries and source not in swap_mounts) or not all(
            entry.is_file(follow_symlinks=False) and str(root / entry.name) in declared
            for entry in entries
        ):
            kept.append(source)
            continue
        if not _is_real_subvolume_root(source):
            raise ValueError(f"swap source is not a Btrfs subvolume root: {source}")
        # Restore must recreate swapfile/subvolume, not paging data.
        excluded.append(
            {
                "mount_point": source,
                "reason": "verified_volatile_swap_only",
                "swapfiles": sorted(str(root / entry.name) for entry in entries),
                "restore": "Recreate swap subvolume/file on target; "
                "check fstab and hibernation resume offset.",
            }
        )
    return kept, excluded


def _sources_from_inventory() -> tuple[list[str], dict]:
    inventory = inspect_live_machine()
    if not inventory["inventory_complete"]:
        problems = inventory.get("warnings", [])
        if not isinstance(problems, list):
            raise ValueError("invalid Btrfs inventory warnings")
        priority: list[str] = []
        unmounted = inventory.get("unmounted_configured_volumes", [])
        if isinstance(unmounted, list):
            priority.extend(
                f"Configured filesystem not mounted: {row.get('mount_point')} "
                f"({row.get('filesystem')})"
                for row in unmounted
                if isinstance(row, dict)
            )
        missing = inventory.get("uncovered_persistent_mounts", [])
        if isinstance(missing, list):
            priority.extend(
                f"Non-Btrfs system data not covered: {row.get('mount_point')} "
                f"({row.get('filesystem')})"
                for row in missing
                if isinstance(row, dict)
            )
        binds = inventory.get("bind_mounts", [])
        if isinstance(binds, list):
            priority.extend(
                f"Uncovered Btrfs mount: {row.get('mount_point')}"
                for row in binds
                if isinstance(row, dict)
                and row.get("reason")
                in (
                    "uncovered_btrfs_bind_alias",
                    "uncovered_nonroot_btrfs_mount",
                )
            )
        detail = "; ".join((priority or [str(p) for p in problems])[:5])
        raise ValueError(
            "Cannot safely cover the whole computer: "
            + (detail or "Btrfs enumeration is incomplete")
            + ". No backup will be started."
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


def _execute_machine_set_unlocked(args: argparse.Namespace) -> int:
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
    if action == "set-retention-apply":
        return _retention_apply(root, args)
    if action == "set-retention-resume":
        return _retention_resume(root, args)
    if action == "set-list":
        # Destination-only catalog browsing is always read-only and portable,
        # including a foreign NFS mount or USB disk with a different path.
        # Never silently truncate or hide corrupt backups as "not found".
        guard, _ = _open_guard(root, allow_local=True)
        rows: list[dict[str, object]] = []
        with guard:
            names = sorted(os.listdir(guard.directory_fd))
            if len(names) > 8192:
                raise ValueError(
                    "destination catalog inventory exceeds safe scan limit"
                )
            selected = [
                name
                for name in names
                if name.startswith(".harbor-machine-set-") and name.endswith(".json")
            ]
            if len(selected) > 1024:
                raise ValueError("too many machine backup sets to enumerate safely")
            for name in selected:
                identifier = name[len(".harbor-machine-set-") : -len(".json")]
                guard.validate_fd(guard.directory_fd)
                data = _read(root, _canonical_uuid(identifier))
                rows.append(
                    {
                        "set_id": data["set_id"],
                        "status": data["status"],
                        "coverage": data["coverage"],
                        "bootable": False,
                        "created_utc": data.get("created_utc"),
                        "snapshot_provider": data.get("snapshot_provider", "harbor"),
                        "backup_trigger": data.get("backup_trigger", "manual"),
                        "excluded_technical_subvolumes": data.get(
                            "excluded_technical_subvolumes", []
                        ),
                        "source_snapshot_dates": [
                            member["snapshot_created_utc"]
                            for member in data["members"]
                            if isinstance(member.get("snapshot_created_utc"), str)
                        ],
                        "members": len(data["members"]),
                        "boot_partitions": len(data.get("boot_members", [])),
                    }
                )
            guard.validate_fd(guard.directory_fd)
        print(json.dumps(rows, sort_keys=True))
        return 0
    if action == "set-start":
        requested = getattr(args, "source", None) or []
        sources, inventory = (
            (requested, None) if requested else _sources_from_inventory()
        )
        excluded_technical: list[dict[str, object]] = []
        if not requested:
            sources, excluded_technical = _exclude_verified_volatile_swap(sources)
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
            if not _is_real_subvolume_root(str(live)):
                raise ValueError(
                    f"source is not a verified Btrfs subvolume root: {source}. "
                    "A bind-mounted directory or unavailable subvolume cannot "
                    "be snapshotted as a whole-machine backup."
                )
        provider = getattr(args, "snapshot_provider", "harbor")
        if provider not in ("harbor", "snapper", "snapper-fresh"):
            raise ValueError("unknown machine backup snapshot provider")
        if provider in ("snapper", "snapper-fresh") and requested:
            raise ValueError(
                "Snapper complete-machine mode requires automatic inventory, "
                "not selected individual sources"
            )
        # Use existing Snapper for configured sources. Non-Snapper data
        # volumes receive Harbor captures, labeled per member.
        snapper_sources = (
            _snapper_latest_for_machine_sources(sources)
            if provider == "snapper"
            else _snapper_fresh_for_machine_sources(sources)
            if provider == "snapper-fresh"
            else None
        )
        # Journal Harbor-owned reservations before snapshot creation.
        # Snapper mode journals verified, existing UUID identities instead.
        for i, source in enumerate(sources):
            member_provider = (
                snapper_sources[i]["snapshot_provider"]
                if snapper_sources is not None
                else "harbor"
            )
            member = {
                "archive": f"machine{timestamp}-{set_id[:8]}-{i:03d}",
                "original_mount": source,
                "snapshot_provider": member_provider,
                "status": "pending",
                "transfer_id": None,
            }
            selected_snapper = snapper_sources[i] if snapper_sources is not None else {}
            if member_provider == "harbor":
                reserved_name = reserve_native_snapshot_name()
                member.update(
                    {
                        "snapshot_path": str(
                            Path(source) / NATIVE_FOLDER / reserved_name
                        ),
                        "snapshot_uuid": None,
                    }
                )
            else:
                member.update(
                    {
                        key: selected_snapper[key]
                        for key in (
                            "snapshot_path",
                            "snapshot_uuid",
                            "snapper_config",
                            "snapper_number",
                            "snapshot_created_utc",
                        )
                    }
                )
            members.append(member)
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
            "snapshot_provider": (
                "mixed"
                if len({member["snapshot_provider"] for member in members}) > 1
                else members[0]["snapshot_provider"]
            ),
            "snapshot_requested_provider": provider,
            "excluded_technical_subvolumes": excluded_technical,
            "backup_trigger": "manual",
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
            with _live_machine_progress(root, data, estimate_sources=True):
                result = _drive(
                    root,
                    data,
                    allow_local=args.allow_local,
                    state_dir=args.state_dir,
                )
            return _result(result)
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
                with _live_machine_progress(root, data, estimate_sources=True):
                    result = _drive(
                        root,
                        data,
                        allow_local=args.allow_local,
                        state_dir=args.state_dir,
                    )
                return _result(result)
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


def execute_machine_set(args: argparse.Namespace) -> int:
    """One destination-wide shared lease covering complete Btrfs I/O/restore.

    Locks are nonblocking. GC takes the exclusive counterpart and refuses
    every active producer, reader, rescue ISO, SSH mirror, and scheduled run.
    """
    action = args.checkpoint_action
    read_only = action in (
        "set-status",
        "set-list",
        "set-restore",
        "set-rear-copy",
        "set-rear-recover",
        "set-retention-plan",
    )
    root = _target_root(args.target)
    if action in ("set-resume", "set-rescue-iso"):
        # A new global lease must NOT leave even its lock inode on a changed
        # or missing destination export. Validate the saved set fingerprint
        # before any write, exactly as the earlier mutation guard did.
        preflight, stable = _open_guard(root, allow_local=args.allow_local)
        with preflight:
            data = _read(root, _canonical_uuid(args.set_id))
            if stable != data.get("destination_fingerprint"):
                raise ValueError("machine-set destination fingerprint changed")
            preflight.validate_fd(preflight.directory_fd)
    with MountGuard(root, capture_mount_identity(root)) as guard:
        with destination_gc_lock(
            guard,
            exclusive=action in ("set-retention-apply", "set-retention-resume"),
            read_only=read_only,
        ):
            return _execute_machine_set_unlocked(args)


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
            # Restore-side pins live in the recovered Btrfs staging volume,
            # never the original backup export. Other engine invocations keep
            # their standard, durable source pins unchanged.
            env={
                **os.environ,
                "BTRFS_HARBOR_RESTORE_LOCK_ROOT": str(destination),
            },
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
