"""Build ReaR rescue ISO locally then copy atomically to pinned destination.

No global ReaR config, no partition formatting, no mount changes.
An ISO file is not a full bare-metal boot certification.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from ..endpoint.mount_guard_v2 import MountGuard

REAR_CONFIG = Path("/usr/share/btrfs-harbor/rear")
REAR_OUTPUT = Path("/var/lib/rear/output")
REAR_SCRIPT = Path("/usr/lib/btrfs-harbor/recovery/harbor-rear-restore")
ISO_MAX = 8 * 1024**3
ISO_MIN = 65536


def _iso_inventory(root: Path) -> dict[Path, tuple[int, int]]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("ReaR local output directory is missing")
    found: dict[Path, tuple[int, int]] = {}
    for file in root.rglob("*.iso"):
        if file.is_symlink() or not file.is_file():
            raise ValueError("unsafe ReaR ISO output")
        if not file.resolve().is_relative_to(root.resolve()):
            raise ValueError("ReaR ISO path escaped output")
        info = file.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("ReaR ISO is not an ordinary file")
        found[file] = (info.st_mtime_ns, info.st_size)
        if len(found) > 100:
            raise ValueError("ambiguous ReaR ISO output")
    return found


def _check_installation() -> None:
    if os.geteuid() != 0:
        raise PermissionError("ReaR rescue ISO requires authorized root")
    binary = Path("/usr/sbin/rear")
    config = REAR_CONFIG / "local.conf"
    if not binary.is_file() or binary.is_symlink():
        raise FileNotFoundError("Install ReaR 2.9+ from distro repositories")
    if config.is_symlink() or not config.is_file() or config.stat().st_uid != 0:
        raise ValueError("missing or untrusted installed ReaR config")
    if not REAR_SCRIPT.is_file() or REAR_SCRIPT.is_symlink():
        raise FileNotFoundError("missing installed Harbor rescue restoration hook")
    text = config.read_text(encoding="utf8")
    if (
        "OUTPUT=ISO" not in text
        or "BACKUP=EXTERNAL" not in text
        or "EXTERNAL_RESTORE='/usr/lib/btrfs-harbor/recovery/harbor-rear-restore'"
        not in text
    ):
        raise ValueError("ReaR config lacks the static Harbor restoration command")


def _check_catalog(catalog: dict[str, Any]) -> None:
    if catalog.get("status") != "completed_btrfs_and_boot_files":
        raise ValueError("an EFI rescue ISO needs a completed machine backup")
    members = catalog.get("members", [])
    boot = catalog.get("boot_members", [])
    if not isinstance(members, list) or not any(
        m.get("original_mount") == "/" and m.get("status") == "completed"
        for m in members
    ):
        raise ValueError("machine root Btrfs is not fully backed up")
    if not isinstance(boot, list) or not any(
        b.get("filesystem") == "vfat"
        and b.get("mount_point") in ("/efi", "/boot/efi", "/boot/firmware")
        and b.get("status") == "completed"
        for b in boot
    ):
        raise ValueError("completed backup has no FAT32 EFI system partition")


def _copy_pinned(source: Path, guard: MountGuard, set_id: str) -> dict[str, Any]:
    fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    target = f"harbor-rescue-{set_id}-{uuid.uuid4().hex[:12]}.iso"
    temp = f".{target}.tmp"
    directory = guard.directory_fd
    digest = hashlib.sha256()
    length = 0
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1:
            raise ValueError("unsafe rescue ISO input")
        if not ISO_MIN <= meta.st_size <= ISO_MAX:
            raise ValueError("rescue ISO has invalid length")
        os.lseek(fd, 32769, os.SEEK_SET)
        if os.read(fd, 5) != b"CD001":
            raise ValueError("rescue ISO lacks an ISO9660 descriptor")
        os.lseek(fd, 0, os.SEEK_SET)
        guard.validate_fd(directory)
        out = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=directory,
        )
        try:
            with os.fdopen(out, "wb") as stream:
                while True:
                    part = os.read(fd, 1024 * 1024)
                    if not part:
                        break
                    guard.validate_fd(directory)
                    stream.write(part)
                    digest.update(part)
                    length += len(part)
                    if length > ISO_MAX:
                        raise ValueError("rescue ISO exceeded limit")
                stream.flush()
                os.fsync(stream.fileno())
            finished = os.fstat(fd)
            if (
                length != meta.st_size
                or finished.st_mtime_ns != meta.st_mtime_ns
                or finished.st_size != meta.st_size
                or finished.st_ino != meta.st_ino
                or finished.st_dev != meta.st_dev
            ):
                raise ValueError("local ReaR ISO changed during copying")
            guard.validate_fd(directory)
            os.link(
                temp,
                target,
                src_dir_fd=directory,
                dst_dir_fd=directory,
                follow_symlinks=False,
            )
            os.fsync(directory)
            return {
                "iso_created": True,
                "boot_tested": False,
                "iso9660_header_verified": True,
                "name": target,
                "bytes": length,
                "sha256": digest.hexdigest(),
            }
        finally:
            try:
                os.unlink(temp, dir_fd=directory)
                os.fsync(directory)
            except FileNotFoundError:
                pass
    finally:
        os.close(fd)


def build_rescue_iso(
    root: Path,
    catalog: dict[str, Any],
    guard: MountGuard,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    output: Path = REAR_OUTPUT,
    check_installation: bool = True,
) -> dict[str, Any]:
    _check_catalog(catalog)
    if check_installation:
        _check_installation()
    guard.validate_fd(guard.directory_fd)
    previous = _iso_inventory(output)
    result = runner(
        ["/usr/sbin/rear", "-c", str(REAR_CONFIG), "mkrescue"],
        capture_output=True,
        text=True,
        check=False,
        timeout=3600,
    )
    if result.returncode:
        raise RuntimeError(
            "ReaR ISO generation failed: " + (result.stderr or result.stdout)[-1200:]
        )
    current = _iso_inventory(output)
    new = [
        path
        for path, data in current.items()
        if previous.get(path) != data and data[1] >= ISO_MIN
    ]
    if len(new) != 1:
        raise ValueError("ReaR returned without one uniquely identifiable new ISO")
    guard.validate_fd(guard.directory_fd)
    return _copy_pinned(new[0], guard, catalog["set_id"])
