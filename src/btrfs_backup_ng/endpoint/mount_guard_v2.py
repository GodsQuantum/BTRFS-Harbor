"""Fail-closed, mount-aware directory guard for resumable raw destinations.

The live mount ID is intentionally NOT included in persistent identity: it
changes after reboot. Within one session it must stay constant. The pinned
directory file descriptor must still name the current directory at the path.
A failed mount comparison prevents any further filesystem writes.
"""

from __future__ import annotations

import ctypes
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

REMOTE_TYPES = frozenset({"nfs", "nfs4", "cifs", "smb3", "smbfs"})
STATX_MNT_ID_UNIQUE = 0x4000
AT_EMPTY_PATH = 0x1000


def _unescape(text: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


@dataclass(frozen=True)
class MountIdentity:
    fstype: str
    source: str
    root: str
    mount_point: str
    dev: str
    mount_id: int
    unique_mount_id: int | None

    def matches_persistent(self, other: MountIdentity) -> bool:
        return (self.fstype, self.source, self.root, self.mount_point) == (
            other.fstype,
            other.source,
            other.root,
            other.mount_point,
        )

    def matches_live(self, other: MountIdentity) -> bool:
        return (
            self.matches_persistent(other)
            and self.dev == other.dev
            and self.mount_id == other.mount_id
            and (
                self.unique_mount_id is None
                or other.unique_mount_id is None
                or self.unique_mount_id == other.unique_mount_id
            )
        )


def parse_mountinfo(contents: str) -> list[MountIdentity]:
    result: list[MountIdentity] = []
    for line in contents.splitlines():
        if " - " not in line:
            continue
        head, tail = line.split(" - ", 1)
        fields = head.split()
        fs = tail.split()
        if len(fields) < 6 or len(fs) < 2:
            continue
        try:
            mount_id = int(fields[0])
        except ValueError:
            continue
        result.append(
            MountIdentity(
                fstype=_unescape(fs[0]),
                source=_unescape(fs[1]),
                root=_unescape(fields[3]),
                mount_point=_unescape(fields[4]),
                dev=fields[2],
                mount_id=mount_id,
                unique_mount_id=None,
            )
        )
    return result


def _statx_unique_fd(fd: int) -> int | None:
    """Prefer Linux 6.8+ unique mount ID; unavailable is a safe fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    call = getattr(libc, "statx", None)
    if call is None:
        return None
    call.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    call.restype = ctypes.c_int
    buf = ctypes.create_string_buffer(256)
    if call(fd, b"", AT_EMPTY_PATH, STATX_MNT_ID_UNIQUE, buf) != 0:
        return None
    mask = int.from_bytes(buf.raw[0:4], "little")
    if not mask & STATX_MNT_ID_UNIQUE:
        return None
    return int.from_bytes(buf.raw[144:152], "little")


def _find_mount(path: Path, entries: list[MountIdentity]) -> MountIdentity:
    target = os.path.realpath(path)
    choices = [
        item
        for item in entries
        if target == item.mount_point
        or target.startswith(item.mount_point.rstrip("/") + "/")
    ]
    if not choices:
        raise RuntimeError(f"no mount found for destination {path}")
    return max(choices, key=lambda item: len(item.mount_point))


def capture_mount_identity(
    root: Path, *, reader: Callable[[], str] | None = None
) -> MountIdentity:
    """Capture actual mount for a directory, from current mount namespace."""
    if not root.is_dir() or root.is_symlink():
        raise ValueError("destination must be a non-symlink directory")
    text = reader() if reader is not None else Path("/proc/self/mountinfo").read_text()
    mount = _find_mount(root, parse_mountinfo(text))
    dir_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        unique = _statx_unique_fd(dir_fd)
        current_dev = os.fstat(dir_fd).st_dev
        # Mountinfo major:minor must correspond to actual pinned FD device.
        if os.makedev(*map(int, mount.dev.split(":"))) != current_dev:
            raise RuntimeError("filesystem device identity differs from mountinfo")
    finally:
        os.close(dir_fd)
    return MountIdentity(
        mount.fstype,
        mount.source,
        mount.root,
        mount.mount_point,
        mount.dev,
        mount.mount_id,
        unique,
    )


class MountGuard:
    """A live write authorization, not a durable permission to cross reboot."""

    def __init__(
        self,
        root: Path,
        identity: MountIdentity,
        *,
        required_remote: bool = False,
    ) -> None:
        if not isinstance(identity, MountIdentity):
            raise TypeError("a verified mount identity is required")
        self.root = root
        self.expected = identity
        self.required_remote = required_remote
        self.directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self._closed = False
        try:
            self.validate_fd(self.directory_fd)
        except BaseException:
            self.close()
            raise

    def validate_fd(self, directory_fd: int) -> None:
        if self._closed:
            raise RuntimeError("destination mount guard has been closed")
        if self.required_remote and self.expected.fstype not in REMOTE_TYPES:
            raise ValueError("refusing fallback to a non-network filesystem")
        current = capture_mount_identity(self.root)
        if not self.expected.matches_persistent(current):
            raise RuntimeError("destination remote export or filesystem changed")
        if not self.expected.matches_live(current):
            raise RuntimeError("destination live mount ID has changed")
        info = os.fstat(directory_fd)
        now = os.stat(self.root, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != (
            now.st_dev,
            now.st_ino,
        ):
            raise RuntimeError("destination mount directory replaced or detached")
        unique = _statx_unique_fd(directory_fd)
        if (
            unique is not None
            and current.unique_mount_id is not None
            and (unique != current.unique_mount_id)
        ):
            raise RuntimeError("pinned mount unique identity changed")

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.directory_fd)

    def __enter__(self) -> MountGuard:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def reopen_same_destination(
    root: Path,
    previous_identity: MountIdentity,
    *,
    required_remote: bool = False,
) -> MountGuard:
    """Resume after reboot: verify stable export first, then use current ID."""
    fresh = capture_mount_identity(root)
    if not previous_identity.matches_persistent(fresh):
        raise RuntimeError("saved destination does not match current mount")
    return MountGuard(root, fresh, required_remote=required_remote)
