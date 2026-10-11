"""Real Btrfs bind-mounted subdirectory cannot impersonate a subvolume root.

Runs exclusively on disposable loopback Btrfs volumes in CI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from btrfs_backup_ng.core import machine_inventory_v2 as inventory
from btrfs_backup_ng.detection.models import DetectedSubvolume, DetectionResult

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


def _btrfs_id(root: Path) -> int:
    import re

    result = subprocess.run(
        ["btrfs", "subvolume", "show", str(root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    match = re.search(r"(?m)^\s*Subvolume ID:\s*(\d+)\s*$", result.stdout)
    assert match is not None, result.stdout
    return int(match.group(1))


def test_nested_subvolume_without_separate_mount_is_not_omitted(
    btrfs_volume: Path, tmp_path: Path, monkeypatch
):
    source = btrfs_volume / "real-system-root"
    nested = source / "var" / "lib" / "valuable"
    source.mkdir()
    # A normal nested directory is NOT snapshotted independently.
    (source / "var" / "lib").mkdir(parents=True)
    subprocess.run(["btrfs", "subvolume", "create", str(nested)], check=True)
    (nested / "user-data.txt").write_text("important nested files")
    id_parent = 5
    id_nested = _btrfs_id(nested)
    fake_mounts = f"/dev/test-btrfs {btrfs_volume} btrfs rw,subvolid=5,subvol=/ 0 0\n"
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(fake_mounts)
    report = DetectionResult(
        subvolumes=[
            DetectedSubvolume(
                id=id_parent,
                device="/dev/test-btrfs",
                path="/",
                mount_point=str(btrfs_volume),
            ),
            DetectedSubvolume(
                id=id_nested,
                device="/dev/test-btrfs",
                path="/real-system-root/var/lib/valuable",
                mount_point=None,
            ),
        ]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kw: report)
    found = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert found["inventory_complete"]
    assert {
        row["mount_point"] for row in found["sources"] if row["included_by_default"]
    } == {str(btrfs_volume), str(nested)}
    assert inventory._is_real_subvolume_root(str(nested))


def test_real_swap_only_btrfs_subvolume_is_not_snapshotted(
    btrfs_volume: Path, tmp_path: Path
):
    from btrfs_backup_ng.cli.machine_set_v2 import _exclude_verified_volatile_swap

    swap = btrfs_volume / "separate-swap-subvolume"
    subprocess.run(
        ["btrfs", "subvolume", "create", str(swap)],
        check=True,
        capture_output=True,
        text=True,
    )
    (swap / "swapfile").write_bytes(b"synthetic-swap-test-file")
    swaps = tmp_path / "proc-swaps"
    swaps.write_text(
        "Filename Type Size Used Priority\n"
        + str(swap / "swapfile")
        + " file 4096 0 -2\n"
    )
    fstab = tmp_path / "fstab"
    fstab.write_text("")
    keep, omitted = _exclude_verified_volatile_swap(
        [str(swap)], swaps_path=swaps, fstab_path=fstab
    )
    assert keep == []
    assert [item["reason"] for item in omitted] == ["verified_volatile_swap_only"]
    assert omitted[0]["mount_point"] == str(swap)
    assert (swap / "swapfile").exists()
    (swap / "critical-data.txt").write_text("do not omit")
    keep, omitted = _exclude_verified_volatile_swap(
        [str(swap)], swaps_path=swaps, fstab_path=fstab
    )
    assert keep == [str(swap)]
    assert omitted == []
