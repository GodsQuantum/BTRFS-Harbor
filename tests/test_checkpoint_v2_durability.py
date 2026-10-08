"""Crash boundary: on-disk checkpoint prefix wins over uncommitted file tails."""

import importlib
import os
import stat
from dataclasses import replace

import pytest


def api():
    return importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")


def good_manifest():
    m = api()
    return m.ResumeManifest(
        schema_version=2,
        transfer_id="3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        identity={
            "profile_id": "profile-11",
            "source_volume": "/",
            "source_uuid": "b0cbeb30-9917-44d8-9f27-1ed967f91a2d",
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "nfs:server:/export/backup",
            "kernel_release": "6.12.0",
            "btrfs_progs_version": "7.1",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
        checkpoint_size=5,
        state="paused",
        checkpoints=(
            m.Checkpoint(
                0, 0, 5, 0, 4, "a" * 64, "b" * 64, "2026-10-08T20:00:00+00:00"
            ),
            m.Checkpoint(
                1, 5, 3, 4, 7, "c" * 64, "d" * 64, "2026-10-08T20:01:00+00:00"
            ),
        ),
    )


def first_checkpoint_only():
    m = good_manifest()
    return replace(m, checkpoints=m.checkpoints[:1])


def test_atomic_commit_orders_file_sync_rename_and_directory_sync(
    tmp_path, monkeypatch
):
    m = api()
    events = []
    original_fsync = os.fsync
    original_replace = os.replace

    def tracked_fsync(fd):
        mode = os.fstat(fd).st_mode
        events.append("dir_fsync" if stat.S_ISDIR(mode) else "file_fsync")
        return original_fsync(fd)

    def tracked_replace(src, dst, *args, **kwargs):
        events.append("rename")
        return original_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(m.os, "fsync", tracked_fsync)
    monkeypatch.setattr(m.os, "replace", tracked_replace)
    manifest_path = tmp_path / ".harbor-resume.json"
    m.commit_manifest(
        manifest_path,
        first_checkpoint_only(),
        validate_destination=lambda: events.append("guard"),
    )
    assert events == ["guard", "file_fsync", "guard", "rename", "dir_fsync"]
    assert m.parse_manifest(manifest_path.read_bytes()) == first_checkpoint_only()
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_manifest_rename_preserves_old_commit_and_cleans_temp(
    tmp_path, monkeypatch
):
    m = api()
    manifest_path = tmp_path / ".harbor-resume.json"
    m.commit_manifest(
        manifest_path, first_checkpoint_only(), validate_destination=lambda: None
    )
    before = manifest_path.read_bytes()

    def refuse_rename(*args, **kwargs):
        raise OSError("injected pre-rename crash")

    monkeypatch.setattr(m.os, "replace", refuse_rename)
    with pytest.raises(OSError, match="injected"):
        m.commit_manifest(
            manifest_path, good_manifest(), validate_destination=lambda: None
        )
    assert manifest_path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_repair_discards_only_tail_after_crash(tmp_path):
    m = api()
    part = tmp_path / "snapshot.btrfs.zst.transfer.part"
    part.write_bytes(b"abcdNOT_COMMITTED")
    guarded = []

    result = m.reconcile_part(
        part, first_checkpoint_only(), validate_destination=lambda: guarded.append(True)
    )
    assert result == 4
    assert part.read_bytes() == b"abcd"
    assert len(guarded) >= 2


def test_repair_rejects_part_shorter_than_durable_manifest(tmp_path):
    m = api()
    part = tmp_path / "snapshot.btrfs.zst.transfer.part"
    part.write_bytes(b"abc")
    with pytest.raises(ValueError, match="shorter"):
        m.reconcile_part(
            part, first_checkpoint_only(), validate_destination=lambda: None
        )
    assert part.read_bytes() == b"abc"


def test_missing_manifest_or_symlink_part_cannot_be_resumed(tmp_path):
    m = api()
    with pytest.raises((FileNotFoundError, ValueError)):
        m.read_manifest(tmp_path / "no-manifest")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    link = tmp_path / "snapshot.btrfs.zst.transfer.part"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        m.reconcile_part(
            link, first_checkpoint_only(), validate_destination=lambda: None
        )
    assert outside.read_bytes() == b"outside"


def test_failed_guard_does_not_write_manifest_or_truncate(tmp_path):
    m = api()
    manifest_path = tmp_path / ".manifest"
    part = tmp_path / "stream.part"
    part.write_bytes(b"abcdUNCOMMITTED")

    def fail_guard():
        raise RuntimeError("mount missing")

    with pytest.raises(RuntimeError, match="mount"):
        m.commit_manifest(
            manifest_path, first_checkpoint_only(), validate_destination=fail_guard
        )
    assert not manifest_path.exists()
    with pytest.raises(RuntimeError, match="mount"):
        m.reconcile_part(part, first_checkpoint_only(), validate_destination=fail_guard)
    assert part.read_bytes() == b"abcdUNCOMMITTED"
