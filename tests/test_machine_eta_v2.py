"""Tests for bounded, advisory Btrfs machine-set transfer ETA."""

from __future__ import annotations

from types import SimpleNamespace

from btrfs_backup_ng.core.machine_eta_v2 import estimate_source_bytes, forecast_machine


def test_estimate_source_bytes_reads_btrfs_du_once_without_mutating(
    monkeypatch, tmp_path
):
    source = tmp_path / "root"
    source.mkdir()
    calls = []

    def fake_run(argv, **kw):
        calls.append((argv, kw))
        return SimpleNamespace(
            returncode=0,
            stdout=f"Total Exclusive Set shared Filename\n10737418240 10737418240 0 {source}\n",
            stderr="",
        )

    monkeypatch.setattr("btrfs_backup_ng.core.machine_eta_v2.subprocess.run", fake_run)
    assert estimate_source_bytes(source) == 10 * 1024**3
    assert len(calls) == 1
    assert calls[0][0] == ["btrfs", "filesystem", "du", "-s", "--raw", str(source)]
    assert calls[0][1]["timeout"] <= 6


def test_estimate_source_fail_closed_if_timeout_or_symlink(monkeypatch, tmp_path):
    import subprocess

    real = tmp_path / "snap"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real)
    assert estimate_source_bytes(link) is None

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 4)

    monkeypatch.setattr("btrfs_backup_ng.core.machine_eta_v2.subprocess.run", timeout)
    assert estimate_source_bytes(real) is None


def test_ballpark_predicts_total_from_references_and_calibrated_compression():
    gib = 1024**3
    members = [
        {"archive": "r", "status": "completed", "source_reference_bytes": 10 * gib},
        {"archive": "home", "status": "sending", "source_reference_bytes": 20 * gib},
        {"archive": "data", "status": "pending", "source_reference_bytes": 5 * gib},
    ]
    actual = {"r": 5 * gib, "home": 4 * gib}
    result = forecast_machine(members, actual, rate_bps=20 * 1024**2)
    assert result["estimate_method"] == "btrfs-extents+measured-compression"
    assert 15 * gib <= result["estimated_total_bytes"] <= 19 * gib
    assert result["estimated_min_bytes"] < result["estimated_total_bytes"]
    assert result["estimated_max_bytes"] > result["estimated_total_bytes"]
    assert result["estimated_percent"] > 0
    assert result["eta_seconds"] > 0
    assert result["estimate_covered_members"] == 3


def test_no_reference_and_no_finished_members_stays_unknown():
    members = [{"archive": "a", "status": "sending"}]
    result = forecast_machine(members, {"a": 1024**3}, rate_bps=1024**2)
    assert result["estimated_total_bytes"] is None
    assert result["eta_seconds"] is None


def test_partial_exceeding_forecast_never_shows_100_percent_until_done():
    members = [
        {"archive": "a", "status": "sending", "source_reference_bytes": 1000000},
    ]
    result = forecast_machine(members, {"a": 2000000}, rate_bps=100000)
    assert result["estimated_total_bytes"] >= 2000000
    assert result["estimated_percent"] < 100
    assert result["eta_seconds"] >= 0


def test_idle_rate_blocks_eta_but_preserves_size_ballpark():
    members = [
        {"archive": "a", "status": "sending", "source_reference_bytes": 10000000}
    ]
    result = forecast_machine(members, {"a": 1024}, rate_bps=0)
    assert result["estimated_total_bytes"] is not None
    assert result["eta_seconds"] is None


def test_unknown_later_volumes_are_marked_low_confidence_not_ignored():
    members = [
        {"archive": "a", "status": "completed", "source_reference_bytes": 1024**3},
        {"archive": "b", "status": "pending"},
    ]
    result = forecast_machine(members, {"a": 700000000}, rate_bps=1_000_000)
    assert result["estimated_total_bytes"] > 700000000
    assert result["estimate_covered_members"] == 1
    assert result["estimate_quality"] == "low"


def test_completed_transfer_uses_actual_total_not_heuristic():
    members = [
        {"archive": "a", "status": "completed", "source_reference_bytes": 10000000}
    ]
    result = forecast_machine(members, {"a": 1000}, rate_bps=0)
    assert result["estimated_total_bytes"] == 1000
    assert result["estimated_percent"] == 100
    assert result["eta_seconds"] == 0


