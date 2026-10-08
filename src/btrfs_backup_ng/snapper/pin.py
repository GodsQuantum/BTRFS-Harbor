"""Durable Snapper native cleanup pin leasing with crash-safe journal.

Clear cleanup eligibility *before* Btrfs send. Keep a persistent per-snapshot
lease set, so finishing one transfer never unpins another. On UUID mismatch,
never restore metadata to a different snapshot occupying the same number.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator


@dataclass(frozen=True)
class LiveSnapshot:
    uuid: str
    cleanup: str


def modify_snapper_cleanup(config: str, number: int, cleanup: str) -> None:
    """Use Snapper's native cleanup policy, no manual info.xml modification."""
    if not config or "/" in config or number <= 0:
        raise ValueError("invalid Snapper configuration or number")
    subprocess.run(
        [
            "snapper",
            "-c",
            config,
            "modify",
            "--cleanup-algorithm",
            cleanup,
            str(number),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )


class PinManager:
    def __init__(
        self,
        state_path: Path,
        *,
        query: Callable[[str, int], LiveSnapshot | None],
        modify: Callable[[str, int, str], None] = modify_snapper_cleanup,
    ):
        self.state_path = state_path
        self.query = query
        self.modify = modify

    @contextmanager
    def _locked(self) -> Iterator[dict]:
        directory = self.state_path.parent
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError("pin state directory must already exist safely")
        lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if self.state_path.exists():
                stat = self.state_path.lstat()
                if not stat.st_mode & 0o170000 == 0o100000:
                    raise RuntimeError("pin journal is not a regular file")
                with self.state_path.open("rb") as source:
                    data = source.read(1024 * 1024 + 1)
                if len(data) > 1024 * 1024:
                    raise RuntimeError("pin journal exceeds size limit")
                state = json.loads(data)
                if state.get("version") != 1 or not isinstance(state.get("pins"), dict):
                    raise ValueError("unsupported/corrupt pin journal")
            else:
                state = {"version": 1, "pins": {}}
            yield state
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _save(self, state: dict) -> None:
        content = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        temp = self.state_path.with_name(
            "." + self.state_path.name + "." + uuid.uuid4().hex + ".tmp"
        )
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as writer:
                writer.write(content)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(temp, self.state_path)
            parent_fd = os.open(
                self.state_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _key(config: str, number: int) -> str:
        if not config or "/" in config or type(number) is not int or number <= 0:
            raise ValueError("invalid Snapper target")
        return f"{config}:{number}"

    def acquire(
        self,
        config: str,
        number: int,
        snapshot_uuid: str,
        transfer_id: str,
        *,
        restore_cleanup: str | None = None,
    ) -> None:
        key = self._key(config, number)
        if not snapshot_uuid or not transfer_id:
            raise ValueError("pin requires immutable source UUID and transfer id")
        if restore_cleanup is not None and restore_cleanup not in (
            "timeline",
            "number",
            "empty-pre-post",
        ):
            raise ValueError("invalid native Snapper restore cleanup algorithm")
        with self._locked() as state:
            item = state["pins"].get(key)
            current = self.query(config, number)
            if current is None or current.uuid != snapshot_uuid:
                raise ValueError("cannot pin missing or different source UUID")
            if item is None:
                if restore_cleanup is not None and current.cleanup != "":
                    raise ValueError(
                        "a Harbor-created Snapper source must initially be cleanup-exempt"
                    )
                item = {
                    "uuid": snapshot_uuid,
                    "original_cleanup": restore_cleanup
                    if restore_cleanup is not None
                    else current.cleanup,
                    "leases": [transfer_id],
                }
                state["pins"][key] = item
                # Save pending lease before external native Snapper operation.
                self._save(state)
                if current.cleanup != "":
                    self.modify(config, number, "")
                actual = self.query(config, number)
                if (
                    actual is None
                    or actual.uuid != snapshot_uuid
                    or actual.cleanup != ""
                ):
                    raise RuntimeError("native Snapper cleanup pin was not confirmed")
                return
            if item["uuid"] != snapshot_uuid:
                raise ValueError("existing pin identity mismatch")
            if (
                restore_cleanup is not None
                and item["original_cleanup"] != restore_cleanup
            ):
                raise ValueError(
                    "Snapper cleanup restoration policy changed during pin"
                )
            if transfer_id in item["leases"]:
                return
            item["leases"].append(transfer_id)
            self._save(state)
            if current.cleanup != "":
                self.modify(config, number, "")

    def release(
        self, config: str, number: int, snapshot_uuid: str, transfer_id: str
    ) -> None:
        key = self._key(config, number)
        with self._locked() as state:
            item = state["pins"].get(key)
            if (
                item is None
                or item["uuid"] != snapshot_uuid
                or transfer_id not in item["leases"]
            ):
                raise ValueError("pin release identity or lease mismatch")
            item["leases"].remove(transfer_id)
            self._save(state)
            if item["leases"]:
                return
            current = self.query(config, number)
            if current is None or current.uuid != snapshot_uuid:
                # Keep orphan record for operator diagnostics; never modify
                # an unrelated new snapshot using the same Snapper number.
                return
            if current.cleanup == "":
                original = item["original_cleanup"]
                if original != "":
                    self.modify(config, number, original)
            # Do not override a cleanup policy modified by an external user.
            del state["pins"][key]
            self._save(state)

    def reconcile(self) -> dict[str, str]:
        statuses: dict[str, str] = {}
        with self._locked() as state:
            for key, item in list(state["pins"].items()):
                config, number_text = key.rsplit(":", 1)
                number = int(number_text)
                current = self.query(config, number)
                if current is None:
                    statuses[key] = "needs-source"
                elif current.uuid != item["uuid"]:
                    statuses[key] = "identity-mismatch"
                elif item["leases"]:
                    statuses[key] = (
                        "healthy" if current.cleanup == "" else "re-pin-needed"
                    )
                else:
                    if current.cleanup == "" and item["original_cleanup"]:
                        self.modify(config, number, item["original_cleanup"])
                    del state["pins"][key]
                    self._save(state)
                    statuses[key] = "stale-pin-restored"
        return statuses
