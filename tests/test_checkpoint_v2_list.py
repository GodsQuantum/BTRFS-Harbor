import json

from btrfs_backup_ng.cli.dispatcher import main
from btrfs_backup_ng.core.checkpoint_v2 import ResumeManifest, commit_manifest


def test_list_reports_root_owned_style_manifest_with_name_and_resume(tmp_path, capsys):
    transfer_id = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"
    manifest = ResumeManifest(
        schema_version=2,
        transfer_id=transfer_id,
        checkpoint_size=128,
        state="paused",
        checkpoints=(),
        identity={
            "profile_id": "profile",
            "source_volume": "/",
            "source_path": "/.snapshots/1/snapshot",
            "source_uuid": "b0cbeb30-9917-44d8-9f27-1ed967f91a2d",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "export",
            "kernel_release": "7",
            "btrfs_progs_version": "7",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
    )
    commit_manifest(
        tmp_path / f".harbor-resume-{transfer_id}.json",
        manifest,
        validate_destination=lambda: None,
    )
    (tmp_path / f"home.btrfs.zst.{transfer_id}.part").write_bytes(b"")
    assert main(["raw", "checkpoint-v2", "list", "--target", str(tmp_path)]) == 0
    entries = json.loads(capsys.readouterr().out)
    assert len(entries) == 1
    assert entries[0]["transfer_id"] == transfer_id
    assert entries[0]["name"] == "home"
    assert entries[0]["resumable"] is True
    assert entries[0]["verified"] is False
    assert entries[0]["committed_raw_bytes"] == 0


def test_list_ignores_symlinked_manifests_and_corrupt_entries(tmp_path, capsys):
    (tmp_path / ".harbor-resume-3b88e8c1-5cb8-4f67-aa10-f9544b6228f0.json").symlink_to(
        "/etc/passwd"
    )
    (
        tmp_path / ".harbor-resume-3b88e8c1-5cb8-4f67-aa10-f9544b6228f0.json.tmp"
    ).write_text("{}")
    assert main(["raw", "checkpoint-v2", "list", "--target", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_list_missing_target_never_creates_directory(tmp_path):
    missing = tmp_path / "absent"
    assert main(["raw", "checkpoint-v2", "list", "--target", str(missing)]) == 2
    assert not missing.exists()
