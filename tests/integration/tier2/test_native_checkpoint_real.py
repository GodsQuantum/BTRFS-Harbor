"""Real root/Btrfs integration for Harbor-owned readonly snapshots.

The upstream tier2 fixture creates an isolated temporary loopback Btrfs
filesystem; no original system snapshots, mounts or backups are used.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from btrfs_backup_ng.core.native_snapshots import (
    create_native_snapshot,
    find_native_snapshot,
    list_native_snapshots,
)

from .conftest import requires_btrfs


def command(*args: str) -> None:
    subprocess.run(list(args), check=True, capture_output=True, text=True, timeout=60)


@pytest.mark.tier2
@requires_btrfs
def test_real_native_provider_full_then_incremental_restore(
    btrfs_volume: Path, tmp_path: Path
) -> None:
    source = btrfs_volume / "live"
    command("btrfs", "subvolume", "create", str(source))
    (source / "document.txt").write_text("First version\n")
    first = create_native_snapshot(source)
    assert find_native_snapshot(source, first.name).uuid == first.uuid
    assert first.path.parent == source / ".btrfs-harbor-snapshots"
    (source / "document.txt").write_text("Second version\n")
    second = create_native_snapshot(source)
    assert [item.name for item in list_native_snapshots(source)][:2] == [
        second.name,
        first.name,
    ]
    full = tmp_path / "full.btrfs"
    delta = tmp_path / "incremental.btrfs"
    command("btrfs", "send", "-f", str(full), str(first.path))
    command(
        "btrfs",
        "send",
        "-p",
        str(first.path),
        "-f",
        str(delta),
        str(second.path),
    )
    recovery = btrfs_volume / "staged-restore"
    recovery.mkdir()
    command("btrfs", "receive", "-f", str(full), str(recovery))
    command("btrfs", "receive", "-f", str(delta), str(recovery))
    assert (recovery / first.name / "document.txt").read_text() == "First version\n"
    assert (recovery / second.name / "document.txt").read_text() == "Second version\n"
