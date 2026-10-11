"""All-or-nothing existing Snapper provider, no silent Harbor snapshot creation."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from btrfs_backup_ng.cli import machine_set_v2 as m
from btrfs_backup_ng.snapper.source_policy import SourceDecision


PROFILE = "55555555-5555-4555-8555-555555555555"
SET_ID = "66666666-6666-4666-8666-666666666666"
UUID = "77777777-7777-4777-8777-777777777777"


def _fake_snapper(monkeypatch, tmp_path: Path, *, covered: bool = True):
    root = tmp_path / "system"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    p = root / ".snapshots" / "7" / "snapshot"
    p.mkdir(parents=True)
    q = home / ".snapshots" / "8" / "snapshot"
    q.mkdir(parents=True)
    a = SimpleNamespace(
        config_name="root",
        number=7,
        subvolume_path=p,
        snapshot_type="single",
        pre_num=None,
        date=datetime(2026, 10, 11, 10, tzinfo=timezone.utc),
    )
    b = SimpleNamespace(
        config_name="home",
        number=8,
        subvolume_path=q,
        snapshot_type="single",
        pre_num=None,
        date=datetime(2026, 10, 11, 10, tzinfo=timezone.utc),
    )
    configurations = [
        SimpleNamespace(name="root", subvolume=root, fstype="btrfs"),
        *(
            [SimpleNamespace(name="home", subvolume=home, fstype="btrfs")]
            if covered
            else []
        ),
    ]
    scanner = Mock()
    scanner.list_configs.return_value = configurations
    scanner.get_snapshots.side_effect = lambda conf: (
        [a] if getattr(conf, "name", conf) == "root" else [b]
    )
    monkeypatch.setattr(m, "SnapperScanner", lambda: scanner)
    monkeypatch.setattr(
        m,
        "select_source",
        lambda snaps, **kwargs: SourceDecision("send_existing", list(snaps)[0], UUID),
    )
    monkeypatch.setattr(
        m,
        "inspect_readonly_btrfs_source",
        lambda _path: SimpleNamespace(uuid=UUID, readonly=True),
    )
    return root, home, p, q


def test_existing_snapper_source_coverage_uses_snapshots_without_creating(
    monkeypatch, tmp_path
):
    root, home, p, q = _fake_snapper(monkeypatch, tmp_path)
    monkeypatch.setattr(
        m,
        "create_native_snapshot",
        lambda *_args, **_kwargs: pytest.fail("must not create Harbor snapshot"),
    )
    found = m._snapper_latest_for_machine_sources([str(root), str(home)])
    assert [item["snapshot_path"] for item in found] == [str(p), str(q)]
    assert [item["snapper_number"] for item in found] == [7, 8]
    assert [item["snapshot_uuid"] for item in found] == [UUID, UUID]
    assert all(item["snapshot_created_utc"].startswith("2026-10-11") for item in found)
    assert all(item["snapshot_created_utc"].startswith("2026-10-11") for item in found)


def test_missing_snapper_config_refuses_partial_machine(monkeypatch, tmp_path):
    root, home, *_ = _fake_snapper(monkeypatch, tmp_path, covered=False)
    with pytest.raises(ValueError, match="Snapper cannot cover"):
        m._snapper_latest_for_machine_sources([str(root), str(home)])


def test_machine_manifest_accepts_snapper_but_not_path_traversal(tmp_path):
    root = tmp_path / "root"
    item = {
        "archive": "machine20261011T170600-66666666-000",
        "original_mount": str(root),
        "snapshot_provider": "snapper",
        "snapshot_path": str(root / ".snapshots" / "7" / "snapshot"),
        "snapshot_uuid": UUID,
        "snapper_config": "root",
        "snapper_number": 7,
        "status": "pending",
        "transfer_id": None,
    }
    data = {
        "schema_version": 1,
        "set_id": SET_ID,
        "profile_id": PROFILE,
        "members": [item],
        "coverage": "btrfs-only",
        "status": "pending",
    }
    manifest = tmp_path / f".harbor-machine-set-{SET_ID}.json"
    manifest.write_text(json.dumps(data))
    assert m._read(tmp_path, SET_ID)["members"][0]["snapshot_provider"] == "snapper"
    item["snapshot_path"] = str(root / ".snapshots" / "8" / "snapshot")
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsafe Snapper"):
        m._read(tmp_path, SET_ID)
    item["snapshot_path"] = str(root / ".snapshots" / "7" / "snapshot")
    item["snapper_config"] = "../../bad"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unsafe Snapper"):
        m._read(tmp_path, SET_ID)


def test_machine_manifest_old_harbor_provider_is_still_accepted(tmp_path):
    root = tmp_path / "root"
    data = {
        "schema_version": 1,
        "set_id": SET_ID,
        "profile_id": PROFILE,
        "members": [
            {
                "archive": "machine20261011T170600-66666666-000",
                "original_mount": str(root),
                "snapshot_path": str(
                    root
                    / ".btrfs-harbor-snapshots"
                    / "harbor-20261011T170600.123456789Z-abcdef012345"
                ),
                "snapshot_uuid": None,
                "status": "pending",
                "transfer_id": None,
            }
        ],
        "coverage": "btrfs-only",
        "status": "pending",
    }
    (tmp_path / f".harbor-machine-set-{SET_ID}.json").write_text(json.dumps(data))
    assert (
        m._read(tmp_path, SET_ID)["members"][0].get("snapshot_provider", "harbor")
        == "harbor"
    )
