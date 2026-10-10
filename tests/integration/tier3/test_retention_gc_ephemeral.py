"""Disposable GitHub-only GC safety/correctness and interrupted transactions.

Every deletion is limited to TemporaryDirectory fixtures allocated in the
ephemeral Github-hosted runner. NEVER execute on Cloud9 or real storage.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from btrfs_backup_ng.config import RetentionConfig
from btrfs_backup_ng.core.machine_retention_v2 import SetPoint, calculate_set_retention
from btrfs_backup_ng.core.machine_retention_gc_v2 import (
    active_gc,
    collect_verified_files,
    destination_gc_lock,
)
from btrfs_backup_ng.endpoint.mount_guard_v2 import MountGuard, capture_mount_identity

NOW = datetime(2026, 10, 10, tzinfo=timezone.utc)
SET_OLD = "11111111-1111-4111-8111-111111111111"
SET_NEW = "22222222-2222-4222-8222-222222222222"
SET_DEP = "33333333-3333-4333-8333-333333333333"


def payload(root: Path, name: str, content: bytes) -> None:
    (root / name).write_bytes(content)
    os.chmod(root / name, 0o600)


def test_parent_closure_before_disposable_gc(root: Path) -> None:
    sets = [
        SetPoint(SET_OLD, NOW, ("machine20261010T010000-11111111-000",), True),
        SetPoint(
            SET_DEP,
            NOW + timedelta(hours=1),
            ("machine20261010T020000-33333333-000",),
            True,
        ),
        SetPoint(
            SET_NEW,
            NOW + timedelta(hours=2),
            ("machine20261010T030000-22222222-000",),
            True,
        ),
    ]
    graph = {
        sets[0].archives[0]: None,
        sets[1].archives[0]: sets[0].archives[0],
        sets[2].archives[0]: None,
    }
    plan = calculate_set_retention(
        sets, graph, RetentionConfig(min="0d", keep=1), now=NOW + timedelta(days=5)
    )
    assert plan.keep == frozenset({SET_NEW})
    assert plan.eligible == frozenset({SET_OLD, SET_DEP})
    old = sets[0].archives[0]
    child = sets[1].archives[0]
    for archive in (old, child, sets[2].archives[0]):
        payload(root, archive + ".btrfs.zst", archive.encode())
        payload(root, archive + ".btrfs.zst.meta", b"sealed")
    foreign = root / "family-photos.txt"
    foreign.write_bytes(b"UNTOUCHED")
    to_delete = [
        name
        for archive in (old, child)
        for name in (archive + ".btrfs.zst", archive + ".btrfs.zst.meta")
    ]
    with MountGuard(root, capture_mount_identity(root)) as guard:
        with destination_gc_lock(guard, exclusive=True):
            with pytest.raises(RuntimeError, match="injected"):
                collect_verified_files(root, guard, to_delete, fail_after_stages=2)
            assert len(active_gc(guard)) == 1
            assert foreign.read_bytes() == b"UNTOUCHED"
            result = collect_verified_files(root, guard, None, resume=True)
            assert set(result["deleted"]) == set(to_delete)
            assert active_gc(guard) == []
    assert foreign.read_bytes() == b"UNTOUCHED"
    assert (root / (sets[2].archives[0] + ".btrfs.zst")).exists()
    assert not any((root / name).exists() for name in to_delete)


def test_corruption_symlink_foreign_naming_refuses_without_delete(root: Path) -> None:
    name = "machine20261010T010000-11111111-000.btrfs.zst"
    payload(root, name, b"genuine")
    outsider = root / "priceless.txt"
    outsider.write_bytes(b"safe")
    (root / "symlink").symlink_to(outsider)
    with MountGuard(root, capture_mount_identity(root)) as guard:
        with destination_gc_lock(guard, exclusive=True):
            for bad in (
                ["priceless.txt"],
                ["symlink"],
                ["../priceless.txt"],
                [name, name],
            ):
                with pytest.raises(ValueError):
                    collect_verified_files(root, guard, bad)
            assert not active_gc(guard)
    assert outsider.read_bytes() == b"safe"
    assert (root / name).read_bytes() == b"genuine"


def test_resume_refuses_new_foreign_or_modified_archive(root: Path) -> None:
    name = "machine20261010T010000-11111111-000.btrfs.zst"
    payload(root, name, b"old")
    with MountGuard(root, capture_mount_identity(root)) as guard:
        with destination_gc_lock(guard, exclusive=True):
            with pytest.raises(RuntimeError):
                collect_verified_files(root, guard, [name], fail_after_stages=1)
            foreign = root / "new-family-photos"
            foreign.write_bytes(b"never delete")
            with pytest.raises(ValueError, match="protected"):
                collect_verified_files(root, guard, None, resume=True)
            assert foreign.read_bytes() == b"never delete"
            foreign.unlink()
            assert collect_verified_files(root, guard, None, resume=True)["resumed"]
            assert not (root / name).exists()


def test_incomplete_chain_not_eligible_for_prune(root: Path) -> None:
    sets = [
        SetPoint(SET_OLD, NOW, ("machine20261010T010000-11111111-000",), True),
        SetPoint(
            SET_DEP,
            NOW + timedelta(hours=1),
            ("machine20261010T020000-33333333-000",),
            True,
        ),
    ]
    graph = {sets[0].archives[0]: None, sets[1].archives[0]: sets[0].archives[0]}
    plan = calculate_set_retention(
        sets, graph, RetentionConfig(min="0d", keep=1), now=NOW + timedelta(days=5)
    )
    assert plan.eligible == frozenset()
    assert plan.protected_dependencies == frozenset({SET_OLD})


@pytest.fixture
def root() -> Path:
    assert os.environ.get("GITHUB_ACTIONS") == "true", (
        "NO DESTRUCTIVE TESTS OUTSIDE GITHUB EPHEMERAL RUNNER"
    )
    with tempfile.TemporaryDirectory(prefix="harbor-disposable-gc-") as directory:
        path = Path(directory)
        assert path.stat().st_uid == os.getuid()
        yield path


def main() -> None:
    import pytest

    assert os.environ.get("GITHUB_ACTIONS") == "true"
    raise SystemExit(pytest.main(["-v", "-s", __file__]))


if __name__ == "__main__":
    main()
