"""First full, then native incremental when a valid remote/local parent exists."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli
from btrfs_backup_ng.cli.dispatcher import create_subcommand_parser
from btrfs_backup_ng.snapper.source_policy import ResolvedSource

NOW = datetime.now(timezone.utc)
ROOT = "00000000-0000-4000-8000-000000000001"
BEFORE = "00000000-0000-4000-8000-000000000002"


def arg(root: Path):
    return create_subcommand_parser().parse_args(
        [
            "raw",
            "checkpoint-v2",
            "start",
            "--target",
            str(root),
            "--name",
            "latest",
            "--profile-id",
            ROOT,
            "--source-mode",
            "latest-snapper",
            "--snapper-config",
            "root",
        ]
    )


def candidate(tmp_path: Path, number: int):
    path = tmp_path / f"snap-{number}"
    path.mkdir()
    return SimpleNamespace(
        number=number,
        config_name="root",
        snapshot_type="single",
        pre_num=None,
        subvolume_path=path,
        date=NOW - timedelta(minutes=(40 - number)),
    )


def remote(tmp_path, number: int, source_uuid: str, parent=None):
    target = tmp_path / f"back-{number}.btrfs.zst"
    target.write_bytes(b"valid compressed archive fixture")
    target.with_suffix(target.suffix + ".meta").write_text("{}")
    return SimpleNamespace(
        name=f"back-{number}",
        source_uuid=source_uuid,
        stream_path=target,
        metadata_path=target.with_suffix(target.suffix + ".meta"),
        checksum_value="a" * 64,
        stream_completeness="complete",
        provenance_origin="native-write",
        parent_uuid=parent,
        parent_name=f"back-{number - 1}" if parent else None,
    )


def setup(monkeypatch, s1, s2, remotes):
    monkeypatch.setattr(
        cli,
        "SnapperScanner",
        lambda: SimpleNamespace(get_snapshots=lambda conf: [s1, s2]),
    )
    monkeypatch.setattr(
        cli,
        "resolve_source_identity",
        lambda x: ResolvedSource(BEFORE if x is s1 else ROOT, True, True),
    )
    monkeypatch.setattr(cli, "discover_raw_snapshots", lambda root: remotes)


def test_no_remote_base_sends_latest_as_full(tmp_path, monkeypatch):
    s1 = candidate(tmp_path, 1)
    s2 = candidate(tmp_path, 2)
    setup(monkeypatch, s1, s2, [])
    args = arg(tmp_path)
    cli._resolve_source_choice(args, tmp_path)
    cli._select_automatic_incremental_parent(args, tmp_path)
    assert args.source == str(s2.subvolume_path)
    assert args.parent is None
    assert getattr(args, "parent_snapper_number", None) is None


def test_existing_full_and_older_readonly_snapshot_send_incremental(
    tmp_path, monkeypatch
):
    s1 = candidate(tmp_path, 1)
    s2 = candidate(tmp_path, 2)
    setup(monkeypatch, s1, s2, [remote(tmp_path, 1, BEFORE)])
    args = arg(tmp_path)
    cli._resolve_source_choice(args, tmp_path)
    cli._select_automatic_incremental_parent(args, tmp_path)
    assert args.source == str(s2.subvolume_path)
    assert args.parent == str(s1.subvolume_path)
    assert args.parent_snapper_number == 1


def test_missing_local_parent_falls_back_full(tmp_path, monkeypatch):
    s1 = candidate(tmp_path, 1)
    s2 = candidate(tmp_path, 2)
    setup(monkeypatch, s1, s2, [remote(tmp_path, 1, BEFORE)])
    s1.subvolume_path.rmdir()
    args = arg(tmp_path)
    cli._resolve_source_choice(args, tmp_path)
    cli._select_automatic_incremental_parent(args, tmp_path)
    assert args.parent is None


def test_broken_remote_ancestor_chain_falls_back_full(tmp_path, monkeypatch):
    s1 = candidate(tmp_path, 1)
    s2 = candidate(tmp_path, 2)
    # Back 1 claims to be incremental, but its parent is missing: invalid base.
    setup(monkeypatch, s1, s2, [remote(tmp_path, 1, BEFORE, parent="missing")])
    args = arg(tmp_path)
    cli._resolve_source_choice(args, tmp_path)
    cli._select_automatic_incremental_parent(args, tmp_path)
    assert args.parent is None


def test_legacy_parent_without_authoritative_checksum_falls_back_full(
    tmp_path, monkeypatch
):
    s1 = candidate(tmp_path, 1)
    s2 = candidate(tmp_path, 2)
    legacy = remote(tmp_path, 1, BEFORE)
    legacy.checksum_value = None
    setup(monkeypatch, s1, s2, [legacy])
    args = arg(tmp_path)
    cli._resolve_source_choice(args, tmp_path)
    cli._select_automatic_incremental_parent(args, tmp_path)
    assert args.parent is None


def test_both_source_and_incremental_parent_cleanup_pins_released(monkeypatch):
    from btrfs_backup_ng.core.checkpoint_v2 import ResumeManifest

    calls = []
    monkeypatch.setattr(
        cli,
        "_pin_manager",
        lambda state: SimpleNamespace(release=lambda *args: calls.append(args)),
    )
    manifest = ResumeManifest(
        2,
        ROOT,
        identity={
            "profile_id": ROOT,
            "source_volume": "/",
            "source_uuid": ROOT,
            "source_path": "/.snapshots/2/snapshot",
            "parent_uuid": BEFORE,
            "parent_path": "/.snapshots/1/snapshot",
            "parent_backup_name": "back-1",
            "parent_snapper_number": 1,
            "snapper_config": "root",
            "snapper_number": 2,
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
        checkpoint_size=128,
        state="completed",
        checkpoints=(),
    )
    cli._release_snapper_pins(Path("/ignored"), manifest)
    assert calls == [("root", 2, ROOT, ROOT), ("root", 1, BEFORE, ROOT)]


def test_periodic_full_base_breaks_parent_chain_after_bounded_depth(
    tmp_path, monkeypatch
):
    s1 = candidate(tmp_path, 1)
    s2 = candidate(tmp_path, 2)
    older = "00000000-0000-4000-8000-000000000003"
    first = remote(tmp_path, 0, older)
    second = remote(tmp_path, 1, BEFORE, parent=older)
    setup(monkeypatch, s1, s2, [first, second])

    options = arg(tmp_path)
    options.max_incremental_depth = 1
    cli._resolve_source_choice(options, tmp_path)
    cli._select_automatic_incremental_parent(options, tmp_path)
    assert options.parent is None, "depth limit must force a new independent full send"

    allowed = arg(tmp_path)
    allowed.max_incremental_depth = 2
    cli._resolve_source_choice(allowed, tmp_path)
    cli._select_automatic_incremental_parent(allowed, tmp_path)
    assert allowed.parent == str(s1.subvolume_path)


def test_incremental_depth_fails_closed_on_cycle(tmp_path):
    older = "00000000-0000-4000-8000-000000000003"
    first = remote(tmp_path, 0, older)
    second = remote(tmp_path, 1, BEFORE, parent=older)
    first.parent_name = second.name
    first.parent_uuid = BEFORE
    try:
        cli._incremental_depth(second, {item.name: item for item in (first, second)})
    except ValueError as exc:
        assert "ancestor chain" in str(exc)
    else:
        raise AssertionError("cycle must never be used to choose an incremental base")
