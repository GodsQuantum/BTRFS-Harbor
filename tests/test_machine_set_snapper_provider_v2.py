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


def test_missing_snapper_config_uses_explicit_harbor_fallback(monkeypatch, tmp_path):
    root, home, _root_snapshot, _home_snapshot = _fake_snapper(
        monkeypatch, tmp_path, covered=False
    )
    selected = m._snapper_latest_for_machine_sources([str(root), str(home)])
    assert [item["snapshot_provider"] for item in selected] == ["snapper", "harbor"]
    assert "snapper_number" not in selected[1]
    assert selected[1]["source"] == str(home)


def test_swap_only_source_is_excluded_without_creating_snapshots(monkeypatch, tmp_path):
    src = tmp_path / "swap"
    src.mkdir()
    (src / "swapfile").write_bytes(b"SWAPSPACE2")
    swapinfo = tmp_path / "swaps"
    swapinfo.write_text(
        "Filename Type Size Used Priority\n"
        + str(src / "swapfile")
        + " file 1048576 0 -2\n"
    )
    fstab = tmp_path / "fstab"
    fstab.write_text("")
    monkeypatch.setattr(m, "_is_real_subvolume_root", lambda path: path == str(src))
    kept, ignored = m._exclude_verified_volatile_swap(
        ["/", str(src)], swaps_path=swapinfo, fstab_path=fstab, swap_mounts=(str(src),)
    )
    assert kept == ["/"]
    assert ignored[0]["reason"] == "verified_volatile_swap_only"
    assert ignored[0]["swapfiles"] == [str(src / "swapfile")]
    (src / "important-user-document").write_text("DO NOT LOSE ME")
    kept, ignored = m._exclude_verified_volatile_swap(
        ["/", str(src)], swaps_path=swapinfo, fstab_path=fstab, swap_mounts=(str(src),)
    )
    assert kept == ["/", str(src)]
    assert not ignored


def test_swap_with_unrecognized_file_never_hidden(monkeypatch, tmp_path):
    src = tmp_path / "swap"
    src.mkdir()
    (src / "swapfile").write_bytes(b"not registered")
    swaps = tmp_path / "swaps"
    swaps.write_text("Filename Type Size Used Priority\n")
    fstab = tmp_path / "fstab"
    fstab.write_text("")
    monkeypatch.setattr(m, "_is_real_subvolume_root", lambda _p: True)
    assert m._exclude_verified_volatile_swap(
        [str(src)], swaps_path=swaps, fstab_path=fstab, swap_mounts=(str(src),)
    ) == ([str(src)], [])


def test_ambiguous_snapper_configs_still_fail_closed(monkeypatch, tmp_path):
    root, _home, *_ = _fake_snapper(monkeypatch, tmp_path)
    scanner = m.SnapperScanner()
    scanner.list_configs.return_value = [
        SimpleNamespace(name="root", subvolume=root, fstype="btrfs"),
        SimpleNamespace(name="root-duplicate", subvolume=root, fstype="btrfs"),
    ]
    with pytest.raises(ValueError, match="Ambiguous"):
        m._snapper_latest_for_machine_sources([str(root)])


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


