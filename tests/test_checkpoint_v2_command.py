"""Explicit v2 command must never launch a Btrfs send without opt-in/safety."""

from unittest.mock import Mock


from btrfs_backup_ng.cli.dispatcher import main


def test_v2_start_without_experimental_refuses_without_spawning(
    tmp_path, monkeypatch, capsys
):
    import btrfs_backup_ng.core.native_send_v2 as native

    called = Mock(side_effect=AssertionError("unexpected send"))
    monkeypatch.setattr(native, "spawn_btrfs_send", called)
    rc = main(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--target",
            str(tmp_path),
            "--source",
            "/fake/source",
            "--name",
            "snap",
            "--profile-id",
            "test",
            "--allow-local",
        ]
    )
    assert rc == 2
    called.assert_not_called()
    assert "experimental" in capsys.readouterr().err.lower()


def test_v2_status_reads_exact_manifest_without_processing_send(tmp_path, capsys):
    from btrfs_backup_ng.core.checkpoint_v2 import ResumeManifest, commit_manifest
    from btrfs_backup_ng.cli.dispatcher import main

    transfer_id = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"
    m = ResumeManifest(
        2,
        transfer_id,
        {
            "profile_id": "test",
            "source_volume": "/",
            "source_uuid": "b0cbeb30-9917-44d8-9f27-1ed967f91a2d",
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "test",
            "kernel_release": "7",
            "btrfs_progs_version": "7",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
        16,
        "paused",
        (),
    )
    commit_manifest(
        tmp_path / f".harbor-resume-{transfer_id}.json",
        m,
        validate_destination=lambda: None,
    )
    rc = main(
        [
            "raw",
            "checkpoint-v2",
            "status",
            "--target",
            str(tmp_path),
            "--transfer-id",
            transfer_id,
        ]
    )
    assert rc == 0
    stdout = capsys.readouterr().out
    assert '"state": "paused"' in stdout
    assert transfer_id in stdout


def test_v2_discard_without_confirmation_never_deletes(tmp_path):
    part = tmp_path / "snap.btrfs.zst.3b88e8c1-5cb8-4f67-aa10-f9544b6228f0.part"
    part.write_bytes(b"irreplaceable")
    assert (
        main(
            [
                "raw",
                "checkpoint-v2",
                "discard",
                "--target",
                str(tmp_path),
                "--name",
                "snap",
                "--transfer-id",
                "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
            ]
        )
        == 2
    )
    assert part.read_bytes() == b"irreplaceable"


def test_explicit_opt_in_synthetic_end_to_end_cli_start(tmp_path, monkeypatch, capsys):
    import io
    import json

    import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli
    from btrfs_backup_ng.core.replay_v2 import SourceFingerprint
    from btrfs_backup_ng.core.verify_v2 import verify_checkpoint_index

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    source = SourceFingerprint(
        uuid="b0cbeb30-9917-44d8-9f27-1ed967f91a2d",
        parent_uuid=None,
        path="/snapshots/12/snapshot",
        send_fingerprint="protocol=2",
        readonly=True,
    )
    monkeypatch.setattr(cli, "_source_check", lambda path: source)
    monkeypatch.setattr(cli, "_btrfs_version", lambda: "btrfs-progs v7.0")
    captured = []

    class FakeSend:
        def __init__(self):
            self.stdout = io.BytesIO(bytes(range(255)) * 3)
            self.terminated = False

        def wait(self):
            return 0

        def terminate(self):
            self.terminated = True

    def spawn(_source, _parent):
        captured.append(FakeSend())
        return captured[-1]

    monkeypatch.setattr(cli, "spawn_btrfs_send", spawn)
    rc = main(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--experimental",
            "--allow-local",
            "--target",
            str(tmp_path),
            "--source",
            str(source.path),
            "--name",
            "testbackup",
            "--profile-id",
            "profile-test",
            "--checkpoint-size-mib",
            "1",
        ]
    )
    assert rc == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["status"] == "completed"
    assert captured
    verification = verify_checkpoint_index(
        tmp_path / "testbackup.btrfs.zst.meta",
        tmp_path / "testbackup.btrfs.zst",
        full=True,
    )
    assert verification.status == "ok"
    assert result["transfer_id"]


def test_local_destination_without_allow_local_refuses_send(tmp_path, monkeypatch):
    import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli

    called = Mock(side_effect=AssertionError("source must not be inspected"))
    monkeypatch.setattr(cli, "_source_check", called)
    rc = main(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--experimental",
            "--target",
            str(tmp_path),
            "--source",
            "/snapshot",
            "--name",
            "backup",
            "--profile-id",
            "profile-test",
        ]
    )
    assert rc == 2
    called.assert_not_called()


def test_discard_releases_snapper_pin_only_after_partial_discard(tmp_path, monkeypatch):
    from contextlib import contextmanager

    import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli
    from btrfs_backup_ng.core.checkpoint_v2 import ResumeManifest

    transfer_id = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"
    source_uuid = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"
    original_identity = {
        "profile_id": "test",
        "source_volume": "/",
        "source_uuid": source_uuid,
        "source_path": "/snapshots/12/snapshot",
        "parent_uuid": None,
        "send_fingerprint": "protocol=2",
        "compression": "zstd",
        "compression_level": 3,
        "encryption": "none",
        "destination_type": "raw",
        "destination_fingerprint": "local:test",
        "kernel_release": "7",
        "btrfs_progs_version": "7",
        "harbor_version": "0.2.6",
        "engine_version": "0.9.12",
        "snapper_config": "root",
        "snapper_number": 12,
    }
    manifest = ResumeManifest(2, transfer_id, original_identity, 16, "paused", ())
    monkeypatch.setattr(cli, "read_manifest", lambda path: manifest)
    events = []

    @contextmanager
    def guarded():
        yield

    monkeypatch.setattr(
        cli, "_open_guard", lambda *args, **kwargs: (guarded(), "local:test")
    )

    class FakeSink:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def discard(self, confirmed):
            assert confirmed
            events.append("discard")

    monkeypatch.setattr(cli, "load_existing", lambda *a, **kw: FakeSink())

    class FakePins:
        def release(self, *args):
            events.append("unpin")
            assert args == ("root", 12, source_uuid, transfer_id)

    monkeypatch.setattr(cli, "_pin_manager", lambda path: FakePins())
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    rc = main(
        [
            "raw",
            "checkpoint-v2",
            "discard",
            "--target",
            str(tmp_path),
            "--name",
            "snap",
            "--transfer-id",
            transfer_id,
            "--confirm",
            "--allow-local",
        ]
    )
    assert rc == 0
    assert events == ["discard", "unpin"]


def test_v2_stop_requests_and_signals_only_registered_send(
    tmp_path, monkeypatch, capsys
):
    from btrfs_backup_ng.core.checkpoint_control_v2 import ControlJournal

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    called = []
    monkeypatch.setattr(
        ControlJournal,
        "signal_registered_send",
        lambda self: called.append(self.action()) or True,
    )
    rc = main(
        [
            "raw",
            "checkpoint-v2",
            "stop",
            "--transfer-id",
            "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        ]
    )
    assert rc == 0
    assert called == ["stop"]
    assert '"signal_sent": true' in capsys.readouterr().out.lower()
