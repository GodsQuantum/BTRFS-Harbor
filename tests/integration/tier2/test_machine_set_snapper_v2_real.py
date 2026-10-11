"""Real Btrfs/Snapper reuse in disposable CI: no new Harbor snapshots."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess

import pytest

from btrfs_backup_ng.cli import machine_set_v2 as machine
from btrfs_backup_ng.cli import checkpoint_v2_cmd as checkpoint
from btrfs_backup_ng.snapper.scanner import SnapperScanner
from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]
PROFILE = "55555555-5555-4555-8555-555555555555"


def test_readonly_snapper_latest_used_by_complete_machine_set(
    btrfs_volume: Path, tmp_path: Path, monkeypatch
):
    root = btrfs_volume / "system-root"
    subprocess.run(["btrfs", "subvolume", "create", str(root)], check=True)
    (root / "source.txt").write_text("frozen historical Snapper data\n")
    slot = root / ".snapshots" / "7"
    slot.mkdir(parents=True)
    snap = slot / "snapshot"
    subprocess.run(
        ["btrfs", "subvolume", "snapshot", "-r", str(root), str(snap)], check=True
    )
    (slot / "info.xml").write_text(
        '<?xml version="1.0"?>\n'
        "<snapshot><type>single</type><num>7</num>"
        "<date>2026-10-11 10:00:00</date>"
        "<description>Snapper system state</description>"
        "<cleanup></cleanup></snapshot>"
    )
    # Deliberately modify live system AFTER taking the Snapper snapshot:
    # Snapper provider MUST back up historical content, never current state.
    (root / "source.txt").write_text("newer LIVE changes not in Snapper\n")

    cfg = tmp_path / "snapper-config"
    cfg.mkdir()
    (cfg / "root").write_text(f'SUBVOLUME="{root}"\nFSTYPE="btrfs"\n')

    def scanner():
        return SnapperScanner(configs_dir=cfg, use_snapper_command=False)

    monkeypatch.setattr(machine, "SnapperScanner", scanner)
    monkeypatch.setattr(checkpoint, "SnapperScanner", scanner)

    monkeypatch.setattr(
        machine,
        "_sources_from_inventory",
        lambda: (
            [str(root)],
            {
                "inventory_complete": True,
                "sources": [{"mount_point": str(root), "included_by_default": True}],
            },
        ),
    )
    monkeypatch.setattr(machine, "_mount_rows", lambda _contents: [])
    monkeypatch.setattr(machine, "snapshot_boot_layout", lambda: None)
    monkeypatch.setattr(
        machine, "assert_configured_boot_mounts_present", lambda *_args: None
    )

    archive = tmp_path / "backups"
    archive.mkdir()
    journal = tmp_path / "journal"
    args = argparse.Namespace(
        checkpoint_action="set-start",
        target=str(archive),
        profile_id=PROFILE,
        source=None,
        snapshot_provider="snapper",
        allow_local=True,
        state_dir=str(journal),
        experimental=True,
        max_incremental_depth=7,
    )
    assert machine.execute_machine_set(args) == 0
    records = list(archive.glob(".harbor-machine-set-*.json"))
    assert len(records) == 1
    import json

    catalog = json.loads(records[0].read_text())
    assert catalog["snapshot_provider"] == "snapper"
    assert catalog["status"] == "completed_btrfs_only"
    assert catalog["members"][0]["snapper_number"] == 7
    assert catalog["members"][0]["snapshot_path"] == str(snap)
    assert not (root / ".btrfs-harbor-snapshots").exists()
    assert snap.is_dir()
    assert (root / "source.txt").read_text() == "newer LIVE changes not in Snapper\n"

    staging = btrfs_volume / "restore-on-new-machine"
    staging.mkdir()
    restored = argparse.Namespace(
        checkpoint_action="set-restore",
        target=str(archive),
        set_id=catalog["set_id"],
        staging=str(staging),
        allow_local=True,
        experimental=True,
        confirm=True,
    )
    assert machine.execute_machine_set(restored) == 0
    assert any(
        f.read_text() == "frozen historical Snapper data\n"
        for f in staging.rglob("source.txt")
    )