def test_one_click_snapper_with_swap_and_unconfigured_home_catalog_is_mixed(
    monkeypatch, tmp_path
):
    from contextlib import nullcontext
    import argparse
    import copy

    root, home, old_snap, _ = _fake_snapper(monkeypatch, tmp_path, covered=False)
    swap = tmp_path / "swap"
    swap.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    sources = [str(root), str(home), str(swap)]
    inventory = {"inventory_complete": True, "sources": sources}
    omitted = [
        {
            "mount_point": str(swap),
            "reason": "verified_volatile_swap_only",
            "swapfiles": [str(swap / "swapfile")],
        }
    ]
    monkeypatch.setattr(m, "_sources_from_inventory", lambda: (sources, inventory))
    monkeypatch.setattr(
        m,
        "_exclude_verified_volatile_swap",
        lambda items: (items[:2], omitted),
    )
    monkeypatch.setattr(m, "_is_real_subvolume_root", lambda _path: True)
    monkeypatch.setattr(m, "_target_root", lambda _path: target)
    fake_guard = Mock()
    fake_guard.__enter__ = Mock(return_value=fake_guard)
    fake_guard.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        m,
        "_open_guard",
        lambda *_a, **_kw: (fake_guard, "known-mounted-destination"),
    )
    monkeypatch.setattr(m, "_exclusive_set_lock", lambda *_a: nullcontext())
    monkeypatch.setattr(m, "_mount_rows", lambda _raw: [])
    monkeypatch.setattr(
        m,
        "assert_configured_boot_mounts_present",
        lambda *_a: None,
    )
    monkeypatch.setattr(m, "discover_boot_mounts", lambda _rows: [])
    monkeypatch.setattr(m, "snapshot_boot_layout", lambda: {})
    commits = []
    monkeypatch.setattr(
        m, "_write", lambda _target, data, **kw: commits.append(copy.deepcopy(data))
    )
    monkeypatch.setattr(
        m,
        "create_native_snapshot",
        lambda *_a, **_kw: pytest.fail("snapshot before durable journal"),
    )

    def complete(_root, data, **_kw):
        assert commits and commits[-1]["members"] == data["members"]
        data["status"] = "completed_btrfs_only"
        return data

    monkeypatch.setattr(m, "_drive", complete)
    monkeypatch.setattr(m, "_result", lambda _data: 0)
    args = argparse.Namespace(
        checkpoint_action="set-start",
        experimental=True,
        target=str(target),
        profile_id=PROFILE,
        source=None,
        snapshot_provider="snapper",
        allow_local=True,
        state_dir=None,
        max_incremental_depth=7,
    )
    assert m._execute_machine_set_unlocked(args) == 0
    doc = commits[-1]
    assert doc["snapshot_requested_provider"] == "snapper"
    assert doc["snapshot_provider"] == "mixed"
    assert [member["snapshot_provider"] for member in doc["members"]] == [
        "snapper",
        "harbor",
    ]
    assert doc["members"][0]["snapshot_path"] == str(old_snap)
    assert doc["members"][1]["snapshot_uuid"] is None
    assert ".btrfs-harbor-snapshots" in doc["members"][1]["snapshot_path"]
    assert doc["excluded_technical_subvolumes"] == omitted
    assert len(doc["members"]) == 2


def test_machine_progress_uses_destination_file_sizes_without_fabricated_eta(tmp_path):
    from btrfs_backup_ng.cli.machine_set_v2 import _machine_progress_measure

    completed = tmp_path / "machine-000.btrfs.zst"
    completed.write_bytes(b"x" * 1024)
    unfinished = (
        tmp_path / "machine-001.btrfs.zst.12345678-1234-4234-8234-123456789abc.part"
    )
    unfinished.write_bytes(b"y" * 4096)
    # An attacker-controlled symlink that looks like a checkpoint is never read.
    (tmp_path / "machine-001.btrfs.zst.malicious.part").symlink_to(completed)
    data = {
        "members": [
            {"archive": "machine-000", "original_mount": "/", "status": "completed"},
            {"archive": "machine-001", "original_mount": "/home", "status": "sending"},
        ]
    }
    output = _machine_progress_measure(tmp_path, data)
    assert output == {
        "event": "machine_progress",
        "completed_members": 1,
        "total_members": 2,
        "current_mount": "/home",
        "bytes_written": 5120,
        "bytes_per_second": None,
        "eta_seconds": None,
        "percent": None,
    }


def test_machine_progress_handles_missing_destination_without_invented_bytes(tmp_path):
    from btrfs_backup_ng.cli.machine_set_v2 import _machine_progress_measure

    missing = tmp_path / "disconnected-nfs"
    data = {
        "members": [
            {
                "archive": "backup-000",
                "original_mount": "/",
                "status": "pending",
            }
        ]
    }
    result = _machine_progress_measure(missing, data)
    assert result["bytes_written"] is None
    assert result["eta_seconds"] is None


