"""Durable non-Btrfs *boot file* coverage for a Btrfs machine-set.

This is deliberately NOT a bootable disk image.  A full-system ReaR/OVMF
recovery bridge must reconstruct partition layouts and bootloader safely.
No host partition table or filesystem is modified by this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tarfile
import uuid
from pathlib import Path
from typing import Any

from ..endpoint.mount_guard_v2 import (
    MountGuard,
    MountIdentity,
    capture_mount_identity,
)

# Reconstructing system firmware needs the actual, separately mounted ESP
# and independent /boot.  Do NOT archive an arbitrary root or user disk.
BOOT_PATHS = frozenset({"/boot", "/boot/efi", "/boot/firmware", "/efi"})
BOOT_FSTYPES = frozenset({"vfat", "ext2", "ext3", "ext4", "xfs", "f2fs"})
MAX_BOOT_ARCHIVE = 16 * 1024**3
BLOCK = 1024 * 1024


def discover_boot_mounts(
    mount_rows: list[dict[str, str]],
    *,
    mount_info: str | None = None,
) -> list[dict[str, str]]:
    """Only real distinct mounted persistent filesystems; never parent dirs."""
    by_mount: dict[str, dict[str, str]] = {}
    for row in mount_rows:
        mount = row["mount_point"]
        if mount not in BOOT_PATHS or row["filesystem"] == "btrfs":
            continue
        if row["filesystem"] not in BOOT_FSTYPES:
            raise ValueError(f"unsupported filesystem for boot recovery: {mount}")
        if mount in by_mount:
            raise ValueError(f"duplicate boot filesystem mount: {mount}")
        if not row["source"].startswith("/dev/"):
            raise ValueError(f"non-block boot mount source: {mount}")
        by_mount[mount] = dict(row)
    # A mounted ESP under /boot/efi must be captured separately from /boot.
    return [by_mount[k] for k in sorted(by_mount)]


def assert_configured_boot_mounts_present(
    mount_rows: list[dict[str, str]], fstab_text: str
) -> None:
    """Never declare coverage if an /etc/fstab boot partition is unmounted."""
    present = {row["mount_point"] for row in mount_rows}
    for line in fstab_text.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) < 3:
            continue
        mount_point = parts[1].replace(r"\040", " ")
        if mount_point in BOOT_PATHS and mount_point not in present:
            raise ValueError(f"configured boot filesystem not mounted: {mount_point}")


def snapshot_boot_layout() -> dict[str, Any]:
    """Read-only disk topology, useful to plan a future ReaR restore."""
    proc = subprocess.run(
        [
            "lsblk",
            "--json",
            "--bytes",
            "--paths",
            "--output",
            "PATH,TYPE,SIZE,FSTYPE,UUID,PARTUUID,PARTTYPE,PKNAME,MOUNTPOINTS",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=15,
        check=False,
    )
    if proc.returncode != 0 or len(proc.stdout) > 2_000_000:
        raise RuntimeError("cannot inventory partition identities")
    response = json.loads(proc.stdout)
    if not isinstance(response, dict) or not isinstance(
        response.get("blockdevices"), list
    ):
        raise ValueError("invalid read-only disk layout")
    return response


def _source_identity(member: dict[str, Any]) -> MountIdentity:
    source = Path(member["mount_point"])
    if not source.is_absolute() or source.is_symlink() or not source.is_dir():
        raise ValueError("boot partition mount is unavailable")
    identity = capture_mount_identity(source)
    if (
        identity.mount_point != str(source)
        or identity.fstype != member["filesystem"]
        or identity.source != member["source"]
    ):
        raise RuntimeError("boot filesystem unmounted or replaced")
    return identity


def capture_boot_member(
    root: Path,
    guard: MountGuard,
    member: dict[str, Any],
) -> dict[str, Any]:
    """Fsync a private tar.gz and publish by directory-FD atomic rename.

    If killed during streaming, the only residue is an orphaned private temp
    file; rerunning recomputes it safely, with no duplicate source copies.
    No writes to the source EFI partition and no restore to a host device.
    """
    name = member["archive"]
    if (
        not isinstance(name, str)
        or not name.startswith(".harbor-boot-")
        or not name.endswith(".tar.gz")
        or "/" in name
        or len(name) > 140
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-." for char in name)
    ):
        raise ValueError("invalid reserved boot archive name")
    guard.validate_fd(guard.directory_fd)
    source_mount = Path(member["mount_point"])
    start = _source_identity(member)
    final_fd = guard.directory_fd
    # A process killed during the previous attempt leaves a uniquely named
    # unpublished temporary file, never a valid archive. Clean ONLY our own
    # verified files under this pinned destination, not the user's folders.
    for entry in os.listdir(final_fd):
        middle = entry[len(name) + 1 : -4]
        if not (
            entry.startswith(name + ".")
            and entry.endswith(".tmp")
            and len(middle) == 32
            and all(c in "0123456789abcdef" for c in middle)
        ):
            continue
        info = os.stat(entry, dir_fd=final_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != os.geteuid()
        ):
            raise ValueError("unsafe orphan boot archive residue")
        guard.validate_fd(final_fd)
        os.unlink(entry, dir_fd=final_fd)
    guard.validate_fd(final_fd)
    # A final file from a crash before catalog commit may be replaced ONLY
    # with the same reserved name by the transaction holding its set lock.
    try:
        existing = os.stat(name, dir_fd=final_fd, follow_symlinks=False)
    except FileNotFoundError:
        existing = None
    if existing and (not stat.S_ISREG(existing.st_mode) or existing.st_nlink != 1):
        raise ValueError("boot archive destination is not a safe regular file")
    temp = f"{name}.{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temp,
        os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY,
        0o600,
        dir_fd=final_fd,
    )
    digest = hashlib.sha256()
    size = 0
    try:
        # -C path + "." prevents source-relative traversal. --one-file-system
        # keeps nested /boot/efi out of a separately saved /boot partition.
        proc = subprocess.Popen(
            [
                "tar",
                "--create",
                "--gzip",
                "--file",
                "-",
                "--numeric-owner",
                "--one-file-system",
                "--xattrs",
                "--acls",
                "--sparse",
                "--directory",
                str(source_mount),
                ".",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert proc.stdout is not None
        try:
            with os.fdopen(fd, "wb", closefd=False) as dest:
                while True:
                    block = proc.stdout.read(BLOCK)
                    if not block:
                        break
                    size += len(block)
                    if size > MAX_BOOT_ARCHIVE:
                        proc.kill()
                        raise ValueError("boot partition archive exceeds safety limit")
                    dest.write(block)
                    digest.update(block)
                dest.flush()
                os.fsync(fd)
            if proc.wait(timeout=60):
                raise RuntimeError("boot partition changed/unreadable during capture")
        finally:
            proc.stdout.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        if size < 20:
            raise ValueError("empty/invalid boot archive")
        final_source = _source_identity(member)
        if not start.matches_live(final_source):
            raise RuntimeError("source boot filesystem changed during capture")
        guard.validate_fd(final_fd)
        os.replace(temp, name, src_dir_fd=final_fd, dst_dir_fd=final_fd)
        os.fsync(final_fd)
        return {
            **member,
            "status": "completed",
            "sha256": digest.hexdigest(),
            "bytes": size,
        }
    finally:
        os.close(fd)
        try:
            os.unlink(temp, dir_fd=final_fd)
        except FileNotFoundError:
            pass


def verify_boot_member(root: Path, member: dict[str, Any]) -> bool:
    """Detect corruption, symlink swapping and size mismatches."""
    expected = member.get("sha256")
    length = member.get("bytes")
    if not isinstance(expected, str) or len(expected) != 64:
        return False
    if not isinstance(length, int) or length <= 0 or length > MAX_BOOT_ARCHIVE:
        return False
    path = root / member["archive"]
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size != length
        ):
            return False
        result = hashlib.sha256()
        with os.fdopen(fd, "rb", closefd=False) as stream:
            for block in iter(lambda: stream.read(BLOCK), b""):
                result.update(block)
        return result.hexdigest() == expected
    finally:
        os.close(fd)


def _validate_tar_safety(archive: Path) -> None:
    """A self-created archive can nevertheless be tampered with externally."""
    with tarfile.open(archive, mode="r:gz") as tar:
        for item in tar:
            path = Path(item.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or item.isdev()
                or item.isfifo()
            ):
                raise ValueError("unsafe boot archive member")
            if item.issym() or item.islnk():
                target = Path(item.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError("unsafe symlink in boot archive")


def stage_boot_members(
    root: Path,
    members: list[dict[str, Any]],
    destination: Path,
) -> list[dict[str, str]]:
    """Extract into NEW empty staging folders, never an active ESP or disk."""
    if (
        destination.is_symlink()
        or not destination.is_dir()
        or any(destination.iterdir())
    ):
        raise ValueError("boot staging must be an empty, real directory")
    plan: list[dict[str, str]] = []
    for index, member in enumerate(members):
        if not verify_boot_member(root, member):
            raise ValueError("corrupt/nonexistent boot archive")
        archive = root / member["archive"]
        _validate_tar_safety(archive)
        target = destination / f"boot-{index:03d}"
        target.mkdir(mode=0o700)
        # A non-destructive stage intentionally does not preserve ownership.
        with tarfile.open(archive, mode="r:gz") as tar:
            tar.extractall(target, filter="data")
        plan.append({"original_mount": member["mount_point"], "staging": str(target)})
    return plan
