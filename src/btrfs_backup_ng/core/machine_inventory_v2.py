"""Read-only inventory of a Linux machine before declaring backup coverage.

Btrfs snapshots never include nested subvolumes, and Btrfs send cannot protect
the EFI system partition. This report is intentionally *not* a completion
certificate or a destructive discovery tool.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
import re
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
BOOT_ARCHIVE_PATHS = frozenset({"/boot", "/boot/efi", "/boot/firmware", "/efi"})
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
        identity: min(
            paths,
            key=lambda path: (
                path.startswith(EXTERNAL_PREFIXES)
                or path in ("/mnt", "/media", "/run/media"),
                len(Path(path).parts),
                len(path),
                path,
            ),
        )
        for identity, paths in roots.items()
    }

    # Find nested subvolumes that are reachable as directories inside an
    # already-mounted subvolume but are NOT separate mount points. A snapshot
    # of the parent creates only an empty stub, NOT the nested subvolume data.
    nested: dict[tuple[str, int], str] = {}
    mappings = []
    for mount in mounts:
        identity = (mount.device, mount.subvol_id)
        if preferred.get(identity) != mount.mount_point:
            continue
        prefix = PurePosixPath("/" + mount.subvol_path.lstrip("/"))
        mappings.append((mount, prefix))

    for sv in detection.subvolumes:
        identity = (sv.device or "", sv.id)
        if identity in preferred:
            continue
        if sv.is_snapshot or sv.classification in (
            SubvolumeClass.SNAPSHOT,
            SubvolumeClass.INTERNAL,
        ):
            continue
        if any(marker in sv.path + "/" for marker in SNAPSHOT_PATH_MARKERS):
            continue
        wanted = PurePosixPath(sv.path)
        if not wanted.is_absolute() or ".." in wanted.parts:
            continue
        possibilities = []
        for mount, prefix in mappings:
            if sv.device != mount.device or sv.id == mount.subvol_id:
                continue
            try:
                relative = wanted.relative_to(prefix)
            except ValueError:
                continue
            if not relative.parts or ".." in relative.parts:
                continue
            candidate = Path(mount.mount_point).joinpath(*relative.parts)
            if candidate.is_symlink() or any(
                parent.is_symlink()
                for parent in candidate.parents
                if parent != Path("/")
                and parent != Path(mount.mount_point)
                and Path(mount.mount_point) in parent.parents
            ):
                continue
            possibilities.append(
                (len(prefix.parts), len(mount.mount_point), str(candidate))
            )
        for _, _, path in sorted(
            possibilities, key=lambda item: (-item[0], item[1], item[2])
        ):
            if not _is_real_subvolume_root(path):
                continue
            # Check the root ID, not merely that btrfs-progs reports success:
            # another subvolume could have been substituted while scanning.
            result = subprocess.run(
                ["btrfs", "subvolume", "show", path],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            found = re.search(r"(?m)^\s*Subvolume ID:\s*(\d+)\s*$", result.stdout)
            if result.returncode == 0 and found and int(found.group(1)) == sv.id:
                nested[identity] = path
                break

    seen: set[tuple[str, int]] = set()
    for sv in detection.subvolumes:
        identity = (sv.device or "", sv.id)
        sv.mount_point = preferred.get(identity) or nested.get(identity)
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


def _configured_unmounted_system_volumes(
    mount_rows: list[dict[str, str]], fstab_text: str
) -> list[dict[str, str]]:
    """A missing /home in fstab is data loss, not a successful root backup."""
    current = {row["mount_point"] for row in mount_rows}
    missing = []
    for line in fstab_text.splitlines():
        fields = line.split("#", 1)[0].split()
        if len(fields) < 4:
            continue
        source, raw_target, filesystem, opts = fields[:4]
        target = __util__.unescape_mount_field(raw_target)
        options = set(opts.split(","))
        if (
            filesystem not in PERSISTENT_FILESYSTEMS
            or target in current
            or target in BOOT_ARCHIVE_PATHS
            or not target.startswith("/")
            or target.startswith(EXTERNAL_PREFIXES + ("/run/", "/snap/"))
            or target in ("/mnt", "/media", "/run/media", "/run", "/snap")
            or "bind" in options
            or "rbind" in options
        ):
            continue
        missing.append(
            {
                "source": source,
                "mount_point": target,
                "filesystem": filesystem,
            }
        )
    return missing


def inspect_live_machine(
    *, mounts_path: Path = Path("/proc/mounts"), fstab_path: Path | None = None
) -> dict[str, object]:
    """Read-only inventory, selecting only actual Btrfs subvolume roots."""
    mounts_content = mounts_path.read_text(encoding="utf-8")
    # External filesystems are not part of a one-click COMPUTER capture.
    # A missing/unreadable external disk must not prevent covering the OS.
    # We still list external mounts separately for truthful coverage reporting.
    detection = scan_system(allow_partial=True, include_removable=False)
    ignored, uncovered = _select_live_subvolume_roots(detection, mounts_content)
    report = build_machine_inventory(detection, _mount_rows(mounts_content))
    inventory_sources = report["sources"]
    if not isinstance(inventory_sources, list):
        raise ValueError("invalid machine inventory sources")
    covered = {
        (row["device"], str(row["subvolume_id"]))
        for row in inventory_sources
        if isinstance(row, dict) and row.get("included_by_default")
    }
    # A root under /mnt (or a snapshot/internal root) is NOT covered by the
    # normal system backup. Its subdirectory bind alias under /home cannot be
    # claimed as covered merely because the same Btrfs id exists somewhere.
    for row in ignored:
        path = row["mount_point"]
        external = path.startswith(EXTERNAL_PREFIXES) or path in (
            "/mnt",
            "/media",
            "/run/media",
        )
        if (
            row["reason"] == "bind_alias_of_verified_btrfs_root"
            and not external
            and (row["device"], row["subvolume_id"]) not in covered
        ):
            row["reason"] = "uncovered_btrfs_bind_alias"
            uncovered.append(row)
    report["bind_mounts"] = ignored
    # Production inventory must also detect a configured but *unmounted*
    # persistent /home or /var volume. Reading /etc/fstab is safe; no mount
    # operation is attempted. Fixture inventories with a custom mounts_path
    # do not accidentally read the CI runner's unrelated host fstab.
    configured = (
        fstab_path
        if fstab_path is not None
        else Path("/etc/fstab")
        if mounts_path == Path("/proc/mounts")
        else None
    )
    missing_configured = []
    if configured is not None and configured.exists():
        missing_configured = _configured_unmounted_system_volumes(
            _mount_rows(mounts_content), configured.read_text(encoding="utf-8")
        )
    report["unmounted_configured_volumes"] = missing_configured
    if missing_configured:
        report["inventory_complete"] = False
        warnings = report["warnings"]
        if not isinstance(warnings, list):
            raise ValueError("invalid inventory warnings")
        warnings.extend(
            f"Configured system volume is not mounted: {row['mount_point']} "
            f"({row['filesystem']}). A whole-computer backup must not silently "
            "skip this filesystem."
            for row in missing_configured
        )
    # A snapshot of / cannot capture a separate ext4/XFS/F2FS /home or /var.
    # Only independent EFI/boot filesystems have an explicit archive adapter.
    # Never report a successful "whole computer" backup if these are missing.
    unsupported = [
        row
        for row in _mount_rows(mounts_content)
        if row["filesystem"] != "btrfs"
        and row["mount_point"] not in BOOT_ARCHIVE_PATHS
        and not row["mount_point"].startswith(
            EXTERNAL_PREFIXES + ("/run/", "/snap/", "/proc/", "/sys/", "/dev/")
        )
        and row["mount_point"]
        not in (
            "/mnt",
            "/media",
            "/run/media",
            "/run",
            "/snap",
        )
    ]
    report["uncovered_persistent_mounts"] = unsupported
    if unsupported:
        report["inventory_complete"] = False
        warnings = report["warnings"]
        if not isinstance(warnings, list):
            raise ValueError("invalid warning inventory")
        warnings.extend(
            f"Persistent {row['filesystem']} mount {row['mount_point']} "
            "is not backed up by Btrfs snapshots or the EFI adapter. "
            "Whole-computer backup is blocked to prevent silently missing data."
            for row in unsupported
        )
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
