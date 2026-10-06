"""Machine-readable status contract for Btrfs Harbor."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from btrfs_backup_ng.cli import status


class FakeSnapshot:
    def __init__(self, name: str) -> None:
        self._name = name

    def get_name(self) -> str:
        return self._name


class FakeEndpoint:
    def __init__(self, names: list[str]) -> None:
        self._snapshots = [FakeSnapshot(name) for name in names]

    def prepare(self) -> None:
        return None

    def list_snapshots(self) -> list[FakeSnapshot]:
        return self._snapshots


def _config(source: Path, target: Path) -> SimpleNamespace:
    target_config = SimpleNamespace(path=str(target))
    volume = SimpleNamespace(
        path=str(source),
        snapshot_prefix="root-",
        snapshot_dir=".snapshots",
        targets=[target_config],
    )
    global_config = SimpleNamespace(
        parallel_volumes=1,
        parallel_targets=1,
        transaction_log=None,
    )
    config = SimpleNamespace(global_config=global_config)
    config.get_enabled_volumes = Mock(return_value=[volume])
    return config


def _patch_status_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    source_snapshots: list[str],
    target_snapshots: list[str],
) -> tuple[Path, Path]:
    source = tmp_path / "source"
    target = tmp_path / "target"
    snapshot_dir = source / ".snapshots"
    source.mkdir()
    target.mkdir()
    snapshot_dir.mkdir()

    config_path = tmp_path / "config.toml"
    config_path.write_text("[global]\n", encoding="utf-8")
    config = _config(source, target)

    monkeypatch.setattr(status, "find_config_file", lambda _value: config_path)
    monkeypatch.setattr(status, "load_config", lambda _path: (config, None))
    monkeypatch.setattr(status, "apply_config_verbosity", lambda _args, _config: None)
    monkeypatch.setattr(status, "btrfs_debug_enabled", lambda _args, _config: False)
    monkeypatch.setattr(status, "get_timestamp_format", lambda _config: "%Y%m%d")
    monkeypatch.setattr(
        status, "resolve_snapshot_dir", lambda _dir, _source: snapshot_dir
    )
    monkeypatch.setattr(
        status, "thread_ssh_target_config", lambda _kwargs, _target: None
    )

    endpoints = iter(
        [
            FakeEndpoint(source_snapshots),
            FakeEndpoint(target_snapshots),
        ]
    )
    monkeypatch.setattr(
        status.endpoint, "choose_endpoint", lambda *_a, **_kw: next(endpoints)
    )
    return source, target


def test_status_json_reports_healthy_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, target = _patch_status_runtime(
        monkeypatch,
        tmp_path,
        source_snapshots=["root-001", "root-002"],
        target_snapshots=["root-001", "root-002"],
    )

    args = argparse.Namespace(
        config=None,
        format="json",
        transactions=False,
        limit=10,
        verbose=0,
        quiet=False,
        debug=False,
    )

    exit_code = status.execute_status(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == 1
    assert payload["healthy"] is True
    assert payload["config"]
    assert payload["volumes"] == [
        {
            "path": str(source),
            "source": {
                "status": "ok",
                "snapshot_count": 2,
                "latest_snapshot": "root-002",
            },
            "targets": [
                {
                    "path": str(target),
                    "status": "ok",
                    "backup_count": 2,
                    "pending": 0,
                }
            ],
        }
    ]


def test_status_json_is_valid_when_target_has_no_backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_runtime(
        monkeypatch,
        tmp_path,
        source_snapshots=["root-001"],
        target_snapshots=[],
    )

    args = argparse.Namespace(
        config=None,
        format="json",
        transactions=False,
        limit=10,
        verbose=0,
        quiet=False,
        debug=False,
    )

    exit_code = status.execute_status(args)
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 1
    assert payload["schema_version"] == 1
    assert payload["healthy"] is False
    assert payload["volumes"][0]["targets"][0]["status"] == "no backups"
    assert payload["volumes"][0]["targets"][0]["backup_count"] == 0


def test_status_parser_accepts_json_format() -> None:
    from btrfs_backup_ng.cli.dispatcher import create_subcommand_parser

    args = create_subcommand_parser().parse_args(["status", "--format", "json"])

    assert args.command == "status"
    assert args.format == "json"