def test_progress_sampler_uses_estimate_without_scanning_source(tmp_path, monkeypatch):
    from btrfs_backup_ng.cli.machine_set_v2 import _machine_progress_measure
    from btrfs_backup_ng.core.machine_eta_v2 import forecast_machine

    source = tmp_path / "source"
    source.mkdir()
    archive = "machine-001"
    tx = "12345678-1234-4234-8234-123456789abc"
    (tmp_path / f"{archive}.btrfs.zst.{tx}.part").write_bytes(b"x" * 900)
    data = {
        "members": [
            {
                "archive": archive,
                "original_mount": str(source),
                "status": "sending",
                "source_reference_bytes": 1000,
            }
        ]
    }
    monkeypatch.setattr(
        "btrfs_backup_ng.core.machine_eta_v2.estimate_source_bytes",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("source scan in monitor")
        ),
    )
    sample = _machine_progress_measure(tmp_path, data, forecast_rate=60)
    assert sample["estimated_total_bytes"] >= sample["bytes_written"]
    assert sample["estimate_method"] == "btrfs-extents-heuristic"
    assert sample["eta_seconds"] is not None
    assert sample["percent"] is None
    assert sample["estimated_percent"] < 100
    assert sample["eta_min_seconds"] is None  # range added by rate sampler
    assert (
        forecast_machine(data["members"], {archive: 900}, rate_bps=60)["eta_seconds"]
        >= 0
    )


def test_progress_source_meter_is_async_and_bounded_at_finalization(
    tmp_path, monkeypatch, capsys
):
    import json
    import threading
    import time
    from btrfs_backup_ng.cli.machine_set_v2 import _live_machine_progress

    entered = threading.Event()
    release = threading.Event()

    def slow_meter(_path):
        entered.set()
        release.wait(5)
        return 10000

    monkeypatch.setattr(
        "btrfs_backup_ng.cli.machine_set_v2.estimate_source_bytes", slow_meter
    )
    record = {
        "members": [
            {
                "archive": "machine-000",
                "status": "sending",
                "original_mount": str(tmp_path),
                "snapshot_path": str(tmp_path),
            }
        ]
    }
    try:
        started = time.monotonic()
        with _live_machine_progress(tmp_path, record, estimate_sources=True):
            assert entered.wait(1)
        assert time.monotonic() - started < 0.8
        release.set()
        time.sleep(0.05)
        events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        assert events[-1]["event"] == "machine_progress"
        assert len(events) <= 2
    finally:
        release.set()


def test_qgroup_optional_fallback_without_enabling_quotas(tmp_path, monkeypatch):
    source = tmp_path / "snapshot"
    source.mkdir()
    commands = []

    def fake_run(argv, **kw):
        commands.append(argv)
        if argv[1] == "filesystem":
            return SimpleNamespace(returncode=1, stdout="", stderr="not supported")
        assert argv[1:] == ["qgroup", "show", "--raw", "-f", str(source)]
        return SimpleNamespace(
            returncode=0,
            stdout="qgroupid         rfer         excl\n--------         ----         ----\n0/257          1000000000    700000000\n",
            stderr="",
        )

    monkeypatch.setattr("btrfs_backup_ng.core.machine_eta_v2.subprocess.run", fake_run)
    assert estimate_source_bytes(source) == 1_000_000_000
    assert len(commands) == 2


def test_qgroup_ambiguous_is_not_guessed(tmp_path, monkeypatch):
    source = tmp_path / "snapshot"
    source.mkdir()

    def fake_run(argv, **kw):
        if argv[1] == "filesystem":
            return SimpleNamespace(returncode=1, stdout="", stderr="not supported")
        return SimpleNamespace(
            returncode=0,
            stdout="qgroupid rfer excl\n0/257 500000 400000\n0/258 700000 600000\n",
            stderr="",
        )

    monkeypatch.setattr("btrfs_backup_ng.core.machine_eta_v2.subprocess.run", fake_run)
    assert estimate_source_bytes(source) is None


def test_async_successful_source_probe_does_not_mutate_machine_catalog(
    tmp_path, monkeypatch
):
    import json
    import time
    from btrfs_backup_ng.cli.machine_set_v2 import _live_machine_progress

    monkeypatch.setattr(
        "btrfs_backup_ng.cli.machine_set_v2.estimate_source_bytes",
        lambda *_a: 1000000,
    )
    member = {
        "archive": "machine-000",
        "status": "sending",
        "original_mount": str(tmp_path),
        "snapshot_path": str(tmp_path),
    }
    data = {"members": [member]}
    original = json.dumps(data, sort_keys=True)
    with _live_machine_progress(tmp_path, data, estimate_sources=True):
        time.sleep(0.05)
    assert json.dumps(data, sort_keys=True) == original
