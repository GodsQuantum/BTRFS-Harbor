"""EFI/boot files are archived atomically without modifying source devices."""

from __future__ import annotations

import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from btrfs_backup_ng.core import boot_files_v2 as boot
from btrfs_backup_ng.endpoint.mount_guard_v2 import MountGuard, capture_mount_identity


def test_discovery_only_distinct_boot_partitions() -> None:
    rows = [
        {"mount_point": "/", "filesystem": "btrfs", "source": "/dev/root"},
        {"mount_point": "/boot", "filesystem": "ext4", "source": "/dev/vda2"},
        {"mount_point": "/boot/efi", "filesystem": "vfat", "source": "/dev/vda1"},
        {"mount_point": "/home", "filesystem": "xfs", "source": "/dev/vdb1"},
        {"mount_point": "/boot/firmware", "filesystem": "btrfs", "source": "/dev/vdc1"},
    ]
    assert [r["mount_point"] for r in boot.discover_boot_mounts(rows)] == [
        "/boot",
        "/boot/efi",
    ]


def test_discovery_rejects_duplicate_ambiguous_and_network_boot_mount() -> None:
    mount = {"mount_point": "/efi", "filesystem": "vfat", "source": "/dev/vda1"}
    with pytest.raises(ValueError, match="duplicate"):
        boot.discover_boot_mounts([mount, mount])
    with pytest.raises(ValueError, match="non-block"):
        boot.discover_boot_mounts([{**mount, "source": "server:/efi"}])
    with pytest.raises(ValueError, match="unsupported"):
        boot.discover_boot_mounts([{**mount, "filesystem": "ntfs3"}])


def _capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "simulated-efi"
    source.mkdir()
    (source / "EFI/Boot").mkdir(parents=True)
    (source / "EFI/Boot/BOOTX64.EFI").write_bytes(b"UEFI EFI boot fixture" * 100)
    root = tmp_path / "destination"
    root.mkdir()
    member = {
        "archive": ".harbor-boot-12345678-1234-4234-8234-123456789abc-000.tar.gz",
        "mount_point": str(source),
        "source": "/dev/simulated-vda1",
        "filesystem": "vfat",
        "status": "pending",
    }
    identity = capture_mount_identity(source)
    monkeypatch.setattr(boot, "_source_identity", lambda _member: identity)
    with MountGuard(root, capture_mount_identity(root)) as guard:
        saved = boot.capture_boot_member(root, guard, member)
    return root, saved


def test_atomic_efi_archive_checksum_and_staged_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, saved = _capture(tmp_path, monkeypatch)
    assert saved["status"] == "completed"
    assert (
        saved["sha256"]
        == hashlib.sha256((root / saved["archive"]).read_bytes()).hexdigest()
    )
    assert boot.verify_boot_member(root, saved)
    assert not list(root.glob("*.tmp"))
    stage = tmp_path / "restored-efi"
    stage.mkdir()
    plan = boot.stage_boot_members(root, [saved], stage)
    assert len(plan) == 1
    assert (stage / "boot-000/EFI/Boot/BOOTX64.EFI").read_bytes() == (
        b"UEFI EFI boot fixture" * 100
    )
    assert not list(tmp_path.glob("*.img")), "never write a partition"


def test_archive_corruption_and_symlink_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, saved = _capture(tmp_path, monkeypatch)
    path = root / saved["archive"]
    path.write_bytes(path.read_bytes()[:-1] + b"x")
    assert not boot.verify_boot_member(root, saved)
    stage = tmp_path / "target"
    stage.mkdir()
    with pytest.raises(ValueError, match="corrupt"):
        boot.stage_boot_members(root, [saved], stage)
    path.unlink()
    path.symlink_to(tmp_path / "source")
    with pytest.raises(OSError):
        boot.verify_boot_member(root, saved)


def test_stage_rejects_existing_data_even_when_archive_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, saved = _capture(tmp_path, monkeypatch)
    stage = tmp_path / "target"
    stage.mkdir()
    (stage / "existing-user-data").write_text("must remain")
    with pytest.raises(ValueError, match="empty"):
        boot.stage_boot_members(root, [saved], stage)
    assert (stage / "existing-user-data").read_text() == "must remain"


def test_stage_rejects_tar_path_escape(tmp_path: Path) -> None:
    archive = tmp_path / "invalid.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        item = tarfile.TarInfo("../../outside")
        payload = b"forbidden"
        item.size = len(payload)
        stream.addfile(item, io.BytesIO(payload))
    with pytest.raises(ValueError, match="unsafe"):
        boot._validate_tar_safety(archive)


def test_fstab_unmounted_esp_is_error_instead_of_false_success() -> None:
    rows = [{"mount_point": "/", "filesystem": "btrfs", "source": "/dev/vda2"}]
    with pytest.raises(ValueError, match="not mounted: /boot/efi"):
        boot.assert_configured_boot_mounts_present(
            rows, "UUID=abcd /boot/efi vfat umask=0077 0 2\n"
        )
    boot.assert_configured_boot_mounts_present(
        rows
        + [{"mount_point": "/boot/efi", "filesystem": "vfat", "source": "/dev/vda1"}],
        "UUID=abcd /boot/efi vfat umask=0077 0 2\n",
    )


def test_sigkill_residue_cleaned_without_touching_foreign_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "efi"
    source.mkdir()
    (source / "BOOTX64.EFI").write_bytes(b"binary")
    dest = tmp_path / "backups"
    dest.mkdir()
    member = {
        "archive": ".harbor-boot-12345678-1234-4234-8234-123456789abc-000.tar.gz",
        "mount_point": str(source),
        "source": "/dev/loop123",
        "filesystem": "vfat",
        "status": "pending",
    }
    orphan = dest / f"{member['archive']}.{'a' * 32}.tmp"
    orphan.write_bytes(b"partial-tar-from-killed-backup")
    foreign = dest / "unrelated-user-document"
    foreign.write_text("preserve me")
    identity = capture_mount_identity(source)
    monkeypatch.setattr(boot, "_source_identity", lambda _: identity)
    with MountGuard(dest, capture_mount_identity(dest)) as guard:
        saved = boot.capture_boot_member(dest, guard, member)
    assert boot.verify_boot_member(dest, saved)
    assert not orphan.exists()
    assert foreign.read_text() == "preserve me"
