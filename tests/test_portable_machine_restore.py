"""Portable restore may relocate backup mount; mutating actions never may."""

from __future__ import annotations

import argparse
import uuid
from pathlib import Path

import pytest

from btrfs_backup_ng.cli import machine_set_v2 as machine


class ReadGuard:
    directory_fd = 17
    checked = 0

    def validate_fd(self, fd: int) -> None:
        assert fd == self.directory_fd
        self.checked += 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        pass


def request(root: Path, action: str) -> argparse.Namespace:
    return argparse.Namespace(
        checkpoint_action=action,
        target=str(root),
        set_id="74f78258-4ff3-45e1-9840-df1a4db31cef",
        allow_local=True,
        experimental=True,
        staging=str(root / "not-created-staging"),
        confirm=False,
        state_dir=None,
    )


def test_restore_with_new_destination_path_is_read_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = ReadGuard()
    monkeypatch.setattr(
        machine, "_open_guard", lambda *_args, **_kw: (guard, "new-mounted-export")
    )
    monkeypatch.setattr(
        machine,
        "_read",
        lambda *_args: {
            "destination_fingerprint": "original-machine-export",
            "set_id": str(uuid.uuid4()),
        },
    )

    def forbidden_lock(*_args):
        pytest.fail("recovery must never create a lock on the source backup volume")

    monkeypatch.setattr(machine, "_exclusive_set_lock", forbidden_lock)
    restored = []
    monkeypatch.setattr(
        machine,
        "_restore",
        lambda root, record, args: restored.append((root, args.checkpoint_action)) or 0,
    )
    result = machine.execute_machine_set(request(tmp_path, "set-restore"))
    assert result == 0
    assert restored == [(tmp_path, "set-restore")]
    assert guard.checked


def test_backup_mutation_with_changed_mount_is_rejected_without_new_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    guard = ReadGuard()
    monkeypatch.setattr(
        machine, "_open_guard", lambda *_a, **_kw: (guard, "new-mounted-export")
    )
    monkeypatch.setattr(
        machine,
        "_read",
        lambda *_a: {"destination_fingerprint": "original-machine-export"},
    )
    monkeypatch.setattr(
        machine,
        "_exclusive_set_lock",
        lambda *_a: pytest.fail("should fail before attempting lock creation"),
    )
    for action in ("set-resume", "set-rescue-iso"):
        with pytest.raises(ValueError, match="fingerprint changed"):
            machine.execute_machine_set(request(tmp_path, action))
    assert not list(tmp_path.iterdir())


def test_portable_restore_requires_valid_existing_backup_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="already exist"):
        machine.execute_machine_set(request(tmp_path / "not-mounted", "set-restore"))
