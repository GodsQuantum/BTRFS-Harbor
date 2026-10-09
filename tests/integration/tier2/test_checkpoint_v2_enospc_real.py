"""Actual ENOSPC during Btrfs v2 send; grow disposable target, resume, verify."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from btrfs_backup_ng.core.checkpoint_v2 import read_manifest
from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]

PROFILE_ID = "55555555-5555-4555-8555-555555555555"


def run(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_tmpfs_enospc_resume_preserves_committed_frames(
    btrfs_volume: Path, tmp_path: Path
):
    source = btrfs_volume / "enospc-source"
    subprocess.run(["btrfs", "subvolume", "create", str(source)], check=True)
    digest = hashlib.sha256()
    with (source / "payload.bin").open("wb") as out:
        for i in range(64):
            data = hashlib.shake_256(i.to_bytes(4, "big")).digest(1024 * 1024)
            out.write(data)
            digest.update(data)
    snapshot = btrfs_volume / "enospc-frozen"
    subprocess.run(
        ["btrfs", "subvolume", "snapshot", "-r", str(source), str(snapshot)],
        check=True,
    )
    target = tmp_path / "8MiB-target"
    target.mkdir()
    subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "size=8m", "tmpfs", str(target)],
        check=True,
        timeout=20,
    )
    try:
        private_state = tmp_path / "control-state"
        initial = run(
            "raw",
            "checkpoint-v2",
            "start",
            "--source",
            str(snapshot),
            "--target",
            str(target),
            "--name",
            "enospc-case",
            "--profile-id",
            PROFILE_ID,
            "--checkpoint-size-mib",
            "1",
            "--state-dir",
            str(private_state),
            "--allow-local",
            "--experimental",
        )
        assert initial.returncode != 0, "8MiB disk cannot contain 64MiB random send"
        journal = list(target.glob(".harbor-resume-*.json"))
        assert len(journal) == 1
        partial = read_manifest(journal[0])
        assert partial.checkpoints, "must commit at least one frame before ENOSPC"

        # Resize SAME disposable tmpfs mount without dropping committed data.
        subprocess.run(
            ["mount", "-o", "remount,size=192m", str(target)],
            check=True,
            timeout=20,
        )
        resumed = run(
            "raw",
            "checkpoint-v2",
            "resume",
            "--target",
            str(target),
            "--name",
            "enospc-case",
            "--transfer-id",
            partial.transfer_id,
            "--state-dir",
            str(private_state),
            "--allow-local",
            "--experimental",
        )
        assert resumed.returncode == 0, resumed.stderr
        assert (
            json.loads(resumed.stdout.strip().splitlines()[-1])["status"] == "completed"
        )
        final = read_manifest(journal[0])
        assert final.state == "completed"
        assert final.checkpoints[: len(partial.checkpoints)] == partial.checkpoints

        restored = btrfs_volume / "enospc-restored"
        restored.mkdir()
        result = run(
            "restore", f"raw://{target}", str(restored), "--snapshot", "enospc-case"
        )
        assert result.returncode == 0, result.stderr
        files = list(restored.rglob("payload.bin"))
        assert len(files) == 1
        actual = hashlib.sha256()
        with files[0].open("rb") as input_stream:
            while block := input_stream.read(1024 * 1024):
                actual.update(block)
        assert actual.hexdigest() == digest.hexdigest()
    finally:
        subprocess.run(["umount", str(target)], check=True, timeout=30)
