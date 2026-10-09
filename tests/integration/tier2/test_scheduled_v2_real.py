"""Scheduled native source uses the existing machine-set journal and v2 incrementals."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]

PROFILE_ID = "55555555-5555-4555-8555-555555555555"


def _run(*args: str) -> int:
    command = subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", "raw", "checkpoint-v2", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert command.returncode == 0, command.stdout + command.stderr
    return command.returncode


def test_schedule_native_restarts_without_creating_duplicate_archive(
    btrfs_volume: Path,
    tmp_path: Path,
):
    live = btrfs_volume / "scheduled"
    subprocess.run(["btrfs", "subvolume", "create", str(live)], check=True)
    (live / "version.txt").write_text("one")
    archive = tmp_path / "archive"
    archive.mkdir()
    state = tmp_path / "state"
    arguments = [
        "schedule-run",
        "--target",
        str(archive),
        "--profile-id",
        PROFILE_ID,
        "--source",
        str(live),
        "--allow-local",
        "--state-dir",
        str(state),
        "--experimental",
    ]

    assert _run(*arguments) == 0
    records = list(archive.glob(".harbor-machine-set-*.json"))
    assert len(records) == 1
    first = json.loads(records[0].read_text())
    assert first["status"] == "completed_btrfs_only"
    prior = first["members"][0]["archive"]
    first_id = first["members"][0]["transfer_id"]

    # The second invocation on the same source produces one verified
    # incremental, rather than a duplicate backup of the same source UUID.
    (live / "version.txt").write_text("two")
    assert _run(*arguments) == 0
    records = list(archive.glob(".harbor-machine-set-*.json"))
    assert len(records) == 2
    newer = [
        json.loads(record.read_text())
        for record in records
        if json.loads(record.read_text())["set_id"] != first["set_id"]
    ]
    assert len(newer) == 1
    second = newer[0]
    assert second["status"] == "completed_btrfs_only"
    current = second["members"][0]["archive"]
    assert current != prior
    assert second["members"][0]["transfer_id"] != first_id

    raw = subprocess.run(
        [
            sys.executable,
            "-m",
            "btrfs_backup_ng",
            "raw",
            "list",
            str(archive),
            "--json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=90,
    )
    by_name = {item["name"]: item for item in json.loads(raw.stdout)}
    assert by_name[current]["parent_name"] == prior
