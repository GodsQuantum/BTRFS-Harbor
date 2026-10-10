"""SSH adapter for already verified Harbor checkpoint-v2 machine sets.

One engine: Btrfs stream formation/checkpointing remains the normal v2 engine.
This adapter mirrors sealed bytes to a restricted remote receiver in durable
SHA256-checked frames, resuming at the server's committed byte position.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any

from .ssh_checkpoint_v2_receiver import MAX_CHUNK, safe_name

USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
HOST = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]{0,252}\Z")
IDENTITY = re.compile(r"[0-9a-f]{64}\Z")


class SSHMirror:
    def __init__(
        self,
        *,
        host: str,
        user: str,
        key: Path,
        known_hosts: Path,
        port: int = 22,
    ) -> None:
        if not USER.fullmatch(user) or not HOST.fullmatch(host):
            raise ValueError("SSH user/host are invalid (no options or shell syntax)")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("invalid SSH port")
        for path in (key, known_hosts):
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise ValueError(
                    "SSH key and pinned known_hosts must be regular absolute files"
                )
        self.argv = [
            "ssh",
            "-F",
            "/dev/null",
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "PasswordAuthentication=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ClearAllForwardings=yes",
            "-o",
            "ControlMaster=no",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=2",
            "-o",
            "ConnectTimeout=12",
            "-o",
            f"UserKnownHostsFile={known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-i",
            str(key),
            "-p",
            str(port),
            f"{user}@{host}",
            "harbor-v2",
        ]

    def request(self, payload: dict[str, Any], blob: bytes = b"") -> dict[str, Any]:
        raw = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode() + blob
        response = subprocess.run(
            self.argv,
            input=raw,
            capture_output=True,
            timeout=150,
            check=False,
        )
        if response.returncode != 0:
            # SSH failure must never be interpreted as an acknowledgement.
            raise RuntimeError(
                f"remote SSH receiver failed (code={response.returncode}); "
                "remote committed offset will be rechecked on retry"
            )
        if len(response.stdout) > 8192:
            raise ValueError("SSH receiver returned oversized status")
        data = json.loads(response.stdout)
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ValueError("invalid SSH receiver status")
        return data

    def mirror_file(self, local: Path) -> dict[str, Any]:
        name = safe_name(local.name)
        if local.is_symlink() or not local.is_file():
            raise ValueError("SSH mirror refuses nonregular local files")
        size = local.stat().st_size
        if not 0 < size <= (1 << 42):
            raise ValueError("invalid SSH mirror size")
        digest = hashlib.sha256()
        with local.open("rb") as source:
            while piece := source.read(1024 * 1024):
                digest.update(piece)
        full_sha = digest.hexdigest()
        base = {"version": 1, "name": name, "total": size, "sha256": full_sha}
        status = self.request({**base, "op": "status"})
        if status.get("name") != name:
            raise ValueError("remote SSH receiver identity mismatch")
        committed = status.get("committed")
        if type(committed) is not int or not 0 <= committed <= size:
            raise ValueError("remote SSH committed offset invalid")
        if status.get("complete") is True:
            if committed != size:
                raise ValueError("impossible remote final status")
            return {"name": name, "bytes": size, "already_present": True}
        with local.open("rb") as source:
            source.seek(committed)
            while committed < size:
                piece = source.read(min(MAX_CHUNK, size - committed))
                if not piece:
                    raise ValueError("local SSH source truncated while mirroring")
                reply = self.request(
                    {
                        **base,
                        "op": "put",
                        "offset": committed,
                        "size": len(piece),
                        "chunk_sha256": hashlib.sha256(piece).hexdigest(),
                    },
                    piece,
                )
                expected = committed + len(piece)
                if reply.get("name") != name or reply.get("committed") != expected:
                    raise ValueError(
                        "SSH receiver did not durably acknowledge new chunk"
                    )
                committed = expected
        reply = self.request({**base, "op": "finish"})
        if (
            reply.get("name") != name
            or reply.get("complete") is not True
            or reply.get("committed") != size
        ):
            raise ValueError("SSH final commit not acknowledged")
        return {"name": name, "bytes": size, "already_present": False}


def mirror_machine_set(
    local_root: Path, set_id: str, client: SSHMirror
) -> list[dict[str, Any]]:
    """Copy all verified Btrfs parents before the final machine-set catalog."""
    from .cli.machine_set_v2 import _archive_valid, _read
    from .core.boot_files_v2 import verify_boot_member
    from .endpoint.raw_metadata import discover_raw_snapshots
    from .endpoint.mount_guard_v2 import MountGuard, capture_mount_identity

    if str(local_root) != str(local_root.resolve()) or local_root.is_symlink():
        raise ValueError("SSH source directory must be real and nonsymlink")
    with MountGuard(local_root, capture_mount_identity(local_root)) as guard:
        data = _read(local_root, set_id)
        if data["status"] not in (
            "completed_btrfs_only",
            "completed_btrfs_and_boot_files",
        ):
            raise ValueError("machine set is incomplete, no SSH mirror permitted")
        snapshots = {row.name: row for row in discover_raw_snapshots(local_root)}
        ordered: list[str] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def walk(name: str) -> None:
            if name in visiting:
                raise ValueError("cycle in incremental chain")
            if name in visited:
                return
            visiting.add(name)
            row = snapshots.get(name)
            if row is None or not _archive_valid(local_root, name):
                raise ValueError(
                    "SSH mirror refuses missing or unverified parent stream"
                )
            if row.parent_name:
                ancestor = snapshots.get(row.parent_name)
                if ancestor is None or ancestor.source_uuid != row.parent_uuid:
                    raise ValueError(
                        "SSH mirror refuses mismatching incremental parent"
                    )
                walk(row.parent_name)
            elif row.parent_uuid:
                raise ValueError("orphaned remote incremental parent")
            visiting.remove(name)
            visited.add(name)
            ordered.append(name)

        for member in data["members"]:
            if member["status"] != "completed":
                raise ValueError("incomplete machine member")
            walk(member["archive"])
        files: list[Path] = []
        for name in ordered:
            files.extend(
                (
                    local_root / f"{name}.btrfs.zst",
                    local_root / f"{name}.btrfs.zst.meta",
                )
            )
        for member in data.get("boot_members", []):
            if member.get("status") != "completed" or not verify_boot_member(
                local_root, member
            ):
                raise ValueError("SSH backup boot partition incomplete or corrupt")
            files.append(local_root / safe_name(member["archive"]))
        # Publish catalog LAST, after all Btrfs parents and boot partitions.
        files.append(local_root / f".harbor-machine-set-{set_id}.json")
        results = []
        for path in files:
            guard.validate_fd(guard.directory_fd)
            results.append(client.mirror_file(path))
        guard.validate_fd(guard.directory_fd)
    return results
