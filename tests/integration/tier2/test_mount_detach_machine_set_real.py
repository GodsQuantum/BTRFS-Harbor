"""Real disposable mount-detach test. Does not touch user mounts or disks.

This tests lost mount IDs and underlying-destination safety. It is NOT a
substitute for real NFS network loss, reconnect and authentication testing.
"""

from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

import pytest

from btrfs_backup_ng.cli.machine_set_v2 import _write
from btrfs_backup_ng.cli.checkpoint_v2_cmd import _open_guard

pytestmark = pytest.mark.tier2


def test_detached_live_mount_never_writes_underlying_path(tmp_path: Path):
    target = tmp_path / "emulated-network-mount"
    target.mkdir()
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "size=16m", "tmpfs", str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    mounted = True
    try:
        guard, fingerprint = _open_guard(target, allow_local=True)
        with guard:
            guard.validate_fd(guard.directory_fd)
            set_id = str(uuid.uuid4())
            data = {
                "set_id": set_id,
                "destination_fingerprint": fingerprint,
                "status": "pending",
                "coverage": "btrfs-only",
                "members": [],
            }
            # First write on verified mount succeeds.
            _write(target, data, allow_local=True)
            assert (target / f".harbor-machine-set-{set_id}.json").exists()
            subprocess.run(
                ["umount", "-l", str(target)],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            mounted = False
            # Previously pinned fd is no longer the directory at the path.
            with pytest.raises((RuntimeError, ValueError)):
                guard.validate_fd(guard.directory_fd)
            with pytest.raises((RuntimeError, ValueError)):
                _write(target, data, allow_local=True)
            assert not list(target.iterdir()), (
                "backing directory must stay empty after mount disappears"
            )
    finally:
        if mounted:
            subprocess.run(["umount", str(target)], check=True, timeout=20)
