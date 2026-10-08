"""Native Snapper selection in explicit v2 job, no Btrfs filesystem mutation."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli
from btrfs_backup_ng.snapper.source_policy import ResolvedSource
from btrfs_backup_ng.cli.dispatcher import create_subcommand_parser


def parsed(*extra):
    return create_subcommand_parser().parse_args(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--target",
            "/backup",
            "--name",
            "test",
            "--profile-id",
            "p1",
            "--snapper-config",
            "root",
            *extra,
        ]
    )


def test_snapper_latest_discovers_most_recent_stable_and_does_not_reuse_remote(
    tmp_path, monkeypatch
):
    now = datetime.now(timezone.utc)
    snap = SimpleNamespace(
        config_name="root",
        number=42,
        snapshot_type="single",
        date=now - timedelta(minutes=4),
        pre_num=None,
        subvolume_path=tmp_path / "snapshots" / "42" / "snapshot",
    )
    snap.subvolume_path.mkdir(parents=True)

    class Scanner:
        def get_snapshots(self, config):
            assert config == "root"
            return [snap]

    monkeypatch.setattr(cli, "SnapperScanner", Scanner)
    monkeypatch.setattr(
        cli,
        "resolve_source_identity",
        lambda obj: ResolvedSource("uuid-42", True, True),
    )
    monkeypatch.setattr(cli, "discover_raw_snapshots", lambda root: [])
    args = parsed("--source-mode", "latest-snapper")
    cli._resolve_source_choice(args, tmp_path)
    assert args.source == str(snap.subvolume_path)
    assert args.snapper_number == 42


def test_snapper_selected_pre_accepted_explicitly(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    snap = SimpleNamespace(
        config_name="root",
        number=7,
        snapshot_type="pre",
        date=now,
        pre_num=None,
        subvolume_path=tmp_path / "snapshots" / "7" / "snapshot",
    )
    snap.subvolume_path.mkdir(parents=True)
    monkeypatch.setattr(
        cli, "SnapperScanner", lambda: SimpleNamespace(get_snapshots=lambda c: [snap])
    )
    monkeypatch.setattr(
        cli,
        "resolve_source_identity",
        lambda obj: ResolvedSource("uuid-pre", True, True),
    )
    monkeypatch.setattr(cli, "discover_raw_snapshots", lambda root: [])
    args = parsed("--source-mode", "selected-snapper", "--snapper-number", "7")
    cli._resolve_source_choice(args, tmp_path)
    assert args.source == str(snap.subvolume_path)


def test_snapper_create_uses_native_snapshot_creation_command(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    snap = SimpleNamespace(
        config_name="root",
        number=71,
        snapshot_type="single",
        date=now,
        pre_num=None,
        subvolume_path=tmp_path / "snapshots" / "71" / "snapshot",
    )
    snap.subvolume_path.mkdir(parents=True)
    monkeypatch.setattr(
        cli, "SnapperScanner", lambda: SimpleNamespace(get_snapshots=lambda c: [snap])
    )
    monkeypatch.setattr(
        cli,
        "resolve_source_identity",
        lambda obj: ResolvedSource("uuid-71", True, True),
    )
    monkeypatch.setattr(cli, "discover_raw_snapshots", lambda root: [])
    commands = []

    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout="71\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", run)
    args = parsed("--source-mode", "create-snapper")
    cli._resolve_source_choice(args, tmp_path)
    assert commands
    assert commands[0][:4] == ["snapper", "-c", "root", "create"]
    assert "--print-number" in commands[0]
    assert args.snapper_number == 71
    assert args.source == str(snap.subvolume_path)


def test_without_source_or_snapper_rejected_before_backup(tmp_path):
    args = create_subcommand_parser().parse_args(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--target",
            str(tmp_path),
            "--name",
            "snap",
            "--profile-id",
            "p1",
        ]
    )
    with pytest.raises(ValueError, match="source"):
        cli._resolve_source_choice(args, tmp_path)


def test_latest_snapper_does_not_backfill_an_older_snapshot(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc)
    older = SimpleNamespace(
        config_name="root",
        number=11,
        snapshot_type="single",
        date=now - timedelta(hours=3),
        pre_num=None,
        subvolume_path=tmp_path / "11" / "snapshot",
    )
    latest = SimpleNamespace(
        config_name="root",
        number=12,
        snapshot_type="single",
        date=now - timedelta(minutes=2),
        pre_num=None,
        subvolume_path=tmp_path / "12" / "snapshot",
    )
    older.subvolume_path.mkdir(parents=True)
    latest.subvolume_path.mkdir(parents=True)
    monkeypatch.setattr(
        cli,
        "SnapperScanner",
        lambda: SimpleNamespace(get_snapshots=lambda config: [older, latest]),
    )
    monkeypatch.setattr(
        cli,
        "resolve_source_identity",
        lambda candidate: ResolvedSource(f"uuid-{candidate.number}", True, True),
    )
    monkeypatch.setattr(
        cli,
        "discover_raw_snapshots",
        lambda root: [SimpleNamespace(source_uuid="uuid-12")],
    )
    args = parsed("--source-mode", "latest-snapper")
    with pytest.raises(ValueError, match="already|eligible"):
        cli._resolve_source_choice(args, tmp_path)
