"""Kill a *multi-volume* set while member #2 streams; resume same set.

Distinct from single-source v2 SIGKILL tests: member #1 must NOT be resent.
All Btrfs sources and restore targets are disposable loopback images.
"""

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

PROFILE_ID = "55555555-5555-4555-8555-555555555555"


def _run(*args: str, timeout: int = 300) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", "raw", "checkpoint-v2", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_killed_second_member_restores_entire_group(tmp_path: Path):
    with LoopbackBtrfs(
        size_mb=1100, label="harbor-group-crash", base_dir=tmp_path
    ) as vol:
        root, home = vol / "system", vol / "system" / "home"
        subprocess.run(["btrfs", "subvolume", "create", str(root)], check=True)
        subprocess.run(["btrfs", "subvolume", "create", str(home)], check=True)
        (root / "root.txt").write_text("ROOT\n")
        digest = hashlib.sha256()
        with (home / "large.bin").open("wb") as out:
            for i in range(270):
                chunk = hashlib.shake_256(i.to_bytes(4, "big")).digest(1024 * 1024)
                out.write(chunk)
                digest.update(chunk)

        archive = tmp_path / "dest"
        archive.mkdir()
        journal = tmp_path / "private"
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "btrfs_backup_ng",
                "raw",
                "checkpoint-v2",
                "set-start",
                "--target",
                str(archive),
                "--profile-id",
                PROFILE_ID,
                "--source",
                str(root),
                "--source",
                str(home),
                "--allow-local",
                "--state-dir",
                str(journal),
                "--experimental",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        partial = None
        catalog = None
        deadline = time.monotonic() + 120
        try:
            while time.monotonic() < deadline:
                catalogs = list(archive.glob(".harbor-machine-set-*.json"))
                if catalogs:
                    catalog = json.loads(catalogs[0].read_text())
                    if catalog["members"][0]["status"] == "completed":
                        candidates = list(archive.glob(".harbor-resume-*.json"))
                        for path in candidates:
                            try:
                                item = read_manifest(path)
                                if (
                                    item.identity["source_path"]
                                    == catalog["members"][1]["snapshot_path"]
                                    and len(item.checkpoints) >= 1
                                    and item.state != "completed"
                                ):
                                    partial = item
                                    break
                            except (OSError, ValueError, KeyError):
                                pass
                    if partial:
                        break
                if child.poll() is not None:
                    break
                time.sleep(0.02)
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate(timeout=30)
        assert catalog is not None and partial is not None, (
            stdout[-1000:],
            stderr[-1000:],
        )
        assert catalog["members"][0]["status"] == "completed"
        prior_first = catalog["members"][0]["transfer_id"]
        assert prior_first is not None
        set_id = catalog["set_id"]

        finished = _run(
            "set-resume",
            "--target",
            str(archive),
            "--set-id",
            set_id,
            "--allow-local",
            "--state-dir",
            str(journal),
            "--experimental",
        )
        assert finished["status"] == "completed_btrfs_only"
        final = json.loads((archive / f".harbor-machine-set-{set_id}.json").read_text())
        assert final["members"][0]["transfer_id"] == prior_first
        assert all(m["status"] == "completed" for m in final["members"])

        staging = vol / "recovery"
        staging.mkdir()
        recovered = _run(
            "set-restore",
            "--target",
            str(archive),
            "--set-id",
            set_id,
            "--staging",
            str(staging),
            "--allow-local",
            "--experimental",
            "--confirm",
        )
        assert recovered["restored"]
        assert [p.read_text() for p in staging.rglob("root.txt")] == ["ROOT\n"]
        files = list(staging.rglob("large.bin"))
        assert len(files) == 1
        check = hashlib.sha256()
        with files[0].open("rb") as stream:
            while block := stream.read(1024 * 1024):
                check.update(block)
        assert check.hexdigest() == digest.hexdigest()
