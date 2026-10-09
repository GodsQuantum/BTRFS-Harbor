"""Actual two-subvolume Harbor v2 machine-set backup and destination-only recovery."""

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


def _run(*args: str, timeout: int = 180) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", "raw", "checkpoint-v2", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "COLUMNS": "200"},
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_nested_root_and_home_machine_set_recovers_from_destination_only(
    btrfs_volume: Path, tmp_path: Path
):
    root = btrfs_volume / "root"
    home = root / "home"
    subprocess.run(["btrfs", "subvolume", "create", str(root)], check=True)
    subprocess.run(["btrfs", "subvolume", "create", str(home)], check=True)
    (root / "system.txt").write_text("root payload\n")
    (home / "user.txt").write_text("home payload\n")

    archive = tmp_path / "archive"
    archive.mkdir()
    journal = tmp_path / "journal"
    outcome = _run(
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
    )
    assert outcome["status"] == "completed_btrfs_only"
    assert outcome["bootable"] is False
    assert len(outcome["members"]) == 2
    assert all(item["status"] == "completed" for item in outcome["members"])

    set_id = outcome["set_id"]
    assert (archive / f".harbor-machine-set-{set_id}.json").exists()
    status = _run(
        "set-status",
        "--target",
        str(archive),
        "--set-id",
        set_id,
        "--allow-local",
    )
    assert status["status"] == outcome["status"]

    staging = btrfs_volume / "staging"
    staging.mkdir()
    plan = _run(
        "set-restore",
        "--target",
        str(archive),
        "--set-id",
        set_id,
        "--staging",
        str(staging),
        "--allow-local",
        "--experimental",
    )
    assert plan["dry_run"] is True and len(plan["plan"]) == 2
    assert not list(staging.iterdir())

    actual = _run(
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
    assert actual["restored"] is True and actual["bootable"] is False
    assert any(f.read_text() == "root payload\n" for f in staging.rglob("system.txt"))
    assert any(f.read_text() == "home payload\n" for f in staging.rglob("user.txt"))

    # A subsequent machine point must choose an independently verified
    # incremental parent for BOTH nested Btrfs sources.
    (root / "system.txt").write_text("root payload v2\n")
    (home / "user.txt").write_text("home payload v2\n")
    incremental_machine = _run(
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
    )
    assert incremental_machine["status"] == "completed_btrfs_only"
    second_names = [m["archive"] for m in incremental_machine["members"]]
    first_names = [m["archive"] for m in outcome["members"]]
    assert second_names != first_names

    records = subprocess.run(
        [
            sys.executable,
            "-m",
            "btrfs_backup_ng",
            "raw",
            "list",
            str(archive),
            "--json",
        ],
        text=True,
        capture_output=True,
        check=True,
        timeout=90,
    )
    by_name = {item["name"]: item for item in json.loads(records.stdout)}
    for second, parent in zip(second_names, first_names, strict=True):
        assert by_name[second]["parent_name"] == parent

    final_stage = btrfs_volume / "incremental-fresh"
    final_stage.mkdir()
    recovered = _run(
        "set-restore",
        "--target",
        str(archive),
        "--set-id",
        incremental_machine["set_id"],
        "--staging",
        str(final_stage),
        "--allow-local",
        "--experimental",
        "--confirm",
    )
    assert recovered["restored"] is True
    assert any(
        f.read_text() == "root payload v2\n" for f in final_stage.rglob("system.txt")
    )
    assert any(
        f.read_text() == "home payload v2\n" for f in final_stage.rglob("user.txt")
    )


def test_machine_set_recovery_after_snapshot_creation_crash(
    btrfs_volume: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Kill between snapshot ioctl and UUID journal: the reserved name is reused."""
    import argparse
    from btrfs_backup_ng.cli import machine_set_v2 as machine

    live = btrfs_volume / "crash-source"
    subprocess.run(["btrfs", "subvolume", "create", str(live)], check=True)
    (live / "before-crash.txt").write_text("durable data before crash\n")
    archive = tmp_path / "backup"
    archive.mkdir()
    state = tmp_path / "state"

    actual = machine.create_native_snapshot
    generated: list[Path] = []

    def die_after_creation(source: Path, *, reserved_name: str | None = None):
        snapshot = actual(source, reserved_name=reserved_name)
        generated.append(snapshot.path)
        raise RuntimeError("synthetic process death AFTER snapshot ioctl")

    monkeypatch.setattr(machine, "create_native_snapshot", die_after_creation)
    with pytest.raises(RuntimeError, match="AFTER snapshot"):
        machine.execute_machine_set(
            argparse.Namespace(
                checkpoint_action="set-start",
                target=str(archive),
                profile_id=PROFILE_ID,
                source=[str(live)],
                allow_local=True,
                state_dir=str(state),
                experimental=True,
            )
        )
    assert len(generated) == 1
    catalogs = list(archive.glob(".harbor-machine-set-*.json"))
    assert len(catalogs) == 1
    pending = json.loads(catalogs[0].read_text())
    assert pending["members"][0]["snapshot_uuid"] is None
    assert pending["members"][0]["snapshot_path"] == str(generated[0])
    assert pending["members"][0]["status"] == "pending"

    # Emulate fresh process with no in-memory context.
    monkeypatch.undo()
    finished = _run(
        "set-resume",
        "--target",
        str(archive),
        "--set-id",
        pending["set_id"],
        "--allow-local",
        "--state-dir",
        str(state),
        "--experimental",
    )
    assert finished["status"] == "completed_btrfs_only"
    assert len(list((live / ".btrfs-harbor-snapshots").iterdir())) == 1
    completed = json.loads(catalogs[0].read_text())
    assert completed["members"][0]["snapshot_uuid"]

    recovery = btrfs_volume / "crash-recovery"
    recovery.mkdir()
    result = _run(
        "set-restore",
        "--target",
        str(archive),
        "--set-id",
        pending["set_id"],
        "--staging",
        str(recovery),
        "--allow-local",
        "--experimental",
        "--confirm",
    )
    assert result["restored"] is True
    assert any(
        file.read_text() == "durable data before crash\n"
        for file in recovery.rglob("before-crash.txt")
    )
