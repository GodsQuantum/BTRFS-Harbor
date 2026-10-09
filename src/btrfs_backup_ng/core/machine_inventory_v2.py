"""Read-only inventory of a Linux machine before declaring backup coverage.

Btrfs snapshots never include nested subvolumes, and Btrfs send cannot protect
the EFI system partition. This report is intentionally *not* a completion
certificate or a destructive discovery tool.
"""

from __future__ import annotations

from pathlib import Path

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
        is_snapshot = sv.classification == SubvolumeClass.SNAPSHOT or any(
            marker in path + "/" for marker in SNAPSHOT_PATH_MARKERS
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


def inspect_live_machine(
    *, mounts_path: Path = Path("/proc/mounts")
) -> dict[str, object]:
    """Read only, including external mounts for honest exclusion reporting."""
    detection = scan_system(allow_partial=True, include_removable=True)
    rows = _mount_rows(mounts_path.read_text(encoding="utf-8"))
    return build_machine_inventory(detection, rows)
