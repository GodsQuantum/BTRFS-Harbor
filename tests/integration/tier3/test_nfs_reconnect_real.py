"""Dedicated remote-NFS acceptance: SIGKILL, actual NFS remount and replay.

Run ONLY with HARBOR_TEST_NFS_TARGET mounted from an isolated disposable
server. Separate CI job is mandatory; no machine/user mounts are modified.
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from btrfs_backup_ng.core.checkpoint_v2 import read_manifest
from tests.integration.tier2.conftest import LoopbackBtrfs

pytestmark = pytest.mark.tier3

PROFILE_ID = "55555555-5555-4555-8555-555555555555"


def _run(*args: str, timeout: int = 240) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def test_real_nfs_sigkill_remount_resume_no_retransmit(tmp_path: Path):
    target_env = os.environ.get("HARBOR_TEST_NFS_TARGET")
    if not target_env:
        pytest.fail("HARBOR_TEST_NFS_TARGET must name a disposable NFS test mount")
    target = Path(target_env)
    info = subprocess.run(
        ["findmnt", "-n", "-o", "FSTYPE", "-T", str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert info.stdout.strip() in ("nfs", "nfs4"), info.stdout
    assert not list(target.iterdir()), "dedicated NFS export MUST be empty"

    with LoopbackBtrfs(size_mb=512, label="harbor-nfs", base_dir=tmp_path) as vol:
        source = vol / "source"
        subprocess.run(["btrfs", "subvolume", "create", str(source)], check=True)
        digest = hashlib.sha256()
        with (source / "payload.bin").open("wb") as output:
            for i in range(160):
                data = hashlib.shake_256(i.to_bytes(4, "big")).digest(1024 * 1024)
                output.write(data)
                digest.update(data)
        frozen = vol / "snapshot"
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", "-r", str(source), str(frozen)],
            check=True,
        )
        state = tmp_path / "private-state"
        name = "nfs-race"
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "btrfs_backup_ng",
                "raw",
                "checkpoint-v2",
                "start",
                "--source",
                str(frozen),
                "--target",
                str(target),
                "--name",
                name,
                "--profile-id",
                PROFILE_ID,
                "--checkpoint-size-mib",
                "1",
                "--state-dir",
                str(state),
                "--experimental",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        early = None
        manifest = None
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                paths = list(target.glob(".harbor-resume-*.json"))
                if paths:
                    manifest = paths[0]
                    try:
                        seen = read_manifest(manifest)
                        if len(seen.checkpoints) >= 5 and seen.state != "completed":
                            early = seen
                            break
                    except (OSError, ValueError):
                        pass
                if process.poll() is not None:
                    break
                time.sleep(0.02)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate(timeout=30)
        assert early is not None, stdout[-2000:] + stderr[-2000:]
        assert manifest is not None
        assert early.checkpoints

        # Detach the actual NFS client and remount the SAME server/export
        # (new kernel mount ID). Never permit writes to the backing directory.
        subprocess.run(["umount", "-l", str(target)], check=True, timeout=40)
        assert not list(target.iterdir()), "destination fallback must stay EMPTY"
        subprocess.run(
            [
                "mount",
                "-t",
                "nfs4",
                "-o",
                "vers=4.1,soft,timeo=50,retrans=1",
                "127.0.0.1:/",
                str(target),
            ],
            check=True,
            timeout=40,
        )

        resumed = _run(
            "raw",
            "checkpoint-v2",
            "resume",
            "--target",
            str(target),
            "--name",
            name,
            "--transfer-id",
            early.transfer_id,
            "--state-dir",
            str(state),
            "--experimental",
        )
        assert resumed.returncode == 0, resumed.stderr
        done = read_manifest(manifest)
        assert done.state == "completed"
        assert done.checkpoints[: len(early.checkpoints)] == early.checkpoints

        restore = vol / "recovery"
        restore.mkdir()
        recovered = _run("restore", f"raw://{target}", str(restore), "--snapshot", name)
        assert recovered.returncode == 0, recovered.stderr
        files = list(restore.rglob("payload.bin"))
        assert len(files) == 1
        restored = hashlib.sha256()
        with files[0].open("rb") as source_stream:
            while block := source_stream.read(1024 * 1024):
                restored.update(block)
        assert restored.hexdigest() == digest.hexdigest()
