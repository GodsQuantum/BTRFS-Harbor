"""An incremental child must not be published without its remote parent."""

from types import SimpleNamespace

import pytest

import btrfs_backup_ng.cli.checkpoint_v2_cmd as cmd
from btrfs_backup_ng.cli.dispatcher import create_subcommand_parser
from btrfs_backup_ng.core.replay_v2 import SourceFingerprint

SOURCE = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"
PARENT = "20e561c9-732d-49b3-ae61-13abf91cedd2"


def args_for(root):
    return create_subcommand_parser().parse_args(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--target",
            str(root),
            "--source",
            "/snapshots/12/snapshot",
            "--parent",
            "/snapshots/10/snapshot",
            "--name",
            "snap",
            "--profile-id",
            "p1",
        ]
    )


def prepare(monkeypatch):
    def probe(path):
        is_parent = path == "/snapshots/10/snapshot"
        return SourceFingerprint(
            uuid=PARENT if is_parent else SOURCE,
            parent_uuid=None,
            path=path,
            send_fingerprint="protocol=2",
            readonly=True,
        )

    monkeypatch.setattr(cmd, "_source_check", probe)
    monkeypatch.setattr(cmd, "_btrfs_version", lambda: "btrfs-progs v7.1")


def test_incremental_parent_absent_from_target_is_refused(tmp_path, monkeypatch):
    prepare(monkeypatch)
    monkeypatch.setattr(cmd, "discover_raw_snapshots", lambda root: [])
    with pytest.raises(ValueError, match="parent"):
        cmd._new_manifest(args_for(tmp_path), "nfs:source")


def test_saved_parent_identity_is_recorded_in_incremental_child(tmp_path, monkeypatch):
    prepare(monkeypatch)
    good_stream = tmp_path / "parent.btrfs.zst"
    good_stream.write_bytes(b"trusted baseline placeholder")
    good_meta = tmp_path / "parent.btrfs.zst.meta"
    good_meta.write_text("{}")
    snap = SimpleNamespace(
        source_uuid=PARENT,
        name="parent-archive",
        stream_path=good_stream,
        metadata_path=good_meta,
        checksum_value="a" * 64,
        stream_completeness="complete",
        provenance_origin="native-write",
    )
    monkeypatch.setattr(cmd, "discover_raw_snapshots", lambda root: [snap])
    manifest, source, parent = cmd._new_manifest(args_for(tmp_path), "nfs:source")
    assert source.parent_uuid == PARENT
    assert str(parent) == "/snapshots/10/snapshot"
    assert manifest.identity["parent_backup_name"] == "parent-archive"


def test_unverifiable_legacy_parent_is_not_a_safe_incremental_base(
    tmp_path, monkeypatch
):
    prepare(monkeypatch)
    stream = tmp_path / "legacy.btrfs"
    stream.write_bytes(b"incomplete")
    snap = SimpleNamespace(
        source_uuid=PARENT,
        name="legacy",
        stream_path=stream,
        metadata_path=tmp_path / "legacy.btrfs.meta",
        checksum_value=None,
        stream_completeness="unknown",
        provenance_origin="filename-inferred",
    )
    monkeypatch.setattr(cmd, "discover_raw_snapshots", lambda root: [snap])
    with pytest.raises(ValueError, match="parent"):
        cmd._new_manifest(args_for(tmp_path), "nfs:source")
