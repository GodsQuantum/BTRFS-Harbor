"""Exercise Harbor v2 itself, not just btrfs send/receive, on a temporary Btrfs disk.

This test deliberately creates its own disposable Btrfs subvolume and local
archive folder; it never references configured user paths, mounts or backups.
A new machine only needs the destination directory, not the original profile.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]

PROFILE_ID = "55555555-5555-4555-8555-555555555555"


def _harbor(*args: str, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env={**os.environ, "COLUMNS": "200"},
        check=False,
    )


def _assert_ok(command: subprocess.CompletedProcess[str]) -> str:
    assert command.returncode == 0, command.stdout + "\n" + command.stderr
    return command.stdout


def test_real_native_full_incremental_catalog_and_profile_free_staged_restore(
    btrfs_volume: Path, tmp_path: Path
) -> None:
    live = btrfs_volume / "live"
    subprocess.run(
        ["btrfs", "subvolume", "create", str(live)],
        capture_output=True,
        check=True,
    )
    payload = live / "document.txt"
    payload.write_text("First version\n")

    archive = tmp_path / "destination"
    archive.mkdir()
    journal = tmp_path / "state"
    first_name = "root.20261009T120000"
    second_name = "root.20261009T120001"

    def save(name: str) -> dict:
        output = _harbor(
            "raw",
            "checkpoint-v2",
            "start",
            "--source",
            str(live),
            "--source-mode",
            "create-native",
            "--target",
            str(archive),
            "--name",
            name,
            "--profile-id",
            PROFILE_ID,
            "--checkpoint-size-mib",
            "1",
            "--state-dir",
            str(journal),
            "--allow-local",
            "--experimental",
        )
        return json.loads(_assert_ok(output).strip().splitlines()[-1])

    first = save(first_name)
    assert first["status"] == "completed"
    payload.write_text("Second version\n")
    second = save(second_name)
    assert second["status"] == "completed"

    raw_list = _assert_ok(_harbor("raw", "list", str(archive), "--json"))
    catalog = json.loads(raw_list)
    assert {entry["name"] for entry in catalog} == {first_name, second_name}
    assert all(entry["checksum"]["value"] for entry in catalog)
    second_record = next(item for item in catalog if item["name"] == second_name)
    assert second_record["parent_name"] == first_name, second_record

    # This is the recovery scenario: a fresh installation sees only the
    # raw:// folder. The original Harbor profile and private state are absent.
    staging = btrfs_volume / "restore-fresh"
    staging.mkdir()
    args = ("restore", f"raw://{archive}", str(staging), "--snapshot", second_name)
    _assert_ok(_harbor(*args, "--dry-run"))
    _assert_ok(_harbor(*args, timeout=180))
    files = list(staging.rglob("document.txt"))
    assert files, list(staging.iterdir())
    assert any(path.read_text() == "Second version\n" for path in files)
    assert all(not path.is_symlink() for path in files)
