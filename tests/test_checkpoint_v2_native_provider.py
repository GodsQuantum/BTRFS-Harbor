"""Native Btrfs snapshots without Snapper: safe source, real UUID and chain selection."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from btrfs_backup_ng.cli import checkpoint_v2_cmd as cli
from btrfs_backup_ng.core import native_snapshots as native
from btrfs_backup_ng.core.replay_v2 import SourceFingerprint
from btrfs_backup_ng.cli.dispatcher import create_subcommand_parser


def args(root: Path, source: Path, mode="create-native", name=None):
    argv = [
        "raw",
        "checkpoint-v2",
        "start",
        "--target",
        str(root),
        "--name",
        "test",
        "--profile-id",
        "11111111-1111-4111-8111-111111111111",
        "--source-mode",
        mode,
        "--source",
        str(source),
    ]
    if name:
        argv += ["--native-name", name]
    return create_subcommand_parser().parse_args(argv)


def fake_source(path: Path):
    return SourceFingerprint(
        uuid="00000000-0000-4000-8000-000000000001",
        parent_uuid=None,
        path=str(path),
        readonly=True,
        send_fingerprint="protocol=2",
    )


def test_native_list_requires_real_subvolume_and_never_follows_symlink(
    tmp_path, monkeypatch
):
    source = tmp_path / "subvol"
    source.mkdir()
    monkeypatch.setattr(native, "_subvolume_root", lambda path: path)
    assert native.list_native_snapshots(source) == []
    snapshot_dir = source / native.NATIVE_FOLDER
    snapshot_dir.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        native.list_native_snapshots(source)


def test_native_list_displays_actual_readonly_snapshots_only(tmp_path, monkeypatch):
    source = tmp_path / "subvol"
    directory = source / native.NATIVE_FOLDER
    directory.mkdir(parents=True)
    n1 = "harbor-20261009T120000Z-abcdef123456"
    n2 = "harbor-20261009T120001Z-abcdef123457"
    (directory / n1).mkdir()
    (directory / n2).mkdir()
    (directory / "ignore-this").mkdir()
    monkeypatch.setattr(native, "_subvolume_root", lambda path: path)
    monkeypatch.setattr(native, "inspect_readonly_btrfs_source", fake_source)
    found = native.list_native_snapshots(source)
    assert [s.name for s in found] == [n2, n1]
    assert found[0].date == "2026-10-09T12:00:01+00:00"
    assert native.find_native_snapshot(source, n1).path == directory / n1
    with pytest.raises(ValueError, match="invalid"):
        native.find_native_snapshot(source, "../etc")


def test_native_same_second_sort_preserves_legacy_names_and_uses_nanoseconds(
    tmp_path, monkeypatch
):
    source = tmp_path / "subvol"
    directory = source / native.NATIVE_FOLDER
    directory.mkdir(parents=True)
    legacy = "harbor-20261009T120000Z-ffffffffffff"
    early = "harbor-20261009T120000.000000001Z-ffffffffffff"
    late = "harbor-20261009T120000.999999999Z-000000000000"
    for item in (legacy, early, late):
        (directory / item).mkdir()
    monkeypatch.setattr(native, "_subvolume_root", lambda path: path)
    monkeypatch.setattr(native, "inspect_readonly_btrfs_source", fake_source)
    assert [x.name for x in native.list_native_snapshots(source)] == [
        late,
        early,
        legacy,
    ]
    assert native.native_snapshot_sort_key(late) > native.native_snapshot_sort_key(
        early
    )
    with pytest.raises(ValueError, match="invalid"):
        native.native_snapshot_sort_key("../etc")


def test_native_creation_uses_argv_and_never_mounts_or_deletes(tmp_path, monkeypatch):
    source = tmp_path / "subvol"
    source.mkdir()
    monkeypatch.setattr(native, "_subvolume_root", lambda path: path)
    monkeypatch.setattr(native, "inspect_readonly_btrfs_source", fake_source)
    calls = []

    def safe_run(argv, **kwargs):
        calls.append(argv)
        assert argv[:4] == ["btrfs", "subvolume", "snapshot", "-r"]
        assert argv[4] == str(source)
        Path(argv[5]).mkdir()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(native.subprocess, "run", safe_run)
    snapshot = native.create_native_snapshot(source)
    assert snapshot.path.is_dir()
    assert snapshot.uuid
    assert len(calls) == 1
    assert snapshot.name.startswith("harbor-")


def test_native_mode_resolves_one_managed_source_and_auto_full(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    chosen = native.NativeSnapshot(
        "harbor-20261009T120000Z-abcdef123456",
        source / native.NATIVE_FOLDER / "harbor-20261009T120000Z-abcdef123456",
        "00000000-0000-4000-8000-000000000001",
        "2026-10-09T12:00:00Z",
    )
    chosen.path.mkdir(parents=True)
    monkeypatch.setattr(cli, "list_native_snapshots", lambda path: [chosen])
    monkeypatch.setattr(cli, "discover_raw_snapshots", lambda root: [])
    a = args(tmp_path, source, "latest-native")
    cli._resolve_source_choice(a, tmp_path)
    assert a.source == str(chosen.path)
    assert a.native_root == str(source)
    cli._select_automatic_incremental_parent(a, tmp_path)
    assert a.parent is None


def test_native_mode_rejects_duplicate_snapshot_uuid(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    chosen = native.NativeSnapshot(
        "harbor-20261009T120000Z-abcdef123456",
        source / "snap",
        "00000000-0000-4000-8000-000000000001",
        "2026-10-09T12:00:00Z",
    )
    monkeypatch.setattr(cli, "list_native_snapshots", lambda path: [chosen])
    monkeypatch.setattr(
        cli,
        "discover_raw_snapshots",
        lambda root: [SimpleNamespace(source_uuid=chosen.uuid)],
    )
    with pytest.raises(ValueError, match="already backed up"):
        cli._resolve_source_choice(args(tmp_path, source, "latest-native"), tmp_path)


def test_native_selected_snapshot_requires_owned_name(tmp_path, monkeypatch):
    a = args(tmp_path, tmp_path / "source", "selected-native", "../oops")
    with pytest.raises(ValueError, match="invalid"):
        cli._resolve_source_choice(a, tmp_path)


def test_native_cli_list_action_is_read_only(tmp_path, monkeypatch, capsys):
    found = native.NativeSnapshot(
        "harbor-20261009T120000Z-abcdef123456",
        tmp_path / "snapshot",
        "00000000-0000-4000-8000-000000000001",
        "2026-10-09T12:00:00Z",
    )
    monkeypatch.setattr(cli, "list_native_snapshots", lambda path: [found])
    a = create_subcommand_parser().parse_args(
        ["raw", "checkpoint-v2", "native-list", "--source", str(tmp_path)]
    )
    assert cli.execute_checkpoint_v2(a) == 0
    assert '"name": "harbor-20261009T120000Z-abcdef123456"' in capsys.readouterr().out
