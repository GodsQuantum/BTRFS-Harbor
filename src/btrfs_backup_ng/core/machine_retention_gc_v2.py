"""Journaled, fail-closed GC for verified Harbor v2 machine-set files.

Only the caller's prevalidated exact basenames may be retired. A private
quarantine and fsynced write-ahead journal support SIGKILL/restart. No
filesystem-wide scans that could delete user directories, Btrfs subvolumes,
other profiles, or foreign archives. Real destructive tests: CI disposable.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any, cast

from ..endpoint.mount_guard_v2 import MountGuard
from .native_send_v2 import destination_fingerprint
from ..endpoint.mount_guard_v2 import capture_mount_identity

TX_PREFIX = ".harbor-retention-v2-"
TX_RE = re.compile(
    r"\.harbor-retention-v2-([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\Z"
)
ALLOWED = re.compile(
    r"(?:machine[0-9]{8}T[0-9]{6}-[a-f0-9]{8}-[0-9]{3}\.btrfs\.zst(?:\.meta)?"
    r"|\.harbor-machine-set-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.json"
    r"|\.harbor-boot-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}-[0-9]{3}\.tar\.gz"
    r"|\.harbor-resume-[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\.json)\Z"
)
JOURNAL = "transaction.json"
MAX_FILES = 4096
MAX_JOURNAL = 2_000_000


def active_gc(guard: MountGuard) -> list[str]:
    guard.validate_fd(guard.directory_fd)
    matches = [
        name for name in os.listdir(guard.directory_fd) if name.startswith(TX_PREFIX)
    ]
    if any(TX_RE.fullmatch(name) is None for name in matches):
        raise ValueError("unrecognized pending retention transaction")
    return sorted(matches)


@contextmanager
def destination_gc_lock(guard: MountGuard, *, exclusive: bool) -> Iterator[None]:
    """All participating v2 writers take SH; GC takes exclusive.

    Never remove the lock inode. A broken NFS lock refuses rather than
    permitting two concurrent destructive owners.
    """
    guard.validate_fd(guard.directory_fd)
    fd = os.open(
        ".harbor-retention-v2.lock",
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
        0o600,
        dir_fd=guard.directory_fd,
    )
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise ValueError("invalid destination-wide retention lock")
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        guard.validate_fd(guard.directory_fd)
        if not exclusive and active_gc(guard):
            raise ValueError("backup destination has unfinished retention transaction")
        yield
    finally:
        os.close(fd)


def _safe(filename: object) -> str:
    if not isinstance(filename, str) or ALLOWED.fullmatch(filename) is None:
        raise ValueError("retention cannot touch foreign or unsafe archive filename")
    return filename


def _sha(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    checksum = hashlib.sha256()
    while content := os.read(fd, 1024 * 1024):
        checksum.update(content)
    return checksum.hexdigest()


def _check_file(
    directory: int, name: str, expected: dict[str, Any] | None = None
) -> dict[str, Any]:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError(
                "retention requires an unlinked regular file, never a symlink or hardlink"
            )
        body = {"name": name, "bytes": info.st_size, "sha256": _sha(fd)}
        if expected is not None and body != expected:
            raise ValueError("retention archive changed from write-ahead plan")
        return body
    finally:
        os.close(fd)


def _journal_write(directory: int, contents: dict[str, Any]) -> None:
    raw = (json.dumps(contents, sort_keys=True, separators=(",", ":")) + "\n").encode()
    if len(raw) > MAX_JOURNAL:
        raise ValueError("retention journal too large")
    name = f".transaction-{uuid.uuid4().hex}.tmp"
    fd = os.open(
        name,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory,
    )
    try:
        if os.write(fd, raw) != len(raw):
            raise OSError("short retention journal write")
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(name, JOURNAL, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _journal_read(directory: int) -> dict[str, Any]:
    fd = os.open(JOURNAL, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_size > MAX_JOURNAL
        ):
            raise ValueError("unsafe retention journal")
        value = json.loads(os.read(fd, MAX_JOURNAL + 1))
    finally:
        os.close(fd)
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or value.get("phase") not in ("stage", "delete")
    ):
        raise ValueError("corrupt retention journal")
    files = value.get("files")
    if not isinstance(files, list) or len(files) > MAX_FILES or not files:
        raise ValueError("invalid retention file list")
    if len({_safe(row.get("name")) for row in files if isinstance(row, dict)}) != len(
        files
    ):
        raise ValueError("duplicate or corrupt retention filename")
    for row in files:
        if (
            not isinstance(row, dict)
            or type(row.get("bytes")) is not int
            or row["bytes"] < 0
        ):
            raise ValueError("invalid retention file metadata")
        digest = row.get("sha256")
        if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise ValueError("invalid retention checksum")
    return value


def collect_verified_files(
    root: Path,
    guard: MountGuard,
    names: list[str] | None,
    *,
    resume: bool = False,
    fail_after_stages: int | None = None,
) -> dict[str, Any]:
    """Use only with caller-held destination-wide exclusive writer lock.

    If resume=False, every file must exist and be verified before a single
    destructive rename. If resume=True, only exactly one existing journal may
    be completed; its recorded untouched inventory must still match.
    """
    guard.validate_fd(guard.directory_fd)
    rootfd = guard.directory_fd
    active = active_gc(guard)
    if resume:
        if len(active) != 1 or names is not None:
            raise ValueError("exactly one pending retention transaction required")
        dirname = active[0]
    else:
        if active:
            raise ValueError("unfinished retention: resume it before any new deletion")
        if not names:
            return {"deleted": [], "resumed": False}
        if len(names) > MAX_FILES or len(set(names)) != len(names):
            raise ValueError("too many or duplicate retention files")
        for name in names:
            _safe(name)
        current = sorted(os.listdir(rootfd))
        planned = set(names)
        if any(name not in current for name in planned):
            raise ValueError("retention candidate disappeared before preflight")
        protected = sorted(set(current) - planned)
        files = [_check_file(rootfd, name) for name in sorted(planned)]
        stamp = uuid.uuid4()
        dirname = TX_PREFIX + str(stamp)
        os.mkdir(dirname, 0o700, dir_fd=rootfd)
        os.fsync(rootfd)
        stagefd = os.open(
            dirname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd
        )
        try:
            journal = {
                "schema": 1,
                "id": str(stamp),
                "phase": "stage",
                "files": files,
                "protected": protected,
                "root_fingerprint": destination_fingerprint(
                    capture_mount_identity(root), root
                ),
            }
            _journal_write(stagefd, journal)
        finally:
            os.close(stagefd)

    stagefd = os.open(
        dirname, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd
    )
    try:
        if not stat.S_ISDIR(os.fstat(stagefd).st_mode):
            raise ValueError("invalid retention quarantine directory")
        journal = _journal_read(stagefd)
        transaction_id = journal.get("id")
        if not isinstance(transaction_id, str) or dirname != TX_PREFIX + transaction_id:
            raise ValueError("retention transaction identity changed")
        if journal.get("root_fingerprint") != destination_fingerprint(
            capture_mount_identity(root), root
        ):
            raise ValueError("retention destination changed")
        rows = cast(list[dict[str, Any]], journal["files"])
        file_names = {row["name"] for row in rows}
        # The entries never targeted by this journal must remain identical.
        # A new archive after a crashed GC could refer to a retired parent.
        present = set(os.listdir(rootfd))
        untouched = sorted(present - file_names - {dirname})
        if untouched != journal.get("protected"):
            raise ValueError("retention protected catalog/foreign entry changed")
        staged = 0
        for row in rows:
            name = row["name"]
            if name in os.listdir(stagefd):
                _check_file(stagefd, name, row)
                if name in os.listdir(rootfd):
                    raise ValueError("retention duplicate file in quarantine and root")
                continue
            if name in os.listdir(rootfd):
                _check_file(rootfd, name, row)
                guard.validate_fd(rootfd)
                os.rename(name, name, src_dir_fd=rootfd, dst_dir_fd=stagefd)
                os.fsync(stagefd)
                os.fsync(rootfd)
                staged += 1
                if fail_after_stages is not None and staged >= fail_after_stages:
                    raise RuntimeError("injected GC crash after durable stage")
            elif journal["phase"] != "delete":
                raise ValueError("retention file unexpectedly missing before deletion")
        if journal["phase"] == "stage":
            journal["phase"] = "delete"
            _journal_write(stagefd, journal)
        for row in rows:
            name = row["name"]
            if name not in os.listdir(stagefd):
                continue
            _check_file(stagefd, name, row)
            guard.validate_fd(rootfd)
            os.unlink(name, dir_fd=stagefd)
            os.fsync(stagefd)
        guard.validate_fd(rootfd)
        os.unlink(JOURNAL, dir_fd=stagefd)
        os.fsync(stagefd)
    finally:
        os.close(stagefd)
    guard.validate_fd(rootfd)
    os.rmdir(dirname, dir_fd=rootfd)
    os.fsync(rootfd)
    return {"deleted": sorted(file_names), "resumed": resume}
