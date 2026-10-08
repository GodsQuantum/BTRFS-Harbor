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

    def action(self) -> str:
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
            return data["action"]
        finally:
            os.close(directory_fd)

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
