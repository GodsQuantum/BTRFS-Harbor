"""Read-only inventory of a Linux machine before declaring backup coverage.

Btrfs snapshots never include nested subvolumes, and Btrfs send cannot protect
the EFI system partition. This report is intentionally *not* a completion
certificate or a destructive discovery tool.
"""

from __future__ import annotations

from pathlib import Path
import stat
import subprocess

from ..detection.models import DetectedSubvolume
from ..detection.scanner import parse_proc_mounts

from .. import __util__
from ..detection.models import DetectionResult, SubvolumeClass
from ..detection.scanner import scan_system

PERSISTENT_FILESYSTEMS = frozenset(
    {
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "xfs",
        "vfat",
        "exfat",
        "ntfs",
        "ntfs3",
        "f2fs",
        "zfs",
        "bcachefs",
        "apfs",
        "hfsplus",
        "udf",
    }
)
EXTERNAL_PREFIXES = ("/media/", "/mnt/", "/run/media/")
SNAPSHOT_PATH_MARKERS = (
    "/.snapshots/",
    "/.btrfs-harbor-snapshots/",
    "/timeshift-btrfs/snapshots/",
)


def _mount_rows(content: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for line in content.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        source, mount_point, fs_type, _ = parts[:4]
        if fs_type not in PERSISTENT_FILESYSTEMS:
            continue
        rows.append(
            {
                "source": __util__.unescape_mount_field(source),
                "mount_point": __util__.unescape_mount_field(mount_point),
                "filesystem": fs_type,
            }
        )
    return rows


def build_machine_inventory(
    detection: DetectionResult,
    mount_rows: list[dict[str, str]],
) -> dict[str, object]:
    """Never infer complete coverage from an unknown or unmounted source."""
    devices = {(sv.device, sv.id) for sv in detection.subvolumes}
    sources: list[dict[str, object]] = []
    excluded: list[dict[str, str]] = []
    warnings: list[str] = []
    if detection.is_partial or detection.error_message:
        warnings.append(detection.error_message or "Incomplete Btrfs scan")

    # One independently backed-up member for each distinct subvolume, not
    # one for each bind mount or snapshot of the containing root.
    for sv in sorted(
        detection.subvolumes,
        key=lambda item: (item.device or "", item.path, item.id),
    ):
        path = sv.path
        is_snapshot = (
            sv.is_snapshot
            or sv.classification == SubvolumeClass.SNAPSHOT
            or any(marker in path + "/" for marker in SNAPSHOT_PATH_MARKERS)
        )
        if is_snapshot or sv.classification == SubvolumeClass.INTERNAL:
            excluded.append(
                {
                    "device": sv.device or "",
                    "path": path,
                    "reason": "snapshot" if is_snapshot else "internal",
                }
            )
            continue
        is_external = bool(
            sv.mount_point
            and (
                sv.mount_point.startswith(EXTERNAL_PREFIXES)
                or sv.mount_point in ("/mnt", "/media", "/run/media")
            )
        )
        include = sv.mount_point is not None and not is_external
        if sv.device is None:
            include = False
            warnings.append(f"Missing filesystem identity for subvolume {path}")
        if sv.mount_point is None:
            warnings.append(
                f"Unmounted subvolume cannot be backed up automatically: {path}"
            )
        if is_external:
            warnings.append(
                f"External Btrfs mount requires explicit opt-in: {sv.mount_point}"
            )
        sources.append(
            {
                "device": sv.device,
                "subvolume_id": sv.id,
                "subvolume_path": path,
                "mount_point": sv.mount_point,
                "classification": sv.classification.value,
                "included_by_default": include,
                "reason": (
                    "included"
                    if include
                    else "external_opt_in"
                    if is_external
                    else "unmounted_or_unverified"
                ),
            }
        )

    # A system-level recovery needs these partitions too: do not quietly
    # describe a Btrfs-only archive as a bootable image.
    other = [
        row
        for row in mount_rows
        if row["filesystem"] != "btrfs"
        and not row["mount_point"].startswith(("/snap/", "/run/"))
    ]
    for row in other:
        warnings.append(
            f"Non-Btrfs persistent filesystem excluded: "
            f"{row['mount_point']} ({row['filesystem']})"
        )
    covered = [s for s in sources if s["included_by_default"]]
    if not covered:
        warnings.append("No automatically selectable Btrfs source")
    if len(devices) != len(detection.subvolumes):
        warnings.append("Duplicate Btrfs subvolume identity encountered")
    return {
        "schema_version": 1,
        "scope": "currently mounted Linux filesystems and discoverable Btrfs subvolumes",
        "inventory_complete": not detection.is_partial and not detection.error_message,
        "bootable_recovery_ready": False,
        "complete_machine_backup": False,
        "included_btrfs_count": len(covered),
        "sources": sources,
        "excluded_internal": excluded,
        "non_btrfs_persistent_mounts": other,
        "warnings": list(dict.fromkeys(warnings)),
    }


def _is_real_subvolume_root(path: str) -> bool:
    """Btrfs bind mounts can expose a directory, not a subvolume root.

    A subvolid in /proc/mounts identifies the containing subvolume even for
    bind mounts of directories (e.g. ~/.local/share/fonts). Such mountpoints
    cannot be passed to btrfs subvolume snapshot. A real root has inode
    256, and btrfs-progs must also recognize it. This is READ-ONLY.
    """
    candidate = Path(path)
    try:
        if not candidate.is_absolute() or candidate.is_symlink():
            return False
        info = candidate.stat()
        if not stat.S_ISDIR(info.st_mode) or info.st_ino != 256:
            return False
        result = subprocess.run(
            ["btrfs", "subvolume", "show", path],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _select_live_subvolume_roots(
    detection: DetectionResult, mounts_content: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """Deduplicate bind aliases while reporting genuinely uncovered mounts.

    Never replace a missing true subvolume root with a bind mount of an inner
    directory. Fail closed if a non-external mounted Btrfs identity has no
    accessible true root; else a complete machine claim would omit data.
    """
    mounts = parse_proc_mounts(content=mounts_content, exclude_removable=False)
    roots: dict[tuple[str, int], list[str]] = {}
    invalid = []
    for mount in mounts:
        identity = (mount.device, mount.subvol_id)
        if mount.subvol_id > 0 and _is_real_subvolume_root(mount.mount_point):
            roots.setdefault(identity, []).append(mount.mount_point)
        else:
            invalid.append(mount)

    # Prefer the shortest stable path for multiple mounts of the same subvol.
    preferred = {
        identity: min(paths, key=lambda path: (len(Path(path).parts), len(path), path))
        for identity, paths in roots.items()
    }

    seen: set[tuple[str, int]] = set()
    for sv in detection.subvolumes:
        identity = (sv.device or "", sv.id)
        sv.mount_point = preferred.get(identity)
        seen.add(identity)

    # scan_system may not list top-level subvolumes (id 5).
    for identity, path in preferred.items():
        if identity in seen:
            continue
        device, subvolid = identity
        detection.subvolumes.append(
            DetectedSubvolume(id=subvolid, device=device, path=path, mount_point=path)
        )

    ignored = []
    uncovered = []
    for mount in invalid:
        path = mount.mount_point
        identity = (mount.device, mount.subvol_id)
        external = path.startswith(EXTERNAL_PREFIXES) or path in (
            "/mnt",
            "/media",
            "/run/media",
        )
        reason = (
            "external_not_auto_included"
            if external
            else "bind_alias_of_verified_btrfs_root"
            if identity in preferred
            else "uncovered_nonroot_btrfs_mount"
        )
        record = {
            "mount_point": path,
            "filesystem": "btrfs",
            "device": mount.device,
            "subvolume_id": str(mount.subvol_id),
            "reason": reason,
        }
        ignored.append(record)
        if reason == "uncovered_nonroot_btrfs_mount":
            uncovered.append(record)
    return ignored, uncovered


def inspect_live_machine(
    *, mounts_path: Path = Path("/proc/mounts")
) -> dict[str, object]:
    """Read-only inventory, selecting only actual Btrfs subvolume roots."""
    mounts_content = mounts_path.read_text(encoding="utf-8")
    detection = scan_system(allow_partial=True, include_removable=True)
    ignored, uncovered = _select_live_subvolume_roots(detection, mounts_content)
    report = build_machine_inventory(detection, _mount_rows(mounts_content))
    report["bind_mounts"] = ignored
    if uncovered:
        report["inventory_complete"] = False
        existing = report["warnings"]
        if not isinstance(existing, list):
            raise ValueError("invalid warning inventory")
        existing.extend(
            f"Btrfs directory mount has no accessible subvolume root: "
            f"{row['mount_point']} ({row['device']}, ID {row['subvolume_id']}). "
            "The complete-machine backup is blocked to avoid missing data."
            for row in uncovered
        )
    return report
