"""Native Btrfs source validation and safe send subprocess for Harbor v2.

Only readonly local snapshots are eligible; arguments are passed as argv,
never through a shell. The live mount guard must be established separately.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tempfile
from pathlib import Path

from ..endpoint.mount_guard_v2 import MountIdentity
from .replay_v2 import SourceFingerprint


def inspect_readonly_btrfs_source(path: Path) -> SourceFingerprint:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ValueError("source must be a real absolute Btrfs directory")
    try:
        result = subprocess.run(
            ["btrfs", "subvolume", "show", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        readonly = subprocess.run(
            ["btrfs", "property", "get", "-ts", str(path), "ro"],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        raise ValueError(f"cannot inspect Btrfs source: {err}") from err
    if (
        result.returncode
        or readonly.returncode
        or not re.search(r"(?m)^\s*ro\s*=\s*true\s*$", readonly.stdout)
    ):
        raise ValueError("source is not a readable readonly Btrfs snapshot")
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if match := re.match(r"^\s*([^:]+):\s*(.*)$", line):
            fields[match.group(1).strip()] = match.group(2).strip()
    received = fields.get("Received UUID", "")
    uid = received if received and received != "-" else fields.get("UUID", "")
    if not uid or uid == "-":
        raise ValueError("source Btrfs UUID is missing")
    return SourceFingerprint(
        uuid=uid,
        parent_uuid=None,
        path=str(path),
        send_fingerprint="protocol=2",
        readonly=True,
    )


def destination_fingerprint(mount: MountIdentity, path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("backup destination must be absolute")
    stable = {
        "path": str(path.resolve()),
        "type": mount.fstype,
        "source": mount.source,
        "root": mount.root,
        "mountpoint": mount.mount_point,
    }
    return hashlib.sha256(
        json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def spawn_btrfs_send(
    source: Path, parent: Path | None = None
) -> subprocess.Popen[bytes]:
    if not source.is_absolute() or (parent is not None and not parent.is_absolute()):
        raise ValueError("Btrfs send requires absolute source and parent paths")
    argv = ["btrfs", "send", "--proto", "2"]
    if parent is not None:
        argv += ["-p", str(parent)]
    argv += [str(source)]
    # A PIPE not consumed concurrently can fill and deadlock btrfs send.
    # Anonymous temporary file lets the child write any amount of diagnostics
    # without blocking its stdout; the owning runner closes it after wait.
    stderr_log = tempfile.TemporaryFile(mode="w+b")
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=stderr_log,
            start_new_session=True,
            bufsize=0,
            close_fds=True,
        )
    except BaseException:
        stderr_log.close()
        raise
    setattr(process, "_harbor_stderr_log", stderr_log)
    return process
