"""Snapper sends carry the source subvolume basename, not the backup file name."""

from btrfs_backup_ng.endpoint.raw_metadata import RawSnapshot


def test_v2_sidecar_persists_source_basename_for_received_snapshot(tmp_path):
    original = RawSnapshot(
        name="root-123-20261008",
        stream_path=tmp_path / "root-123-20261008.btrfs.zst",
        source_uuid="1fe1ed64-184c-4c15-b09f-6aafab134d85",
        received_subvolume_name="snapshot",
        compress="zstd",
    )
    restored = RawSnapshot.from_dict(original.to_dict(), original.stream_path)
    assert restored.name == "root-123-20261008"
    assert restored.received_name == "snapshot"
    assert restored.to_dict()["received_subvolume_name"] == "snapshot"


def test_legacy_raw_received_name_unchanged(tmp_path):
    restored = RawSnapshot.from_dict(
        {"name": "my-backup", "version": 1}, tmp_path / "my-backup.btrfs"
    )
    assert restored.received_name == "my-backup"
