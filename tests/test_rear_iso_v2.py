"""ISO builder is mount-pinned, no-clobber and refuses incomplete EFI sources."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from btrfs_backup_ng.core.rear_iso_v2 import (
    _check_catalog,
    _copy_pinned,
    build_rescue_iso,
)
from btrfs_backup_ng.endpoint.mount_guard_v2 import MountGuard, capture_mount_identity

IDENTITY = "11111111-1111-4111-8111-111111111111"


def iso_fixture(path: Path, *, marker: bool = True) -> bytes:
    data = bytearray(65536 + 4096)
    if marker:
        data[32769:32774] = b"CD001"
    data[-50:] = b"privately generated rescue ISO" + b"x" * 21
    path.write_bytes(bytes(data))
    return bytes(data)


def catalog() -> dict:
    return {
        "set_id": IDENTITY,
        "status": "completed_btrfs_and_boot_files",
        "members": [{"original_mount": "/", "status": "completed"}],
        "boot_members": [
            {"mount_point": "/boot/efi", "filesystem": "vfat", "status": "completed"}
        ],
    }


def test_rear_iso_copy_checksum_and_destination_safety(tmp_path: Path) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    iso = tmp_path / "local.iso"
    original = iso_fixture(iso)
    with MountGuard(destination, capture_mount_identity(destination)) as guard:
        report = _copy_pinned(iso, guard, IDENTITY)
    assert report["iso_created"] is True and report["boot_tested"] is False
    assert report["sha256"] == hashlib.sha256(original).hexdigest()
    assert (destination / report["name"]).read_bytes() == original
    assert not list(destination.glob(".*.tmp"))


def test_fail_closed_for_invalid_iso_magic_and_preserve_other_files(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    (destination / "important").write_text("user data")
    iso = tmp_path / "garbage.iso"
    iso_fixture(iso, marker=False)
    with MountGuard(destination, capture_mount_identity(destination)) as guard:
        with pytest.raises(ValueError, match="ISO9660"):
            _copy_pinned(iso, guard, IDENTITY)
    assert [p.name for p in destination.iterdir()] == ["important"]


def test_no_boot_archive_no_rescue_iso() -> None:
    value = catalog()
    value["status"] = "completed_btrfs_only"
    with pytest.raises(ValueError, match="completed machine backup"):
        _check_catalog(value)
    value = catalog()
    value["boot_members"] = []
    with pytest.raises(ValueError, match="no FAT32 EFI"):
        _check_catalog(value)


def test_builder_invokes_rear_with_private_config_and_uses_only_fresh_output(
    tmp_path: Path,
) -> None:
    target = tmp_path / "destination"
    target.mkdir()
    output = tmp_path / "local-rear-output"
    output.mkdir()
    old = output / "old.iso"
    iso_fixture(old)
    created = output / "new.iso"

    def mock_rear(argv, **kwargs):
        assert argv == [
            "/usr/sbin/rear",
            "-c",
            "/usr/share/btrfs-harbor/rear",
            "mkrescue",
        ]
        iso_fixture(created)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with MountGuard(target, capture_mount_identity(target)) as guard:
        result = build_rescue_iso(
            target,
            catalog(),
            guard,
            runner=mock_rear,
            output=output,
            check_installation=False,
        )
    assert result["iso_created"] is True
    assert result["boot_tested"] is False
    assert len(list(target.glob("*.iso"))) == 1
    assert old.exists()


def test_first_rear_iso_build_when_output_is_initially_missing(tmp_path: Path) -> None:
    destination = tmp_path / "safe"
    destination.mkdir()
    output = tmp_path / "first-rear-build"

    def first_build(argv, **kwargs):
        output.mkdir()
        iso_fixture(output / "rear-first.iso")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with MountGuard(destination, capture_mount_identity(destination)) as guard:
        result = build_rescue_iso(
            destination,
            catalog(),
            guard,
            runner=first_build,
            output=output,
            check_installation=False,
        )
    assert result["iso_created"] is True
    assert len(list(destination.glob("*.iso"))) == 1


def test_rear_iso_output_symlink_refused_before_any_write(tmp_path: Path) -> None:
    destination = tmp_path / "safe"
    destination.mkdir()
    output = tmp_path / "foreign"
    output.mkdir()
    link = tmp_path / "rear-output-symlink"
    link.symlink_to(output)
    with MountGuard(destination, capture_mount_identity(destination)) as guard:
        with pytest.raises(ValueError, match="symlinked ReaR output"):
            build_rescue_iso(
                destination,
                catalog(),
                guard,
                output=link,
                check_installation=False,
            )
    assert not list(destination.iterdir())


def test_failing_rear_build_never_writes_backup_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    output = tmp_path / "rear"
    output.mkdir()

    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 5, stdout="", stderr="unsupported UEFI"
        )

    with MountGuard(target, capture_mount_identity(target)) as guard:
        with pytest.raises(RuntimeError, match="unsupported UEFI"):
            build_rescue_iso(
                target,
                catalog(),
                guard,
                runner=fake,
                output=output,
                check_installation=False,
            )
    assert not list(target.iterdir())
