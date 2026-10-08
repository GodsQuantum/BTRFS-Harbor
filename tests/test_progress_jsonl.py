"""Machine-readable transfer telemetry for Harbor and other API consumers."""

import io
import json

from btrfs_backup_ng.core.progress import (
    FileSizeProgressSampler,
    TransferProgressEvent,
    emit_transfer_progress,
)


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def test_transfer_progress_event_serializes_versioned_plain_json() -> None:
    event = TransferProgressEvent(
        volume="/home",
        snapshot="home-20261008",
        destination="/backup/workstation/home",
        bytes_target=1024,
        bytes_source=None,
        total_estimate=None,
        estimate_kind=None,
        bytes_per_second=512.0,
        elapsed_seconds=2.0,
        eta_seconds=None,
        measurement="raw-target-file",
        certainty="unknown-total",
    )

    raw = event.to_json()
    payload = json.loads(raw)

    assert payload["schema"] == 1
    assert payload["event"] == "transfer_progress"
    assert payload["bytes_target"] == 1024
    assert payload["bytes_source"] is None
    assert payload["eta_seconds"] is None
    assert "\x1b" not in raw


def test_file_sampler_reports_actual_target_bytes_rate_and_elapsed(tmp_path) -> None:
    path = tmp_path / "snapshot.btrfs.zst.part"
    path.write_bytes(b"")
    clock = FakeClock()
    sampler = FileSizeProgressSampler(
        path,
        volume="/home",
        snapshot="home-20261008",
        destination=str(tmp_path),
        now=clock,
    )

    path.write_bytes(b"x" * 1024)
    clock.advance(2.0)
    event = sampler.sample()

    assert event.bytes_target == 1024
    assert event.bytes_per_second == 512.0
    assert event.elapsed_seconds == 2.0
    assert event.total_estimate is None
    assert event.eta_seconds is None
    assert event.certainty == "unknown-total"


def test_file_sampler_only_emits_eta_when_total_is_defensible(tmp_path) -> None:
    path = tmp_path / "snapshot.btrfs.zst.part"
    path.write_bytes(b"")
    clock = FakeClock()
    sampler = FileSizeProgressSampler(
        path,
        volume="/",
        snapshot="root-20261008",
        destination=str(tmp_path),
        total_estimate=4096,
        estimate_kind="snapshot-logical",
        now=clock,
    )

    path.write_bytes(b"x" * 1024)
    clock.advance(2.0)
    event = sampler.sample()

    assert event.bytes_target == 1024
    assert event.bytes_per_second == 512.0
    assert event.total_estimate == 4096
    assert event.eta_seconds == 6.0
    assert event.certainty == "estimated-total"


def test_file_sampler_does_not_invent_rate_when_no_time_elapsed(tmp_path) -> None:
    path = tmp_path / "snapshot.part"
    path.write_bytes(b"x" * 512)
    clock = FakeClock()
    sampler = FileSizeProgressSampler(
        path,
        volume="/srv",
        snapshot="srv-20261008",
        destination=str(tmp_path),
        now=clock,
    )

    event = sampler.sample()

    assert event.bytes_target == 512
    assert event.bytes_per_second is None
    assert event.eta_seconds is None


def test_emit_transfer_progress_is_one_json_object_per_line() -> None:
    stream = io.StringIO()
    event = TransferProgressEvent(
        volume="/home",
        snapshot="home-20261008",
        destination="/backup/home",
        bytes_target=2048,
        bytes_per_second=1024.0,
        elapsed_seconds=2.0,
        measurement="raw-target-file",
        certainty="unknown-total",
    )

    emit_transfer_progress(event, stream=stream, enabled=True)

    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["bytes_target"] == 2048


def test_raw_receive_starts_out_of_band_target_file_monitor(
    tmp_path, monkeypatch
) -> None:
    import btrfs_backup_ng.endpoint.raw as raw_module
    from btrfs_backup_ng.endpoint.raw import RawEndpoint

    endpoint = RawEndpoint(config={"path": str(tmp_path), "compress": "zstd"})
    fake_process = object()
    captured = {}

    monkeypatch.setattr(
        endpoint, "_execute_pipeline", lambda pipeline, stdin: fake_process
    )
    monkeypatch.setattr(raw_module, "progress_jsonl_enabled", lambda: True)

    class FakeMonitor:
        def __init__(self, sampler, process):
            captured["sampler"] = sampler
            captured["process"] = process
            captured["started"] = False

        def start(self):
            captured["started"] = True
            return self

        def stop(self, *, emit_final=True, timeout=2.0):
            captured["stopped"] = emit_final

    monkeypatch.setattr(raw_module, "FileSizeProgressMonitor", FakeMonitor)

    endpoint.set_progress_context(
        {
            "volume": "/home",
            "destination": str(tmp_path),
            "total_estimate": 4096,
            "estimate_kind": "snapshot-logical",
        }
    )
    result = endpoint.receive(object(), "home-20261008")

    assert result is fake_process
    assert captured["started"] is True
    assert captured["process"] is fake_process
    assert captured["sampler"].volume == "/home"
    assert captured["sampler"].snapshot == "home-20261008"
    assert captured["sampler"].destination == str(tmp_path)
    assert captured["sampler"].total_estimate is None
    assert captured["sampler"].estimate_kind is None
    assert endpoint._progress_monitor is not None
