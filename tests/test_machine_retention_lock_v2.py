"""Destination-wide reader/writer exclusion: no live machine needed."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from btrfs_backup_ng.cli import machine_set_v2, checkpoint_v2_cmd
from btrfs_backup_ng.core.machine_retention_gc_v2 import destination_gc_lock
from btrfs_backup_ng.endpoint.mount_guard_v2 import MountGuard, capture_mount_identity


def _exclusive_refuses(root: Path) -> None:
    with MountGuard(root, capture_mount_identity(root)) as other:
        with pytest.raises(BlockingIOError):
            with destination_gc_lock(other, exclusive=True):
                pytest.fail("GC overlapped active v2 operation")


@pytest.mark.parametrize(
    "action",
    [
        "set-start",
        "set-resume",
        "set-restore",
        "set-rear-recover",
        "set-rear-copy",
        "set-rescue-iso",
        "set-retention-plan",
        "set-list",
        "set-status",
    ],
)
def test_machine_lease_protects_entire_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    from btrfs_backup_ng.cli.machine_set_v2 import _execute_machine_set_unlocked

    assert _execute_machine_set_unlocked is not None

    def fake_unlocked(args: argparse.Namespace) -> int:
        _exclusive_refuses(tmp_path)
        return 43

    # A modern archive has the destination-wide lock created by its first
    # writer. Foreign legacy read-only archives without it cannot be pruned.
    (tmp_path / ".harbor-retention-v2.lock").touch()
    monkeypatch.setattr(machine_set_v2, "_execute_machine_set_unlocked", fake_unlocked)
    result = machine_set_v2.execute_machine_set(
        argparse.Namespace(checkpoint_action=action, target=str(tmp_path))
    )
    assert result == 43


@pytest.mark.parametrize(
    "action",
    ["start", "resume", "discard", "ssh-mirror", "schedule-run", "list", "status"],
)
def test_single_archive_and_mirror_lease_protects_entire_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    def fake_execute(args: argparse.Namespace) -> int:
        _exclusive_refuses(tmp_path)
        return 13

    (tmp_path / ".harbor-retention-v2.lock").touch()
    monkeypatch.setattr(checkpoint_v2_cmd, "_execute", fake_execute)
    result = checkpoint_v2_cmd.execute_checkpoint_v2(
        argparse.Namespace(checkpoint_action=action, target=str(tmp_path))
    )
    assert result == 13


def test_readonly_catalog_on_legacy_destination_does_not_create_lock(
    tmp_path: Path,
) -> None:
    with MountGuard(tmp_path, capture_mount_identity(tmp_path)) as guard:
        with destination_gc_lock(guard, exclusive=False, read_only=True):
            assert not (tmp_path / ".harbor-retention-v2.lock").exists()


def test_shared_reader_blocks_gc_and_gc_blocks_new_readers(tmp_path: Path) -> None:
    with MountGuard(tmp_path, capture_mount_identity(tmp_path)) as guard:
        with destination_gc_lock(guard, exclusive=False):
            _exclusive_refuses(tmp_path)
    with MountGuard(tmp_path, capture_mount_identity(tmp_path)) as guard:
        with destination_gc_lock(guard, exclusive=True):
            with MountGuard(tmp_path, capture_mount_identity(tmp_path)) as other:
                with pytest.raises(BlockingIOError):
                    with destination_gc_lock(other, exclusive=False, read_only=True):
                        pytest.fail("reader overlapped GC")


def test_crashed_transaction_refuses_new_jobs(tmp_path: Path) -> None:
    (tmp_path / ".harbor-retention-v2-aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa").mkdir()
    with MountGuard(tmp_path, capture_mount_identity(tmp_path)) as guard:
        with pytest.raises(ValueError, match="unfinished retention"):
            with destination_gc_lock(guard, exclusive=False):
                pytest.fail("accepted incompletely pruned destination")
