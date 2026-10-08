"""Crash after final stream rename but before completed manifest write."""

import hashlib
from unittest.mock import Mock

import pytest

from btrfs_backup_ng.cli.dispatcher import main
from btrfs_backup_ng.cli import checkpoint_v2_cmd as cli
from btrfs_backup_ng.core.checkpoint_v2 import (
    ResumeManifest,
    read_manifest,
)
from btrfs_backup_ng.core.checkpoint_stream import compress_frame
from btrfs_backup_ng.core.checkpoint_v2_runner import _sealed_sidecar
from btrfs_backup_ng.core.native_send_v2 import destination_fingerprint
from btrfs_backup_ng.endpoint.mount_guard_v2 import MountGuard, capture_mount_identity
from btrfs_backup_ng.endpoint.resumable_raw import ResumableRawSink
import btrfs_backup_ng.endpoint.resumable_raw as sink_mod


TRANSFER = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"
SOURCE = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"


def test_reconcile_after_final_rename_never_restarts_send(
    tmp_path, monkeypatch, capsys
):
    identity = capture_mount_identity(tmp_path)
    fingerprint = destination_fingerprint(identity, tmp_path)
    manifest = ResumeManifest(
        schema_version=2,
        transfer_id=TRANSFER,
        checkpoint_size=16,
        state="preparing",
        checkpoints=(),
        identity={
            "profile_id": "p1",
            "source_volume": "/",
            "source_uuid": SOURCE,
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": fingerprint,
            "kernel_release": "7",
            "btrfs_progs_version": "7",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
    )
    with MountGuard(tmp_path, identity) as guard:
        with ResumableRawSink.open_new(tmp_path, "snap", manifest, guard) as sink:
            raw = b"backed up"
            sink.append_frame(
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                raw_length=len(raw),
                frame=compress_frame(raw, level=3, threads=1),
            )
            real = sink_mod._manifest_commit

            def crash_on_completed(fd, name, new_manifest, actual_guard, **kwargs):
                if new_manifest.state == "completed":
                    raise OSError("crash after final stream rename")
                return real(fd, name, new_manifest, actual_guard, **kwargs)

            with monkeypatch.context() as isolated:
                isolated.setattr(sink_mod, "_manifest_commit", crash_on_completed)
                with pytest.raises(OSError, match="crash after"):
                    sink.publish(_sealed_sidecar(sink))

    final_path = tmp_path / "snap.btrfs.zst"
    assert final_path.exists()
    assert (tmp_path / "snap.btrfs.zst.meta").exists()
    assert (
        read_manifest(tmp_path / f".harbor-resume-{TRANSFER}.json").state == "uploading"
    )
    monkeypatch.setattr(
        cli, "_source_check", Mock(side_effect=AssertionError("unnecessary btrfs send"))
    )
    result = main(
        [
            "raw",
            "checkpoint-v2",
            "resume",
            "--target",
            str(tmp_path),
            "--name",
            "snap",
            "--transfer-id",
            TRANSFER,
            "--experimental",
            "--allow-local",
        ]
    )
    assert result == 0
    assert (
        read_manifest(tmp_path / f".harbor-resume-{TRANSFER}.json").state == "completed"
    )
    assert final_path.exists()
    assert "completed" in capsys.readouterr().out


def test_finalizing_with_sidecar_only_replays_and_finishes_without_new_checkpoints(
    tmp_path, monkeypatch
):
    from io import BytesIO
    from btrfs_backup_ng.core.replay_v2 import (
        SourceFingerprint,
        DestinationFingerprint,
    )
    from btrfs_backup_ng.core.checkpoint_v2_runner import execute_checkpoint_job

    source_bytes = b"serialized native Btrfs send fixture"
    identity = capture_mount_identity(tmp_path)
    fingerprint = destination_fingerprint(identity, tmp_path)
    manifest = ResumeManifest(
        schema_version=2,
        transfer_id=TRANSFER,
        checkpoint_size=16,
        state="preparing",
        checkpoints=(),
        identity={
            "profile_id": "p1",
            "source_volume": "/",
            "source_uuid": SOURCE,
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": fingerprint,
            "kernel_release": "7",
            "btrfs_progs_version": "7",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
    )

    class FakeSend:
        def __init__(self):
            self.stdout = BytesIO(source_bytes)

        def terminate(self):
            pass

        def wait(self):
            return 0

    fp = SourceFingerprint(SOURCE, None, "/snapshots/12/snapshot", "protocol=2", True)
    target = DestinationFingerprint("raw", fingerprint)
    with MountGuard(tmp_path, identity) as guard:
        original = sink_mod._rename_noreplace

        def interrupt(fd, old, new):
            if new == "snap.btrfs.zst":
                raise OSError("metadata durable, final stream rename interrupted")
            return original(fd, old, new)

        with monkeypatch.context() as patcher:
            patcher.setattr(sink_mod, "_rename_noreplace", interrupt)
            with pytest.raises(OSError, match="metadata durable"):
                execute_checkpoint_job(
                    target_root=tmp_path,
                    snapshot_name="snap",
                    manifest=manifest,
                    source=fp,
                    destination=target,
                    guard=guard,
                    spawn=FakeSend,
                    action=lambda: "run",
                    chunk_size=16,
                )
    pending = read_manifest(tmp_path / f".harbor-resume-{TRANSFER}.json")
    assert pending.state == "finalizing"
    assert not (tmp_path / "snap.btrfs.zst").exists()
    assert (tmp_path / "snap.btrfs.zst.meta").exists()
    with MountGuard(tmp_path, capture_mount_identity(tmp_path)) as guard:
        finished = execute_checkpoint_job(
            target_root=tmp_path,
            snapshot_name="snap",
            manifest=pending,
            source=fp,
            destination=target,
            guard=guard,
            spawn=FakeSend,
            action=lambda: "run",
            resume=True,
            chunk_size=16,
        )
    assert finished.status == "completed"
    assert finished.new_checkpoints == 0
    assert (tmp_path / "snap.btrfs.zst").exists()
