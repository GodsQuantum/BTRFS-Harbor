"""Root-only disposable loop-backed vfat ESP + real Btrfs Harbor set recovery.

Every mutation is inside a GitHub privileged integration-test container and
its temporary loop devices. Never run this on Cloud9 PVE or user workstations.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]

PROFILE = "96969696-9696-4969-8969-969696969696"


def _cli(*args: str) -> dict:
    process = subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", "raw", "checkpoint-v2", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert process.returncode == 0, process.stdout + "\n" + process.stderr
    return json.loads(process.stdout.splitlines()[-1])


def test_real_btrfs_plus_real_fat32_efi_staged_restore(
    btrfs_volume: Path, tmp_path: Path
) -> None:
    efi = Path("/boot/efi")
    if efi.is_mount():
        pytest.skip("test refuses to alter an existing /boot/efi mount")
    efi.mkdir(parents=True, exist_ok=True)
    image = tmp_path / "fat32-esp.img"
    subprocess.run(["truncate", "-s", "64M", str(image)], check=True)
    subprocess.run(
        ["mkfs.vfat", "-F", "32", str(image)], check=True, capture_output=True
    )
    loop = subprocess.run(
        ["losetup", "--find", "--show", str(image)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    mounted = False
    try:
        subprocess.run(["mount", loop, str(efi)], check=True, capture_output=True)
        mounted = True
        folder = efi / "EFI" / "BOOT"
        folder.mkdir(parents=True)
        (folder / "BOOTX64.EFI").write_bytes(b"harbor-efi-test-boot-file")
        (efi / "machine-test-id").write_text("efi fixture")
        btrfs_source = btrfs_volume / "rootfs"
        subprocess.run(["btrfs", "subvolume", "create", str(btrfs_source)], check=True)
        (btrfs_source / "fstab").write_text("fixture-root")
        original_root_uuid = subprocess.check_output(
            [
                "findmnt",
                "--noheadings",
                "--output",
                "UUID",
                "--mountpoint",
                str(btrfs_volume),
            ],
            text=True,
        ).strip()
        original_efi_uuid = subprocess.check_output(
            ["blkid", "--output", "value", "--match-tag", "UUID", loop], text=True
        ).strip()
        (btrfs_source / "etc").mkdir()
        (btrfs_source / "etc/fstab").write_text(
            f"UUID={original_root_uuid} / btrfs defaults 0 0\n"
            f"UUID={original_efi_uuid} /boot/efi vfat umask=0077 0 2\n"
        )

        archive = tmp_path / "backup"
        archive.mkdir()
        journal = tmp_path / "journal"
        first = _cli(
            "set-start",
            "--target",
            str(archive),
            "--profile-id",
            PROFILE,
            "--source",
            str(btrfs_source),
            "--state-dir",
            str(journal),
            "--allow-local",
            "--experimental",
        )
        assert first["status"] == "completed_btrfs_only"
        catalog = archive / f".harbor-machine-set-{first['set_id']}.json"
        data = json.loads(catalog.read_text())
        data["boot_members"] = [
            {
                "archive": f".harbor-boot-{first['set_id']}-000.tar.gz",
                "mount_point": "/boot/efi",
                "source": loop,
                "filesystem": "vfat",
                "status": "pending",
            }
        ]
        data["coverage"] = "btrfs-and-boot-files"
        data["status"] = "pending"
        catalog.write_text(json.dumps(data))
        captured = _cli(
            "set-resume",
            "--target",
            str(archive),
            "--set-id",
            first["set_id"],
            "--state-dir",
            str(journal),
            "--allow-local",
            "--experimental",
        )
        assert captured["status"] == "completed_btrfs_and_boot_files"
        assert captured["bootable"] is False
        assert captured["boot_partitions"][0]["status"] == "completed"
        assert not list(archive.glob("*.tmp"))
        saved = json.loads(catalog.read_text())
        assert saved["boot_members"][0]["bytes"] > 0
        # The explicit-source integration fixture is not mounted at '/'.
        # Model the same snapshot as the real automatic root inventory would
        # record, while preserving its actual sent UUID and restore bytes.
        from btrfs_backup_ng.core.native_snapshots import NATIVE_FOLDER

        saved["members"][0]["original_mount"] = "/"
        snapshot_name = Path(saved["members"][0]["snapshot_path"]).name
        saved["members"][0]["snapshot_path"] = f"/{NATIVE_FOLDER}/{snapshot_name}"
        saved["disk_layout"] = {
            "blockdevices": [
                {
                    "path": "/dev/test-btrfs",
                    "uuid": original_root_uuid,
                    "partuuid": None,
                    "mountpoints": ["/"],
                },
                {
                    "path": loop,
                    "uuid": original_efi_uuid,
                    "partuuid": None,
                    "mountpoints": ["/boot/efi"],
                },
            ]
        }
        catalog.write_text(json.dumps(saved))
        plan_retention = _cli(
            "set-retention-plan",
            "--target",
            str(archive),
            "--profile-id",
            PROFILE,
            "--allow-local",
            "--experimental",
        )
        assert plan_retention["deletion_performed"] is False
        staged = btrfs_volume / "restored"
        staged.mkdir()
        plan = _cli(
            "set-restore",
            "--target",
            str(archive),
            "--set-id",
            first["set_id"],
            "--staging",
            str(staged),
            "--allow-local",
            "--experimental",
        )
        assert plan["dry_run"] is True
        assert not list(staged.iterdir())
        result = _cli(
            "set-restore",
            "--target",
            str(archive),
            "--set-id",
            first["set_id"],
            "--staging",
            str(staged),
            "--allow-local",
            "--experimental",
            "--confirm",
        )
        assert result["restored"] is True and result["bootable"] is False
        assert (staged / "boot-files/boot-000/EFI/BOOT/BOOTX64.EFI").read_bytes() == (
            b"harbor-efi-test-boot-file"
        )
        assert any(p.read_text() == "fixture-root" for p in staged.rglob("fstab"))
        # True portable destination-only restore: the origin export and mount
        # path have changed, and the original backed-up computer is NOT used.
        # Read-only recovery must not create/update a target lock, journal,
        # directory, checksum or metadata on this relocated backup disk.
        import shutil

        relocated = tmp_path / "relocated-backup"
        shutil.copytree(archive, relocated)
        before = {
            item.name: (item.stat().st_size, item.stat().st_mtime_ns)
            for item in relocated.iterdir()
        }
        alternate = btrfs_volume / "restored-from-relocated"
        alternate.mkdir()
        from_changed_path = _cli(
            "set-restore",
            "--target",
            str(relocated),
            "--set-id",
            first["set_id"],
            "--staging",
            str(alternate),
            "--allow-local",
            "--experimental",
            "--confirm",
        )
        assert from_changed_path["restored"] is True
        assert (
            alternate / "boot-files/boot-000/EFI/BOOT/BOOTX64.EFI"
        ).read_bytes() == (b"harbor-efi-test-boot-file")
        assert before == {
            item.name: (item.stat().st_size, item.stat().st_mtime_ns)
            for item in relocated.iterdir()
        }
        # Actual ReaR bridge: a DIFFERENT blank Btrfs loopback filesystem
        # and a DIFFERENT FAT32 loopback ESP are mounted in this disposable
        # privileged GitHub CI container. Never on Cloud9 or a workstation.
        from .conftest import LoopbackBtrfs

        rear_root = Path("/mnt/local")
        assert not rear_root.is_mount()
        rear_root.mkdir(parents=True, exist_ok=True)
        rear_marker = Path("/etc/rear/rescue.conf")
        assert not rear_marker.exists()
        rear_marker.parent.mkdir(parents=True, exist_ok=True)
        with LoopbackBtrfs(size_mb=256, label="rear-fresh") as fresh:
            subprocess.run(["mount", "--bind", str(fresh), str(rear_root)], check=True)
            rear_esp_loop = None
            rear_efi_mounted = False
            try:
                (rear_root / "etc").mkdir()
                (rear_root / "boot/efi").mkdir(parents=True)
                rear_esp_image = tmp_path / "fresh-esp.img"
                subprocess.run(
                    ["truncate", "-s", "64M", str(rear_esp_image)], check=True
                )
                subprocess.run(
                    ["mkfs.vfat", "-F", "32", str(rear_esp_image)],
                    check=True,
                    capture_output=True,
                )
                rear_esp_loop = subprocess.check_output(
                    ["losetup", "--find", "--show", str(rear_esp_image)], text=True
                ).strip()
                subprocess.run(
                    ["mount", rear_esp_loop, str(rear_root / "boot/efi")], check=True
                )
                rear_efi_mounted = True
                rear_marker.write_text("# ephemeral privileged runner fixture\n")
                try:
                    copied = _cli(
                        "set-rear-copy",
                        "--target",
                        str(archive),
                        "--set-id",
                        first["set_id"],
                        "--staging",
                        str(staged),
                        "--allow-local",
                        "--experimental",
                        "--confirm",
                    )
                finally:
                    rear_marker.unlink()
                assert copied["copied_to_rear"] is True
                assert copied["bootable"] is False
                assert (rear_root / "fstab").read_text() == "fixture-root"
                assert (rear_root / "boot/efi/EFI/BOOT/BOOTX64.EFI").read_bytes() == (
                    b"harbor-efi-test-boot-file"
                )
                new_root_uuid = subprocess.check_output(
                    [
                        "findmnt",
                        "--noheadings",
                        "--output",
                        "UUID",
                        "--mountpoint",
                        str(rear_root),
                    ],
                    text=True,
                ).strip()
                new_esp_uuid = subprocess.check_output(
                    [
                        "blkid",
                        "--output",
                        "value",
                        "--match-tag",
                        "UUID",
                        rear_esp_loop,
                    ],
                    text=True,
                ).strip()
                written_fstab = (rear_root / "etc/fstab").read_text()
                assert f"UUID={new_root_uuid} / btrfs" in written_fstab
                assert f"UUID={new_esp_uuid} /boot/efi vfat" in written_fstab
                assert new_root_uuid != original_root_uuid
                assert new_esp_uuid != original_efi_uuid
            finally:
                if rear_marker.exists():
                    rear_marker.unlink()
                if rear_efi_mounted:
                    subprocess.run(["umount", str(rear_root / "boot/efi")], check=True)
                if rear_esp_loop:
                    subprocess.run(["losetup", "-d", rear_esp_loop], check=True)
                subprocess.run(["umount", str(rear_root)], check=True)
        (efi / "machine-test-id").write_text("source preserved")
    finally:
        if mounted:
            subprocess.run(["umount", str(efi)], check=True)
        subprocess.run(["losetup", "-d", loop], check=True)
