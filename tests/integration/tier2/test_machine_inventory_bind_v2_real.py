"""Real Btrfs bind-mounted subdirectory cannot impersonate a subvolume root.

Runs exclusively on disposable loopback Btrfs volumes in CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from btrfs_backup_ng.core import machine_inventory_v2 as inventory

from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]


def _mount_bind(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["mount", "--bind", str(source), str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_real_btrfs_subvolume_root_and_bind_subdirectory_are_distinct(
    btrfs_volume: Path, tmp_path: Path
):
    source = btrfs_volume / "system"
    subprocess.run(
        ["btrfs", "subvolume", "create", str(source)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    (source / "fonts").mkdir()
    (source / "fonts" / "real-data.txt").write_text("actual content")
    true_mount = tmp_path / "home-root"
    alias = tmp_path / "fonts-bind"
    _mount_bind(source, true_mount)
    try:
        _mount_bind(source / "fonts", alias)
        try:
            assert inventory._is_real_subvolume_root(str(true_mount))
            assert not inventory._is_real_subvolume_root(str(alias))
            # Mount metadata retains the parent subvolume id for a bind of a
            # child directory. Only the real mounted root is valid for send.
            rows = inventory.parse_proc_mounts(exclude_removable=False)
            true_rows = [m for m in rows if m.mount_point == str(true_mount)]
            alias_rows = [m for m in rows if m.mount_point == str(alias)]
            assert len(true_rows) == len(alias_rows) == 1
            assert true_rows[0].subvol_id == alias_rows[0].subvol_id
            assert true_rows[0].device == alias_rows[0].device
        finally:
            subprocess.run(
                ["umount", str(alias)], check=True, capture_output=True, timeout=20
            )
    finally:
        subprocess.run(
            ["umount", str(true_mount)], check=True, capture_output=True, timeout=20
        )