def test_machine_progress_final_line_remains_machine_result(capsys, tmp_path):
    from btrfs_backup_ng.cli.machine_set_v2 import _live_machine_progress, _result

    record = {
        "set_id": SET_ID,
        "status": "pending",
        "coverage": "btrfs-only",
        "members": [
            {
                "archive": "archive-000",
                "original_mount": "/",
                "status": "pending",
            }
        ],
    }
    with _live_machine_progress(tmp_path, record):
        (tmp_path / "archive-000.btrfs.zst.uuid.part").write_bytes(b"x" * 22)
        record["members"][0]["status"] = "completed"
        record["status"] = "completed_btrfs_only"
    assert _result(record) == 0
    lines = capsys.readouterr().out.splitlines()
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["event"] == "machine_progress"
    assert parsed[-2]["bytes_written"] in (None, 0, 22)
    assert parsed[-2]["completed_members"] == 1
    assert parsed[-1]["status"] == "completed_btrfs_only"
    assert parsed[-1]["members"][0]["status"] == "completed"


def test_machine_progress_single_scandir_and_ignores_noncanonical_part(
    tmp_path, monkeypatch
):
    import os
    from btrfs_backup_ng.cli.machine_set_v2 import _machine_progress_measure

    data = {
        "members": [
            {
                "archive": f"machine-{i:03d}",
                "status": "sending",
                "original_mount": f"/{i}",
            }
            for i in range(64)
        ]
    }
    (tmp_path / "machine-001.btrfs.zst.bad.part").write_bytes(b"x" * 99)
    (
        tmp_path / "machine-002.btrfs.zst.12345678-1234-4234-8234-123456789abc.part"
    ).write_bytes(b"y" * 17)
    calls = 0
    original_scandir = os.scandir

    def count_scandir(path):
        nonlocal calls
        calls += 1
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", count_scandir)
    result = _machine_progress_measure(tmp_path, data)
    assert calls == 1
    assert result["bytes_written"] == 17
    assert result["eta_seconds"] is None


def test_machine_progress_final_does_not_stat_stalled_nfs(
    tmp_path, monkeypatch, capsys
):
    import os
    import threading
    import time
    from btrfs_backup_ng.cli.machine_set_v2 import _live_machine_progress, _result

    entered = threading.Event()
    release = threading.Event()
    original_scandir = os.scandir

    def stalled_scandir(path):
        entered.set()
        release.wait(3)
        return original_scandir(path)

    record = {
        "set_id": SET_ID,
        "status": "pending",
        "coverage": "btrfs-only",
        "members": [
            {"archive": "machine-000", "original_mount": "/", "status": "pending"}
        ],
    }
    monkeypatch.setattr(os, "scandir", stalled_scandir)
    try:
        started = time.monotonic()
        with _live_machine_progress(tmp_path, record):
            assert entered.wait(1)
            record["members"][0]["status"] = "completed"
            record["status"] = "completed_btrfs_only"
        assert time.monotonic() - started < 0.65
        assert _result(record) == 0
        release.set()
        time.sleep(0.05)
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert events[-2]["event"] == "machine_progress"
        assert events[-2]["bytes_written"] is None
        assert events[-2]["completed_members"] == 1
        assert events[-1]["status"] == "completed_btrfs_only"
        assert sum(event.get("event") == "machine_progress" for event in events) == 1
    finally:
        release.set()


def test_diverged_member_retry_keeps_original_archive_and_source(tmp_path, monkeypatch):
    root, _, snapshot, _ = _fake_snapper(monkeypatch, tmp_path)
    original = "machine20261011T234658-66666666-000"
    member = {
        "archive": original,
        "original_mount": str(root),
        "snapshot_provider": "snapper",
        "snapshot_path": str(snapshot),
        "snapshot_uuid": UUID,
        "snapper_config": "root",
        "snapper_number": 7,
        "status": "sending",
        "transfer_id": None,
    }
    transfer = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    part = tmp_path / (original + ".btrfs.zst." + transfer + ".part")
    part.write_bytes(b"original committed data preserved")
    m._rebase_divergent_member(
        member,
        {
            "name": original,
            "transfer_id": transfer,
            "state": "failed_unrecoverable",
        },
    )
    assert member["archive"] == original + "-retry1"
    assert member["snapshot_path"] == str(snapshot)
    assert member["status"] == "pending"
    assert member["replay_restarts"] == 1
    assert member["replay_retired"][0]["transfer_id"] == transfer
    assert part.read_bytes() == b"original committed data preserved"
    with pytest.raises(ValueError, match="already"):
        m._rebase_divergent_member(
            member,
            {
                "name": member["archive"],
                "transfer_id": transfer,
                "state": "failed_unrecoverable",
            },
        )


