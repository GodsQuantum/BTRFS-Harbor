"""Portable (Linux) persistent worker requests without a permanent daemon.

The operator's local control directory must be trusted and already exist.
Requests are atomic, mode 0600, and survive logout/reboot. The worker polls
between checkpoints. An emergency stop may signal a matching live Btrfs send
process via pidfd; a missing pidfd never degrades to unsafe pid reuse.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import uuid
from pathlib import Path
from typing import Literal, cast

ACTIONS = frozenset({"run", "pause", "stop"})


def _proc_starttime(pid: int) -> str | None:
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
        return text.rsplit(")", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return None


class ControlJournal:
    def __init__(self, directory: Path, transfer_id: str) -> None:
        if str(uuid.UUID(transfer_id)) != transfer_id:
            raise ValueError("invalid resumable transfer id")
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(
                "checkpoint control directory must exist and not be a symlink"
            )
        self.directory = directory
        self.transfer_id = transfer_id
        self.name = f".harbor-control-{transfer_id}.json"

    def _dirfd(self) -> int:
        return os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def request(self, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError("invalid checkpoint control action")
        directory_fd = self._dirfd()
        name = f".harbor-control-{uuid.uuid4().hex}.tmp"
        fd = -1
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            body = json.dumps(
                {"transfer_id": self.transfer_id, "action": action},
                separators=(",", ":"),
            ).encode()
            with os.fdopen(fd, "wb") as output:
                fd = -1
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(
                name,
                self.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    def action(self) -> Literal["run", "pause", "stop"]:
        directory_fd = self._dirfd()
        try:
            try:
                fd = os.open(
                    self.name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return "run"
            with os.fdopen(fd, "rb") as reader:
                if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                    raise ValueError("control is not a regular file")
                data = json.loads(reader.read(4097))
            if (
                not isinstance(data, dict)
                or data.get("transfer_id") != self.transfer_id
                or data.get("action") not in ACTIONS
            ):
                raise ValueError("checkpoint control record is invalid")
            return cast(Literal["run", "pause", "stop"], data["action"])
        finally:
            os.close(directory_fd)

    @property
    def active_name(self) -> str:
        return f".harbor-active-{self.transfer_id}.json"

    def register_send(self, pid: int) -> None:
        """Record one live source worker PID/starttime for safe emergency Stop."""
        if type(pid) is not int or pid <= 1:
            raise ValueError("invalid Btrfs send pid")
        start = _proc_starttime(pid)
        if not start:
            raise RuntimeError("could not verify source process start time")
        directory_fd = self._dirfd()
        temporary = f".harbor-active-{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            with os.fdopen(fd, "wb") as writer:
                writer.write(
                    json.dumps(
                        {
                            "transfer_id": self.transfer_id,
                            "pid": pid,
                            "starttime": start,
                        },
                        separators=(",", ":"),
                    ).encode()
                )
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(
                temporary,
                self.active_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.close(directory_fd)

    def read_active_send(self) -> tuple[int, str] | None:
        """Return only a valid record from the same transfer id, no symlinks."""
        directory_fd = self._dirfd()
        try:
            try:
                fd = os.open(
                    self.active_name,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return None
            with os.fdopen(fd, "rb") as reader:
                if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
                    raise ValueError("invalid active source record")
                data = json.loads(reader.read(4097))
            if (
                not isinstance(data, dict)
                or data.get("transfer_id") != self.transfer_id
                or type(data.get("pid")) is not int
                or data["pid"] <= 1
                or not isinstance(data.get("starttime"), str)
                or not data["starttime"]
            ):
                raise ValueError("active send process identity is corrupt")
            return data["pid"], data["starttime"]
        finally:
            os.close(directory_fd)

    def clear_active_send(self) -> None:
        directory_fd = self._dirfd()
        try:
            try:
                os.unlink(self.active_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def signal_registered_send(self) -> bool:
        """Stop now by pidfd if this exact transfer still owns a live btrfs send."""
        if self.action() != "stop":
            return False
        registered = self.read_active_send()
        return bool(
            registered and self.signal_active_send(registered[0], registered[1])
        )

    def signal_active_send(self, pid: int, expected_starttime: str) -> bool:
        """Stop only a verified active Btrfs send, never an unrelated PID."""
        if self.action() != "stop":
            return False
        if not isinstance(pid, int) or pid <= 1:
            return False
        if not expected_starttime or _proc_starttime(pid) != expected_starttime:
            return False
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        except OSError:
            return False
        if not cmdline or Path(os.fsdecode(cmdline[0])).name != "btrfs":
            return False
        if b"send" not in cmdline[1:]:
            return False
        try:
            pidfd = os.pidfd_open(pid, 0)
        except (AttributeError, OSError):
            return False
        try:
            # PIDFD avoids a TOCTOU signal to a recycled process ID.
            signal.pidfd_send_signal(pidfd, signal.SIGTERM, None, 0)
            return True
        except (AttributeError, OSError):
            return False
        finally:
            os.close(pidfd)
