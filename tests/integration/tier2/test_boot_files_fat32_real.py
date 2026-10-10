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
        (efi / "machine-test-id").write_text("source preserved")
    finally:
        if mounted:
            subprocess.run(["umount", str(efi)], check=True)
        subprocess.run(["losetup", "-d", loop], check=True)