def test_diverged_member_retry_rejects_fake_error_state():
    mbr = {"archive": "machine20261011T234658-66666666-000"}
    with pytest.raises(ValueError, match="verified"):
        m._rebase_divergent_member(
            mbr,
            {
                "name": mbr["archive"],
                "transfer_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "state": "failed_resumable",
            },
        )


def test_machine_resume_replays_only_failing_member_from_start_without_erasing_partial(
    monkeypatch, tmp_path
):
    root, _home, source, _ = _fake_snapper(monkeypatch, tmp_path)
    archive = "machine20261011T234658-66666666-000"
    tx = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    unfinished = tmp_path / (archive + ".btrfs.zst." + tx + ".part")
    unfinished.write_bytes(b"previous safely committed frames")
    state = "paused"
    calls = []

    def entries(_root):
        rows = [
            {
                "name": archive,
                "state": state,
                "transfer_id": tx,
            }
        ]
        if len(calls) > 1:
            rows.append(
                {
                    "name": archive + "-retry1",
                    "state": "completed",
                    "transfer_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                }
            )
        return rows

    def execute(args):
        nonlocal state
        calls.append((args.checkpoint_action, args.name))
        if len(calls) == 1:
            state = "failed_unrecoverable"
            return 3
        return 0

    monkeypatch.setattr(m, "_list_v2", entries)
    monkeypatch.setattr(m, "execute_checkpoint_v2", execute)
    monkeypatch.setattr(m, "_write", lambda *_a, **_k: None)
    monkeypatch.setattr(m, "_archive_valid", lambda _r, name: name.endswith("-retry1"))
    data = {
        "set_id": SET_ID,
        "profile_id": PROFILE,
        "coverage": "btrfs-only",
        "members": [
            {
                "archive": archive,
                "original_mount": str(root),
                "snapshot_provider": "snapper",
                "snapshot_path": str(source),
                "snapshot_uuid": UUID,
                "snapper_config": "root",
                "snapper_number": 7,
                "status": "sending",
                "transfer_id": tx,
            }
        ],
        "boot_members": [],
    }
    result = m._drive(tmp_path, data, allow_local=True, state_dir=None)
    assert calls == [
        ("resume", archive),
        ("start", archive + "-retry1"),
    ]
    assert result["status"] == "completed_btrfs_only"
    assert result["members"][0]["status"] == "completed"
    assert unfinished.read_bytes() == b"previous safely committed frames"


def test_snapper_fresh_requests_readonly_snapshots_on_managed_volumes(
    monkeypatch, tmp_path
):
    root, home, p, q = _fake_snapper(monkeypatch, tmp_path)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            returncode=0, stdout=("7" if "root" in argv else "8"), stderr=""
        )

    monkeypatch.setattr(m.subprocess, "run", run)
    chosen = m._snapper_fresh_for_machine_sources([str(root), str(home)])
    assert [item["snapshot_path"] for item in chosen] == [str(p), str(q)]
    assert all(item["snapshot_provider"] == "snapper" for item in chosen)
    assert len(calls) == 2
    for argv in calls:
        assert argv[0] == "snapper"
        assert "--read-only" in argv
        assert "--print-number" in argv
        assert argv[argv.index("--cleanup-algorithm") + 1] == "number"


def test_snapper_fresh_does_not_hide_snapshot_creation_failures(monkeypatch, tmp_path):
    root, _home, *_ = _fake_snapper(monkeypatch, tmp_path)
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout="", stderr="DBus unavailable"
        ),
    )
    with pytest.raises(RuntimeError, match="Snapper snapshot creation failed"):
        m._snapper_fresh_for_machine_sources([str(root)])
