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


def test_nfs_server_disappears_while_sender_active(tmp_path: Path):
    """Real server outage mid-transfer, then recover from committed checkpoints.

    This runs exclusively on the disposable localhost NFS server created
    in nfs-fault-lab.yml; systemctl never targets Cloud9 or user machines.
    """
    test_mount = os.environ.get("HARBOR_TEST_NFS_TARGET")
    if not test_mount or not test_mount.startswith("/mnt/harbor-nfs-client"):
        pytest.fail("refusing server test outside isolated GitHub NFS mount")
    target = Path(test_mount) / "server-outage"
    assert not target.exists()
    target.mkdir(mode=0o700)

    with LoopbackBtrfs(
        size_mb=768, label="harbor-server-outage", base_dir=tmp_path
    ) as vol:
        live = vol / "source"
        subprocess.run(["btrfs", "subvolume", "create", str(live)], check=True)
        wanted = hashlib.sha256()
        with (live / "content.bin").open("wb") as output:
            for i in range(190):
                payload = hashlib.shake_256(i.to_bytes(4, "big")).digest(1024 * 1024)
                output.write(payload)
                wanted.update(payload)
        frozen = vol / "readonly"
        subprocess.run(
            ["btrfs", "subvolume", "snapshot", "-r", str(live), str(frozen)],
            check=True,
        )
        state = tmp_path / "control"
        sender = subprocess.Popen(
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
                "server-stopped",
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
        original = None
        manifest_path = None
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            entries = list(target.glob(".harbor-resume-*.json"))
            if entries:
                manifest_path = entries[0]
                try:
                    parsed = read_manifest(manifest_path)
                    if len(parsed.checkpoints) >= 5 and parsed.state != "completed":
                        original = parsed
                        break
                except (OSError, ValueError):
                    pass
            if sender.poll() is not None:
                break
            time.sleep(0.01)
        assert original is not None, "sender completed before outage could be injected"
        assert manifest_path is not None
        # Stop the NFS server without unmounting the client. Writes on the
        # established NFS mount must fail or wait; they must NOT fall back to
        # local filesystem storage.
        try:
            subprocess.run(
                ["systemctl", "stop", "nfs-server.service"], check=True, timeout=35
            )
            assert (
                subprocess.run(
                    ["systemctl", "is-active", "--quiet", "nfs-server.service"],
                    check=False,
                    timeout=5,
                ).returncode
                != 0
            )
            time.sleep(2)
        finally:
            if sender.poll() is None:
                os.killpg(sender.pid, signal.SIGKILL)
            sender.communicate(timeout=35)
            subprocess.run(
                ["systemctl", "start", "nfs-server.service"], check=True, timeout=50
            )

        assert (
            subprocess.run(
                ["findmnt", "-n", "-o", "FSTYPE", "-T", str(target)],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()
            == "nfs4"
        )

        resumed = _run(
            "raw",
            "checkpoint-v2",
            "resume",
            "--target",
            str(target),
            "--name",
            "server-stopped",
            "--transfer-id",
            original.transfer_id,
            "--state-dir",
            str(state),
            "--experimental",
        )
        assert resumed.returncode == 0, resumed.stdout + resumed.stderr
        recovered_manifest = read_manifest(manifest_path)
        assert recovered_manifest.state == "completed"
        assert (
            recovered_manifest.checkpoints[: len(original.checkpoints)]
            == original.checkpoints
        )
        staging = vol / "restore-after-server-loss"
        staging.mkdir()
        result = _run(
            "restore", f"raw://{target}", str(staging), "--snapshot", "server-stopped"
        )
        assert result.returncode == 0, result.stdout + result.stderr
        files = list(staging.rglob("content.bin"))
        assert len(files) == 1
        actual = hashlib.sha256()
        with files[0].open("rb") as inp:
            while chunk := inp.read(1024 * 1024):
                actual.update(chunk)
        assert actual.hexdigest() == wanted.hexdigest()
