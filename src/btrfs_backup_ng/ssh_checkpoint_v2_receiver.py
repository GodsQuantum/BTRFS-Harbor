"""Harbor SSH v2 receiver: restricted forced-command, no shell evaluation.

Dedicated unprivileged ssh authorized_keys entry should use:
 command="/usr/bin/python3 /usr/lib/btrfs-harbor/btrfs-backup-ng.pyz raw checkpoint-v2 ssh-receiver --root /ABSOLUTE/EXISTING/DIR",restrict ssh-ed25519 ...

The receiver MUST first be initialized locally on that machine while the
intended filesystem is mounted. Each call checks the pinned mount identity.
This transport mirrors already VERIFIED v2 archive files; btrfs send itself
remains the sole Harbor backup engine. Remote writes never use sudo.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, BinaryIO

from .core.native_send_v2 import destination_fingerprint
from .endpoint.mount_guard_v2 import MountGuard, capture_mount_identity

MAX_HEADER = 4096
MAX_CHUNK = 8 * 1024 * 1024
# Machine v2 files only. No user-supplied paths or arbitrary extensions.
MACHINE_STREAM = re.compile(
    r"machine[0-9]{8}T[0-9]{6}-[0-9a-f]{8}-[0-9]{3}\.btrfs\.zst(?:\.meta)?\Z"
)
MACHINE_SET = re.compile(
    r"\.harbor-machine-set-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.json\Z"
)
BOOT_FILE = re.compile(
    r"\.harbor-boot-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}-[0-9]{3}\.tar\.gz\Z"
)
ROOT_MARKER = ".harbor-ssh-v2-root.json"


def safe_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or len(name) > 180
        or not (
            MACHINE_STREAM.fullmatch(name)
            or MACHINE_SET.fullmatch(name)
            or BOOT_FILE.fullmatch(name)
        )
    ):
        raise ValueError("SSH receiver only accepts Harbor v2 machine archive names")
    return name


def _hex_hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None:
        raise ValueError("invalid sha256")
    return value


def _regular_fd(fd: int) -> None:
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("unsafe remote file (symlink, hardlink, or special file)")


def _digest_fd(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _read_json(directory: int, name: str) -> dict[str, Any] | None:
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
        )
    except FileNotFoundError:
        return None
    try:
        _regular_fd(fd)
        if os.fstat(fd).st_size > MAX_HEADER:
            raise ValueError("remote state too large")
        obj = json.loads(os.read(fd, MAX_HEADER + 1))
        if not isinstance(obj, dict):
            raise ValueError("invalid state object")
        return obj
    finally:
        os.close(fd)


def _atomic_json(directory: int, name: str, body: dict[str, Any]) -> None:
    raw = (json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_HEADER:
        raise ValueError("state limit exceeded")
    temp = f".harbor-ssh-tmp-{uuid.uuid4().hex}"
    fd = os.open(
        temp,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temp, dir_fd=directory)
        except FileNotFoundError:
            pass


def initialize_receiver(root: Path, *, allow_local: bool = False) -> dict[str, Any]:
    """One-time operator action. Never run implicitly over an absent NAS."""
    if os.geteuid() == 0:
        raise PermissionError("SSH receiver must not run as root")
    root = root.absolute()
    if not root.is_dir() or root.is_symlink():
        raise ValueError("preexisting nonsymlink destination directory required")
    identity = capture_mount_identity(root)
    if not allow_local and identity.fstype not in ("nfs", "nfs4", "cifs", "smb3"):
        raise ValueError("explicit --allow-local needed for non-network receiver root")
    if root.stat().st_uid != os.geteuid():
        raise PermissionError(
            "receiver directory must belong to the restricted account"
        )
    with MountGuard(root, identity) as guard:
        if _read_json(guard.directory_fd, ROOT_MARKER) is not None:
            raise ValueError("receiver mount already initialized")
        body = {
            "version": 1,
            "root": str(root),
            "destination_fingerprint": destination_fingerprint(identity, root),
        }
        raw = (json.dumps(body, sort_keys=True) + "\n").encode()
        fd = os.open(
            ROOT_MARKER,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=guard.directory_fd,
        )
        try:
            os.write(fd, raw)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(guard.directory_fd)
        return body


def _session(root: Path) -> MountGuard:
    if os.geteuid() == 0:
        raise PermissionError("SSH receiver must not run as root")
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        raise ValueError("invalid receiver root")
    if root.stat().st_uid != os.geteuid():
        raise PermissionError("receiver root owner changed")
    identity = capture_mount_identity(root)
    guard = MountGuard(root, identity)
    try:
        marker = _read_json(guard.directory_fd, ROOT_MARKER)
        if (
            marker is None
            or marker.get("version") != 1
            or marker.get("root") != str(root)
        ):
            raise ValueError("SSH root not initialized or marker altered")
        if marker.get("destination_fingerprint") != destination_fingerprint(
            identity, root
        ):
            raise ValueError("SSH destination filesystem/mount identity changed")
        return guard
    except BaseException:
        guard.close()
        raise


def _names(name: str) -> tuple[str, str]:
    token = hashlib.sha256(name.encode("ascii")).hexdigest()
    return f".harbor-ssh-v2-{token}.partial", f".harbor-ssh-v2-{token}.json"


@contextmanager
def _transfer_lock(directory: int, name: str) -> Iterator[None]:
    token = hashlib.sha256(name.encode("ascii")).hexdigest()
    filename = f".harbor-ssh-v2-lock-{token}"
    fd = os.open(
        filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        _regular_fd(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _state(
    directory: int, name: str, total: int, sha: str
) -> tuple[dict[str, Any], str, str]:
    partial, journal = _names(name)
    state = _read_json(directory, journal)
    if state is None:
        state = {
            "version": 1,
            "name": name,
            "total": total,
            "sha256": sha,
            "committed": 0,
            "last_size": 0,
            "last_sha256": None,
        }
    if (
        state.get("version") != 1
        or state.get("name") != name
        or state.get("total") != total
        or state.get("sha256") != sha
        or type(state.get("committed")) is not int
        or not 0 <= state["committed"] <= total
    ):
        raise ValueError("remote transaction identity changed or corrupt")
    return state, partial, journal


def _partial_fd(directory: int, filename: str, committed: int) -> int:
    fd = os.open(
        filename, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        _regular_fd(fd)
        actual = os.fstat(fd).st_size
        if actual < committed:
            raise ValueError("committed SSH bytes missing")
        # Power loss after stream write but before journal fsync: safe tail replay.
        if actual > committed:
            os.ftruncate(fd, committed)
            os.fsync(fd)
        os.lseek(fd, committed, os.SEEK_SET)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _verify_last(fd: int, state: dict[str, Any]) -> None:
    n = state["last_size"]
    committed = state["committed"]
    if type(n) is not int or not 0 <= n <= MAX_CHUNK or n > committed:
        raise ValueError("corrupt last-commit length")
    if n:
        expected = _hex_hash(state["last_sha256"])
        os.lseek(fd, committed - n, os.SEEK_SET)
        if hashlib.sha256(os.read(fd, n)).hexdigest() != expected:
            raise ValueError("last committed SSH chunk damaged")


def dispatch_receiver(
    root: Path, request: dict[str, Any], stdin: BinaryIO
) -> dict[str, Any]:
    """Operate on a single validated file, fully under the pinned FD."""
    if request.get("version") != 1:
        raise ValueError("unsupported SSH protocol version")
    name = safe_name(request.get("name"))
    sha = _hex_hash(request.get("sha256"))
    total = request.get("total")
    if type(total) is not int or not 0 < total <= (1 << 42):
        raise ValueError("invalid archive length")
    op = request.get("op")
    if op not in ("status", "put", "finish"):
        raise ValueError("disallowed SSH operation")
    with _session(root) as guard:
        with _transfer_lock(guard.directory_fd, name):
            return _locked_dispatch(guard, name, sha, total, op, request, stdin)


def _locked_dispatch(
    guard: MountGuard,
    name: str,
    sha: str,
    total: int,
    op: str,
    request: dict[str, Any],
    stdin: BinaryIO,
) -> dict[str, Any]:
    dfd = guard.directory_fd
    state, partial, journal = _state(dfd, name, total, sha)
    try:
        completed = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd
        )
    except FileNotFoundError:
        completed = -1
    if completed >= 0:
        try:
            info = os.fstat(completed)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink not in (1, 2):
                raise ValueError("unsafe finalized SSH object")
            if info.st_size != total or _digest_fd(completed) != sha:
                raise ValueError("remote finalized object has different content")
            if info.st_nlink == 2:
                stage = os.open(partial, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dfd)
                try:
                    other = os.fstat(stage)
                    if (other.st_dev, other.st_ino) != (info.st_dev, info.st_ino):
                        raise ValueError("foreign hardlink at destination")
                finally:
                    os.close(stage)
                guard.validate_fd(dfd)
                os.unlink(partial, dir_fd=dfd)
                os.fsync(dfd)
            if _read_json(dfd, journal) is not None:
                os.unlink(journal, dir_fd=dfd)
                os.fsync(dfd)
            return {"version": 1, "name": name, "committed": total, "complete": True}
        finally:
            os.close(completed)
    # A missing partial is allowed only before any byte is committed.
    fd = _partial_fd(dfd, partial, state["committed"])
    try:
        _verify_last(fd, state)
        if op == "put":
            offset = request.get("offset")
            length = request.get("size")
            piece_sha = _hex_hash(request.get("chunk_sha256"))
            if type(offset) is not int or offset != state["committed"]:
                raise ValueError("unexpected SSH chunk offset; query status and resume")
            if (
                type(length) is not int
                or not 0 < length <= MAX_CHUNK
                or length > total - offset
            ):
                raise ValueError("invalid remote chunk length")
            # Read and validate BEFORE any disk write: avoids truncated partials.
            chunk = stdin.read(length)
            if len(chunk) != length or stdin.read(1) != b"":
                raise ValueError("truncated or overlong SSH chunk")
            if hashlib.sha256(chunk).hexdigest() != piece_sha:
                raise ValueError("SSH chunk SHA256 mismatch")
            os.lseek(fd, offset, os.SEEK_SET)
            if os.write(fd, chunk) != length:
                raise OSError("short remote write")
            os.fsync(fd)
            state.update(
                {
                    "committed": offset + length,
                    "last_size": length,
                    "last_sha256": piece_sha,
                }
            )
            guard.validate_fd(dfd)
            _atomic_json(dfd, journal, state)
        elif op == "finish":
            if state["committed"] != total:
                raise ValueError("cannot finish incomplete remote backup")
            if _digest_fd(fd) != sha:
                raise ValueError("remote complete checksum mismatch")
            guard.validate_fd(dfd)
            # Atomic no-replace publication; never overwrite another file.
            os.link(
                partial, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False
            )
            os.fsync(dfd)
            os.unlink(partial, dir_fd=dfd)
            os.unlink(journal, dir_fd=dfd)
            os.fsync(dfd)
            return {
                "version": 1,
                "name": name,
                "committed": total,
                "complete": True,
            }
        guard.validate_fd(dfd)
        return {
            "version": 1,
            "name": name,
            "committed": state["committed"],
            "complete": False,
        }
    finally:
        os.close(fd)


def receiver_main(root: str) -> int:
    # OpenSSH passes requested command verbatim; NEVER eval it as a shell!
    if os.environ.get("SSH_ORIGINAL_COMMAND") != "harbor-v2":
        raise PermissionError("forced SSH command or identity mismatch")
    raw = sys.stdin.buffer.readline(MAX_HEADER + 1)
    if len(raw) > MAX_HEADER or not raw.endswith(b"\n"):
        raise ValueError("invalid bounded SSH message header")
    request = json.loads(raw)
    if not isinstance(request, dict):
        raise ValueError("invalid SSH request")
    response = dispatch_receiver(Path(root), request, sys.stdin.buffer)
    sys.stdout.write(json.dumps(response, sort_keys=True) + "\n")
    sys.stdout.flush()
    return 0
