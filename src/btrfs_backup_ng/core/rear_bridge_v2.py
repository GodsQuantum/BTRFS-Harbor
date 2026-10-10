"""Fail-closed ReaR EXTERNAL restore bridge for *already reconstructed* mounts.

ReaR owns disk discovery/repartition/format/mount and bootloader finalization.
Harbor only maps verified staged Btrfs receive snapshots and boot-file archives
back to the matching, already mounted target paths. Nothing here partitions a
disk or runs a bootloader. Entry point requires an actual rescue session.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
VALID_MOUNT_PATH = re.compile(r"^/(?:[A-Za-z0-9_+@., -]+/)*[A-Za-z0-9_+@., -]*$")


@dataclass(frozen=True)
class RestoreCopy:
    source: Path
    target: Path
    original_mount: str
    is_boot_files: bool


def _safe_mount(mount: str) -> None:
    if (
        not isinstance(mount, str)
        or not VALID_MOUNT_PATH.fullmatch(mount)
        or "\x00" in mount
        or ".." in Path(mount).parts
        or len(mount) > 512
    ):
        raise ValueError("invalid recorded system mount path")


def _fs_type(mount: Path) -> str:
    response = subprocess.run(
        ["findmnt", "--noheadings", "--output", "FSTYPE", "--mountpoint", str(mount)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if response.returncode or not response.stdout.strip():
        raise ValueError(
            f"recovery destination is not an exact mounted filesystem: {mount}"
        )
    return response.stdout.strip()


def _received_uuid(path: Path) -> str | None:
    probe = subprocess.run(
        ["btrfs", "subvolume", "show", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if probe.returncode:
        return None
    for line in probe.stdout.splitlines():
        if line.lstrip().startswith("Received UUID:"):
            found = line.split(":", 1)[1].strip().lower()
            return found if UUID_PATTERN.fullmatch(found) else None
    return None


def find_received_subvolume(
    volume_folder: Path,
    source_uuid: str,
    *,
    probe: Callable[[Path], str | None] = _received_uuid,
) -> Path:
    """Find the requested received UUID, not a spoofable filename or last child."""
    if not UUID_PATTERN.fullmatch(source_uuid):
        raise ValueError("missing/invalid recorded source snapshot UUID")
    if volume_folder.is_symlink() or not volume_folder.is_dir():
        raise ValueError("staged volume is not a real directory")
    matches: list[Path] = []
    # A raw incremental restore may stage every required parent before the
    # selected snapshot, so the destination can have multiple Btrfs subvols.
    for candidate in volume_folder.iterdir():
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        if probe(candidate) == source_uuid.lower():
            matches.append(candidate)
    if len(matches) != 1:
        raise ValueError("ambiguous/missing received Btrfs subvolume identity")
    return matches[0]


def plan_rear_restore(
    catalog: dict[str, Any],
    staging: Path,
    system_root: Path,
    *,
    probe: Callable[[Path], str | None] = _received_uuid,
    filesystem: Callable[[Path], str] = _fs_type,
) -> tuple[RestoreCopy, ...]:
    """Pure plan: never write, verify all sources and target mounts first."""
    if catalog.get("status") not in (
        "completed_btrfs_only",
        "completed_btrfs_and_boot_files",
    ):
        raise ValueError("machine set not completed")
    if catalog.get("bootable") is not False:
        raise ValueError("unexpected bootability flag in source catalog")
    if (
        not staging.is_absolute()
        or staging.is_symlink()
        or not staging.is_dir()
        or not system_root.is_absolute()
        or system_root.is_symlink()
        or not system_root.is_dir()
    ):
        raise ValueError("staging and ReaR root must exist without symlinks")
    if staging.resolve() == system_root.resolve():
        raise ValueError("source staging cannot be the target root")
    if filesystem(system_root) != "btrfs":
        raise ValueError("ReaR root must already be mounted as Btrfs")
    members = catalog.get("members")
    if not isinstance(members, list) or not members:
        raise ValueError("missing Btrfs members")
    copies: list[RestoreCopy] = []
    seen: set[str] = set()
    for index, member in enumerate(members):
        if not isinstance(member, dict) or member.get("status") != "completed":
            raise ValueError("incomplete staged Btrfs volume")
        mount = member["original_mount"]
        _safe_mount(mount)
        if mount in seen:
            raise ValueError("duplicate mounted system path")
        seen.add(mount)
        target = system_root / mount.lstrip("/")
        if (
            target.is_symlink()
            or not target.is_dir()
            or not target.resolve().is_relative_to(system_root.resolve())
        ):
            raise ValueError(f"ReaR did not create system mount: {mount}")
        if filesystem(target) != "btrfs":
            raise ValueError(f"ReaR did not mount Btrfs volume: {mount}")
        source = find_received_subvolume(
            staging / f"volume-{index:03d}", member["snapshot_uuid"], probe=probe
        )
        copies.append(RestoreCopy(source, target, mount, False))
    boot = catalog.get("boot_members", [])
    for index, member in enumerate(boot):
        if member.get("status") != "completed":
            raise ValueError("boot archive was not completed")
        mount = member["mount_point"]
        _safe_mount(mount)
        if mount in seen:
            raise ValueError("duplicate system mount Btrfs/EFI")
        seen.add(mount)
        target = system_root / mount.lstrip("/")
        if (
            target.is_symlink()
            or not target.is_dir()
            or not target.resolve().is_relative_to(system_root.resolve())
        ):
            raise ValueError(f"ReaR did not mount EFI/boot target: {mount}")
        if filesystem(target) != member["filesystem"]:
            raise ValueError(f"ReaR EFI/boot mount type differs: {mount}")
        source = staging / "boot-files" / f"boot-{index:03d}"
        if source.is_symlink() or not source.is_dir():
            raise ValueError("missing staged boot files")
        copies.append(RestoreCopy(source, target, mount, True))
    # The target root must be the Btrfs source '/' and always copied first.
    if "/" not in seen:
        raise ValueError("no system root '/' included in machine backup")
    return tuple(
        sorted(
            copies,
            key=lambda item: (
                item.original_mount.count("/"),
                item.is_boot_files,
                item.original_mount,
            ),
        )
    )


def reconcile_fstab(
    contents: str,
    recorded_mounts: dict[str, tuple[str | None, str | None]],
    replacement_mounts: dict[str, tuple[str | None, str | None]],
) -> str:
    """Replace ONLY known old UUID/PARTUUID values for restored mounts.

    File layouts are ReaR's responsibility. Never blindly replace a UUID
    globally: one device may legitimately back several distinct subvolumes.
    """
    lines: list[str] = []
    for line in contents.splitlines(keepends=True):
        fields = line.split()
        if not fields or line.lstrip().startswith("#") or len(fields) < 4:
            lines.append(line)
            continue
        device, mount, fs_type = fields[:3]
        if fs_type in ("tmpfs", "proc", "sysfs", "devpts", "overlay"):
            lines.append(line)
            continue
        if mount not in recorded_mounts or mount not in replacement_mounts:
            raise ValueError(f"fstab has a filesystem outside Harbor backup: {mount}")
        original_uuid, original_partuuid = recorded_mounts[mount]
        current_uuid, current_partuuid = replacement_mounts[mount]
        if device.startswith("UUID="):
            if not original_uuid or device[5:].lower() != original_uuid.lower():
                raise ValueError(
                    f"fstab source UUID differs from recorded backup: {mount}"
                )
            if not current_uuid:
                raise ValueError(f"ReaR did not expose new filesystem UUID: {mount}")
            updated = "UUID=" + current_uuid
        elif device.startswith("PARTUUID="):
            if not original_partuuid or device[9:].lower() != original_partuuid.lower():
                raise ValueError(
                    f"fstab PARTUUID differs from recorded backup: {mount}"
                )
            if not current_partuuid:
                raise ValueError(f"ReaR did not expose new partition UUID: {mount}")
            updated = "PARTUUID=" + current_partuuid
        else:
            raise ValueError(
                f"unsupported device identifier for safe fstab repair: {mount}"
            )
        # Keep original whitespace/options/comments, edit only the first field.
        prefix = len(line) - len(line.lstrip())
        start = line[:prefix]
        rest = line[prefix + len(device) :]
        lines.append(start + updated + rest)
    return "".join(lines)


def recorded_uuids(layout: dict[str, Any]) -> dict[str, tuple[str | None, str | None]]:
    """Read-only machine inventory -> per-original mount fs and partition IDs."""
    if not isinstance(layout, dict) or not isinstance(layout.get("blockdevices"), list):
        raise ValueError("backup did not save a disk-layout inventory")
    found: dict[str, tuple[str | None, str | None]] = {}

    def scan(nodes: list[dict[str, Any]]) -> None:
        for node in nodes:
            if not isinstance(node, dict):
                raise ValueError("invalid disk layout entry")
            fs_uuid = node.get("uuid")
            part_uuid = node.get("partuuid")
            mountpoints = node.get("mountpoints", [])
            if isinstance(mountpoints, list):
                for mount in mountpoints:
                    if isinstance(mount, str) and mount.startswith("/"):
                        _safe_mount(mount)
                        old = found.get(mount)
                        pair = (
                            str(fs_uuid) if fs_uuid else None,
                            str(part_uuid) if part_uuid else None,
                        )
                        if old is not None and old != pair:
                            raise ValueError(
                                "ambiguous original partition UUID for mount"
                            )
                        found[mount] = pair
            children = node.get("children")
            if children is not None:
                if not isinstance(children, list):
                    raise ValueError("invalid nested partition table")
                scan(children)

    scan(layout["blockdevices"])
    return found


def _mounted_uuid(path: Path) -> tuple[str | None, str | None]:
    response = subprocess.run(
        [
            "findmnt",
            "--json",
            "--mountpoint",
            str(path),
            "--output",
            "UUID,PARTUUID",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if response.returncode:
        raise ValueError(f"missing ReaR mount UUID for {path}")
    import json

    value = json.loads(response.stdout)
    entries = value.get("filesystems")
    if not isinstance(entries, list) or len(entries) != 1:
        raise ValueError("ambiguous ReaR mounted filesystem UUID")
    row = entries[0]
    return (
        str(row["uuid"]) if row.get("uuid") else None,
        str(row["partuuid"]) if row.get("partuuid") else None,
    )


def _preflight_recovered_fstab(
    catalog: dict[str, Any], plan: tuple[RestoreCopy, ...]
) -> str:
    source = next((x.source for x in plan if x.original_mount == "/"), None)
    if source is None:
        raise ValueError("staged root is absent")
    system_config = source / "etc"
    source_fstab = system_config / "fstab"
    if system_config.is_symlink() or source_fstab.is_symlink():
        raise ValueError("unsafe staged /etc/fstab")
    if not source_fstab.is_file() or source_fstab.stat().st_size > 512 * 1024:
        raise ValueError("missing or excessively large root fstab")
    crypttab = system_config / "crypttab"
    if crypttab.exists():
        if crypttab.is_symlink() or crypttab.stat().st_size > 128 * 1024:
            raise ValueError("unsafe recovered crypttab")
        if any(
            line.strip() and not line.lstrip().startswith("#")
            for line in crypttab.read_text().splitlines()
        ):
            raise ValueError(
                "encrypted filesystems require certified ReaR recovery support"
            )
    layout = catalog.get("disk_layout")
    if not isinstance(layout, dict):
        raise ValueError("missing certified machine disk-layout inventory")
    recorded = recorded_uuids(layout)
    fresh = {job.original_mount: _mounted_uuid(job.target) for job in plan}
    return reconcile_fstab(source_fstab.read_text(encoding="utf-8"), recorded, fresh)


def _replace_fstab_pinned(root: Path, text: str) -> None:
    """Durable and no-symlink fstab replacement on the new recovery root."""
    folder = root / "etc"
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("restored /etc is not a safe directory")
    import uuid

    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temp = f".fstab.harbor-{uuid.uuid4().hex}.tmp"
    try:
        st = os.stat("fstab", dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError("restored fstab is not a regular file")
        out = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=fd,
        )
        try:
            with os.fdopen(out, "wb") as stream:
                stream.write(text.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, "fstab", src_dir_fd=fd, dst_dir_fd=fd)
            os.fsync(fd)
        finally:
            try:
                os.unlink(temp, dir_fd=fd)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def apply_rear_restore(
    catalog: dict[str, Any],
    staging: Path,
    *,
    system_root: Path = Path("/mnt/local"),
) -> tuple[RestoreCopy, ...]:
    """Only callable from ReaR rescue; does not create filesystems or partitions.

    Fail before ANY copy unless every volume and its source is verified.
    ReaR rebuilds bootloader/EFI entries during its subsequent finalize stage.
    """
    if os.geteuid() != 0 or not Path("/etc/rear/rescue.conf").is_file():
        raise PermissionError("Harbor system restore is only allowed in ReaR rescue")
    if system_root != Path("/mnt/local"):
        raise ValueError("ReaR recovery root must be exactly /mnt/local")
    if not Path("/mnt/local").is_mount():
        raise ValueError("ReaR has not mounted a recovered root filesystem")
    plan = plan_rear_restore(catalog, staging, system_root)
    # BEFORE writing any destination files, reconcile the old backed-up
    # fstab identities against ReaR's freshly mounted filesystem UUIDs.
    new_fstab = _preflight_recovered_fstab(catalog, plan)
    for job in plan:
        # Pinned directory FDs, not unchecked absolute paths: a source or
        # destination unmounted/replaced between preparation and rsync cannot
        # silently turn this operation into a write onto the parent volume.
        source_fd = os.open(job.source, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            target_fd = os.open(
                job.target, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:

                def still_same() -> None:
                    for candidate, fd in (
                        (job.source, source_fd),
                        (job.target, target_fd),
                    ):
                        pinned = os.fstat(fd)
                        current = os.stat(candidate, follow_symlinks=False)
                        if (
                            pinned.st_dev != current.st_dev
                            or pinned.st_ino != current.st_ino
                        ):
                            raise RuntimeError("recovery source or mount was replaced")

                still_same()
                # Child inherits ONLY the two explicit FDs. Trailing slash
                # means contents of received UUID-matched subvolume, not its
                # archive filename, are copied into ReaR's chosen mountpoint.
                subprocess.run(
                    [
                        "rsync",
                        "--archive",
                        "--hard-links",
                        "--acls",
                        "--xattrs",
                        "--numeric-ids",
                        "--one-file-system",
                        f"/proc/self/fd/{source_fd}/",
                        f"/proc/self/fd/{target_fd}/",
                    ],
                    check=True,
                    timeout=7200,
                    pass_fds=(source_fd, target_fd),
                )
                still_same()
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)
    _replace_fstab_pinned(system_root, new_fstab)
    return plan
