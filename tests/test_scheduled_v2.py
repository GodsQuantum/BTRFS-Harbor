"""Scheduled profile v2 uses existing checkpoint and machine-set engines."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from btrfs_backup_ng.cli import scheduled_v2 as schedules

PROFILE = "88888888-8888-4888-8888-888888888888"


class Guard:
    directory_fd = 23
    checks = 0

    def validate_fd(self, fd: int) -> None:
        assert fd == self.directory_fd
        self.checks += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def request(root: Path, source: str | None = None, config: str | None = None):
    return argparse.Namespace(
        target=str(root),
        source=source,
        snapper_config=config,
        profile_id=PROFILE,
        name_prefix=None,
        state_dir=None,
        checkpoint_size_mib=128,
        performance="balanced",
        allow_local=True,
        experimental=True,
    )


@pytest.fixture
def ready(monkeypatch):
    g = Guard()
    monkeypatch.setattr(schedules, "_open_guard", lambda *args, **kw: (g, "stable"))
    monkeypatch.setattr(schedules, "_list_v2", lambda *args, **kw: [])
    return g


def test_first_native_scheduled_source_uses_durable_group_journal(
    tmp_path: Path, monkeypatch, ready
):
    source = tmp_path / "source"
    source.mkdir()
    calls = []
    monkeypatch.setattr(
        schedules, "execute_machine_set", lambda req: calls.append(req) or 0
    )
    assert (
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, str(source))) == 0
    )
    assert len(calls) == 1
    assert calls[0].checkpoint_action == "set-start"
    assert calls[0].source == [str(source)]
    assert ready.checks


def test_native_crash_resumes_existing_machine_set_not_new_snapshot(
    tmp_path: Path, monkeypatch, ready
):
    source = tmp_path / "source"
    source.mkdir()
    set_id = str(uuid.uuid4())
    (tmp_path / f".harbor-machine-set-{set_id}.json").write_text("{}")
    monkeypatch.setattr(
        schedules,
        "_read",
        lambda *_: {
            "profile_id": PROFILE,
            "status": "pending",
            "members": [{"original_mount": str(source)}],
        },
    )
    calls = []
    monkeypatch.setattr(
        schedules, "execute_machine_set", lambda req: calls.append(req) or 0
    )
    assert (
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, str(source))) == 0
    )
    assert len(calls) == 1
    assert calls[0].checkpoint_action == "set-resume"
    assert calls[0].set_id == set_id


def test_snapper_partial_resume_before_selecting_new_source(
    tmp_path: Path, monkeypatch, ready
):
    transfer_id = str(uuid.uuid4())
    monkeypatch.setattr(
        schedules,
        "_list_v2",
        lambda *_a, **_k: [
            {"state": "paused", "transfer_id": transfer_id, "name": "existing-name"},
        ],
    )
    monkeypatch.setattr(
        schedules,
        "read_manifest",
        lambda _: SimpleNamespace(
            identity={
                "profile_id": PROFILE,
                "snapper_config": "root",
            },
            transfer_id=transfer_id,
        ),
    )
    received = []
    monkeypatch.setattr(
        schedules, "execute_checkpoint_v2", lambda req: received.append(req) or 0
    )
    assert (
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, config="root")) == 0
    )
    assert len(received) == 1
    assert received[0].checkpoint_action == "resume"
    assert received[0].name == "existing-name"
    assert received[0].transfer_id == transfer_id


def test_ambiguous_snapper_checkpoint_blocks_duplicate(
    tmp_path: Path, monkeypatch, ready
):
    snapshots = [
        {"state": "paused", "transfer_id": str(uuid.uuid4()), "name": f"job{i}"}
        for i in range(2)
    ]
    monkeypatch.setattr(schedules, "_list_v2", lambda *_a, **_k: snapshots)
    monkeypatch.setattr(
        schedules,
        "read_manifest",
        lambda path: SimpleNamespace(
            transfer_id=next(
                item["transfer_id"]
                for item in snapshots
                if item["transfer_id"] in str(path)
            ),
            identity={"profile_id": PROFILE, "snapper_config": "root"},
        ),
    )
    with pytest.raises(ValueError, match="multiple unfinished"):
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, config="root"))


def test_old_manifest_without_archive_name_refuses_duplicate(
    tmp_path: Path, monkeypatch, ready
):
    transfer_id = str(uuid.uuid4())
    monkeypatch.setattr(
        schedules,
        "_list_v2",
        lambda *_a, **_k: [
            {"state": "paused", "transfer_id": transfer_id, "name": None},
        ],
    )
    monkeypatch.setattr(
        schedules,
        "read_manifest",
        lambda _: SimpleNamespace(
            identity={"profile_id": PROFILE, "snapper_config": "root"},
            transfer_id=transfer_id,
        ),
    )
    with pytest.raises(ValueError, match="no recoverable archive name"):
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, config="root"))


def test_snapper_starts_new_uuid_unique_named_transaction(
    tmp_path: Path, monkeypatch, ready
):
    monkeypatch.setattr(schedules, "_resolve_source_choice", lambda *_: None)
    calls = []
    monkeypatch.setattr(
        schedules, "execute_checkpoint_v2", lambda req: calls.append(req) or 0
    )
    assert (
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, config="my.root"))
        == 0
    )
    assert len(calls) == 1
    assert calls[0].checkpoint_action == "start"
    assert calls[0].source_mode == "latest-snapper"
    assert calls[0].name.startswith("my-root-")


def test_invalid_profile_and_native_source_rejected_before_writing(
    tmp_path: Path, ready
):
    req = request(tmp_path, source="/this/does/not/exist")
    with pytest.raises(ValueError, match="real absolute"):
        schedules.execute_scheduled_checkpoint_v2(req)
    req = request(tmp_path, config="root")
    req.profile_id = "../../bad"
    with pytest.raises(ValueError):
        schedules.execute_scheduled_checkpoint_v2(req)


def test_no_change_in_latest_snapper_is_successful(
    tmp_path: Path, monkeypatch, ready, capsys
):
    def already_saved(*args):
        raise ValueError(
            "latest stable Snapper snapshot already backed up; no new UUID"
        )

    monkeypatch.setattr(schedules, "_resolve_source_choice", already_saved)
    assert (
        schedules.execute_scheduled_checkpoint_v2(request(tmp_path, config="root")) == 0
    )
    import json

    assert json.loads(capsys.readouterr().out)["status"] == "no-change"
