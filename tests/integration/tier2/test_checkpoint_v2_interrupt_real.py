"""Fault-inject SIGKILL during an actual Harbor v2 Btrfs send on disposable media."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from btrfs_backup_ng.core.checkpoint_v2 import read_manifest
from .conftest import LoopbackBtrfs, requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]


@pytest.fixture
def large_btrfs_volume(tmp_path: Path):
    # Source + restored copy must both fit without ENOSPC.
    with LoopbackBtrfs(size_mb=512, label="harbor-crash", base_dir=tmp_path) as volume:
        yield volume


PROFILE_ID = "55555555-5555-4555-8555-555555555555"
PAYLOAD_MIB = 160


def _cli(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return result.stdout


@pytest.mark.parametrize("stop_percent", [10, 50, 90])
def test_sigkill_resume_recover_without_profile(
    large_btrfs_volume: Path, tmp_path: Path, stop_percent: int
) -> None:
    btrfs_volume = large_btrfs_volume
    live = btrfs_volume / "live"
    subprocess.run(["btrfs", "subvolume", "create", str(live)], check=True)
    digest = hashlib.sha256()
    # Incompressible, reproducible without holding the whole payload in RAM.
    with (live / "large.bin").open("wb") as output:
        for i in range(PAYLOAD_MIB):
            block = hashlib.shake_256(i.to_bytes(4, "big")).digest(1024 * 1024)
            output.write(block)
            digest.update(block)
    source = btrfs_volume / "frozen"
    subprocess.run(
        ["btrfs", "subvolume", "snapshot", "-r", str(live), str(source)], check=True
    )
    archive = tmp_path / "archive"
    archive.mkdir()
    state = tmp_path / "state"
    name = f"crash-{stop_percent}"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "btrfs_backup_ng",
            "raw",
            "checkpoint-v2",
            "start",
            "--source",
            str(source),
            "--target",
            str(archive),
            "--name",
            name,
            "--profile-id",
            PROFILE_ID,
            "--checkpoint-size-mib",
            "1",
            "--state-dir",
            str(state),
            "--allow-local",
            "--experimental",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    initial = checkpoint_path = None
    threshold = PAYLOAD_MIB * 1024 * 1024 * stop_percent // 100
    deadline = time.monotonic() + 90
    try:
        while time.monotonic() < deadline:
            manifests = list(archive.glob(".harbor-resume-*.json"))
            if manifests:
                checkpoint_path = manifests[0]
                try:
                    current = read_manifest(checkpoint_path)
                except (OSError, ValueError):
                    current = None
                if (
                    current
                    and sum(c.raw_length for c in current.checkpoints) >= threshold
                ):
                    initial = current
                    break
            if process.poll() is not None:
                break
            time.sleep(0.01)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate(timeout=20)
    assert initial is not None, (
        f"send exited before {stop_percent}%: {stdout[-1000:]} {stderr[-1000:]}"
    )
    assert checkpoint_path is not None
    assert initial.state != "completed"
    assert initial.checkpoints
    part = archive / f"{name}.btrfs.zst.{initial.transfer_id}.part"
    assert part.is_file()
    committed_bytes = sum(c.compressed_length for c in initial.checkpoints)
    assert committed_bytes > 0

    # Fresh CLI process: discard its old volatile profile and process state.
    output = _ok(
        _cli(
            "raw",
            "checkpoint-v2",
            "resume",
            "--target",
            str(archive),
            "--name",
            name,
            "--transfer-id",
            initial.transfer_id,
            "--state-dir",
            str(state),
            "--allow-local",
            "--experimental",
        )
    )
    assert json.loads(output.strip().splitlines()[-1])["status"] == "completed"
    completed = read_manifest(checkpoint_path)
    assert completed.state == "completed"
    assert completed.checkpoints[: len(initial.checkpoints)] == initial.checkpoints

    staging = btrfs_volume / "recovery"
    staging.mkdir()
    _ok(_cli("restore", f"raw://{archive}", str(staging), "--snapshot", name))
    recovered = list(staging.rglob("large.bin"))
    assert len(recovered) == 1, recovered
    restored_digest = hashlib.sha256()
    with recovered[0].open("rb") as stream:
        while block := stream.read(1024 * 1024):
            restored_digest.update(block)
    assert restored_digest.hexdigest() == digest.hexdigest()
