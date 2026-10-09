"""Real NFS does not necessarily implement renameat2 NOREPLACE."""

from __future__ import annotations

import ctypes
import errno
import os

import pytest

import btrfs_backup_ng.endpoint.resumable_raw as raw


def _unsupported_rename(*_args):
    ctypes.set_errno(errno.EINVAL)
    return -1


def test_nfs_unsupported_rename_falls_back_to_atomic_link(tmp_path, monkeypatch):
    monkeypatch.setattr(raw, "_RENAMEAT2", _unsupported_rename)
    (tmp_path / "source").write_bytes(b"important")
    directory = os.open(tmp_path, os.O_DIRECTORY)
    try:
        raw._rename_noreplace(directory, "source", "final")
        assert not (tmp_path / "source").exists()
        assert (tmp_path / "final").read_bytes() == b"important"
    finally:
        os.close(directory)


def test_nfs_fallback_never_clobbers_existing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(raw, "_RENAMEAT2", _unsupported_rename)
    (tmp_path / "source").write_bytes(b"new")
    (tmp_path / "final").write_bytes(b"old")
    directory = os.open(tmp_path, os.O_DIRECTORY)
    try:
        with pytest.raises(FileExistsError):
            raw._rename_noreplace(directory, "source", "final")
        assert (tmp_path / "final").read_bytes() == b"old"
        assert (tmp_path / "source").read_bytes() == b"new"
    finally:
        os.close(directory)


def test_nfs_fallback_rejects_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(raw, "_RENAMEAT2", _unsupported_rename)
    (tmp_path / "real").write_bytes(b"keep")
    (tmp_path / "source").symlink_to("real")
    directory = os.open(tmp_path, os.O_DIRECTORY)
    try:
        with pytest.raises(ValueError, match="regular"):
            raw._rename_noreplace(directory, "source", "final")
        assert not (tmp_path / "final").exists()
        assert (tmp_path / "real").read_bytes() == b"keep"
    finally:
        os.close(directory)


def test_fallback_without_renameat2_symbol(tmp_path, monkeypatch):
    monkeypatch.setattr(raw, "_RENAMEAT2", None)
    (tmp_path / "source").write_bytes(b"legacy")
    directory = os.open(tmp_path, os.O_DIRECTORY)
    try:
        raw._rename_noreplace(directory, "source", "final")
        assert (tmp_path / "final").read_bytes() == b"legacy"
    finally:
        os.close(directory)


def test_preflight_failure_releases_pins_only_if_no_part_or_manifest(
    tmp_path, monkeypatch
):
    import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli
    from types import SimpleNamespace

    released = []

    class Guard:
        def __init__(self):
            self.directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

        def validate_fd(self, _fd):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            os.close(self.directory_fd)

    monkeypatch.setattr(cli, "_open_guard", lambda *a, **k: (Guard(), "fingerprint"))
    monkeypatch.setattr(
        cli, "_release_snapper_pins", lambda state, m: released.append(m.transfer_id)
    )
    manifest = SimpleNamespace(
        transfer_id="3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        identity={"destination_fingerprint": "fingerprint"},
    )
    params = dict(
        root=tmp_path, name="root", manifest=manifest, state=tmp_path, allow_local=True
    )
    assert cli._release_if_never_started(**params)
    assert len(released) == 1
    part = tmp_path / f"root.btrfs.zst.{manifest.transfer_id}.part"
    part.write_bytes(b"checkpoint")
    assert not cli._release_if_never_started(**params)
    assert len(released) == 1


def test_preflight_failure_preserves_pins_if_guard_cannot_revalidate(
    tmp_path, monkeypatch
):
    import btrfs_backup_ng.cli.checkpoint_v2_cmd as cli
    from types import SimpleNamespace

    monkeypatch.setattr(
        cli,
        "_open_guard",
        lambda *a, **k: (_ for _ in ()).throw(OSError(errno.ESTALE, "NFS stale")),
    )
    released = []
    monkeypatch.setattr(cli, "_release_snapper_pins", lambda *a: released.append(True))
    manifest = SimpleNamespace(
        transfer_id="3b88e8c1-5cb8-4f67-aa10-f9544b6228f0",
        identity={"destination_fingerprint": "fingerprint"},
    )
    assert not cli._release_if_never_started(
        root=tmp_path,
        name="root",
        manifest=manifest,
        state=tmp_path,
        allow_local=False,
    )
    assert released == []
