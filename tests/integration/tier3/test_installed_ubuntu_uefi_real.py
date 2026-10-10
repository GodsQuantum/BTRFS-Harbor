"""Privileged CI-only RESTORED INSTALLED UBUNTU 24.04 + TWO boots via OVMF.

No physical devices or Cloud9 mounts are touched. This job ONLY runs on
an ephemeral GitHub hosted Ubuntu runner with disposable sparse image files.

Unlike the BusyBox test, the restored system is an actual Ubuntu 24.04
installation with systemd as PID 1 and a multi-user.target acceptance service.
The runner supplies the kernel/initramfs and EFI GRUB: this does NOT qualify
arbitrary distro bootloader/LUKS/UKI/Secure Boot combinations.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from btrfs_backup_ng.core.boot_files_v2 import capture_boot_member
from btrfs_backup_ng.core.native_snapshots import NATIVE_FOLDER
from btrfs_backup_ng.endpoint.mount_guard_v2 import MountGuard, capture_mount_identity

ROOT = Path("/mnt/local")
ESP = Path("/boot/efi")
PROFILE = str(uuid.UUID("e454e15c-ea64-4a93-bb47-e6c3bbf20857"))


def run(*args: str, timeout: int = 120, capture: bool = False) -> str:
    result = subprocess.run(
        args, check=True, text=True, capture_output=capture, timeout=timeout
    )
    return result.stdout.strip() if capture else ""


def cli(*args: str) -> dict:
    process = subprocess.run(
        [sys.executable, "-m", "btrfs_backup_ng", "raw", "checkpoint-v2", *args],
        capture_output=True,
        text=True,
        timeout=480,
        check=True,
    )
    return json.loads(process.stdout.strip().splitlines()[-1])


def new_loop(image: Path, size: str, *, partitions: bool = False) -> str:
    run("truncate", "-s", size, str(image))
    params = ["losetup", "--find", "--show"]
    if partitions:
        params.append("--partscan")
    return run(*params, str(image), capture=True)


def pair() -> tuple[str, str]:
    base = Path("/usr/share/OVMF")
    for code, vars in (
        ("OVMF_CODE_4M.fd", "OVMF_VARS_4M.fd"),
        ("OVMF_CODE.fd", "OVMF_VARS.fd"),
    ):
        if (base / code).is_file() and (base / vars).is_file():
            return str(base / code), str(base / vars)
    raise RuntimeError("missing matched OVMF firmware pair")


def boot_twice(image: Path, scratch: Path) -> None:
    code, variables = pair()
    copied = scratch / "OVMF_VARS.fd"
    shutil.copy2(variables, copied)
    for index in (1, 2):
        log = scratch / f"boot-{index}.serial.log"
        qemu = [
            "qemu-system-x86_64",
            "-machine",
            "q35,accel=tcg",
            "-cpu",
            "max",
            "-smp",
            "2",
            "-m",
            "2048",
            "-drive",
            f"if=pflash,format=raw,readonly=on,file={code}",
            "-drive",
            f"if=pflash,format=raw,file={copied}",
            "-drive",
            f"if=virtio,format=raw,file={image}",
            "-boot",
            "order=c",
            "-display",
            "none",
            "-serial",
            f"file:{log}",
            "-monitor",
            "none",
            "-no-reboot",
        ]
        try:
            subprocess.run(qemu, check=False, timeout=155, capture_output=True)
        except subprocess.TimeoutExpired:
            pass
        result = log.read_text(errors="replace") if log.exists() else ""
        assert "HARBOR_INSTALLED_UBUNTU_RESTORED_OK" in result, (
            f"installed Ubuntu did not reach systemd multi-user.target after boot {index}; "
            f"serial={result[-2500:]!r}"
        )
        print(
            f"BOOT {index}: installed Ubuntu systemd reached multi-user.target on restored GPT Btrfs root"
        )


def main() -> None:
    assert os.geteuid() == 0, "privileged disposable GitHub runner required"
    assert os.environ.get("GITHUB_ACTIONS") == "true", "CI-only disk builder"
    assert not ROOT.is_mount(), "root recovery mount is already in use"
    assert os.readlink("/proc/self/ns/mnt") != os.readlink("/proc/1/ns/mnt"), (
        "isolated mount namespace required"
    )
    # A Github-hosted runner may ALREADY mount a real ESP at /boot/efi.
    # Detach that *namespace-local copy* before attaching our disposable
    # loop FAT32, otherwise /proc/self/mountinfo has stacked entries at
    # the same path and the older hidden entry fails pinned mount checks.
    # The runner host mount namespace is never changed.
    if ESP.is_mount():
        run("umount", str(ESP))
    kernel_images = sorted(Path("/boot").glob("vmlinuz-*"))
    candidates = [
        path
        for path in kernel_images
        if (Path("/lib/modules") / path.name.removeprefix("vmlinuz-")).is_dir()
    ]
    assert candidates, (
        "Install Linux kernel and matching modules in disposable CI runner"
    )
    image_kernel = candidates[-1]
    kver = image_kernel.name.removeprefix("vmlinuz-")
    assert shutil.which("debootstrap"), "real Ubuntu rootfs installer required"

    with tempfile.TemporaryDirectory(prefix="harbor-tier3-blankdisk-") as name:
        temp = Path(name)
        backup = temp / "backup"
        backup.mkdir()
        source_image = temp / "old-root.btrfs.img"
        source_loop = new_loop(source_image, "1536M")
        src_root = temp / "src-root"
        src_root.mkdir()
        old_efi_img = temp / "old-esp.img"
        old_efi_loop = new_loop(old_efi_img, "256M")
        destination_img = temp / "fresh-machine.img"
        run("truncate", "-s", "3072M", str(destination_img))
        # The disk image is a regular file. No operations on a real disk.
        run(
            "sgdisk",
            "--zap-all",
            "--new=1:2048:+256M",
            "--typecode=1:EF00",
            "--new=2:0:0",
            "--typecode=2:8300",
            str(destination_img),
        )
        target_loop = run(
            "losetup",
            "--find",
            "--show",
            "--partscan",
            str(destination_img),
            capture=True,
        )
        target_efi = f"{target_loop}p1"
        target_root = f"{target_loop}p2"
        mounted: list[Path] = []
        try:
            run("mkfs.btrfs", "-f", "-L", "OLDBTRFS", source_loop)
            run("mkfs.vfat", "-F", "32", "-n", "HARBOR-ESP", old_efi_loop)
            run("mkfs.vfat", "-F", "32", "-n", "HARBOR-ESP", target_efi)
            run("mkfs.btrfs", "-f", "-L", "HARBORROOT", target_root)
            run("mount", source_loop, str(src_root))
            mounted.append(src_root)
            run("btrfs", "subvolume", "create", str(src_root / "@"))
            run("umount", str(src_root))
            mounted.pop()
            run("mount", "-o", "subvol=@", source_loop, str(src_root))
            mounted.append(src_root)

            # Target root subvolume is recreated on the DIFFERENT blank disk.
            ROOT.mkdir(exist_ok=True, parents=True)
            run("mount", target_root, str(ROOT))
            mounted.append(ROOT)
            run("btrfs", "subvolume", "create", str(ROOT / "@"))
            run("umount", str(ROOT))
            mounted.pop()
            run("mount", "-o", "subvol=@", target_root, str(ROOT))
            mounted.append(ROOT)
            (ROOT / "boot/efi").mkdir(parents=True)
            run("mount", target_efi, str(ROOT / "boot/efi"))
            mounted.append(ROOT / "boot/efi")

            ESP.mkdir(parents=True, exist_ok=True)
            run("mount", old_efi_loop, str(ESP))
            mounted.append(ESP)
            (ESP / "EFI/BOOT").mkdir(parents=True)
            shutil.copy2(image_kernel, ESP / "vmlinuz")
            image_initrd = temp / "initrd.img"
            run("mkinitramfs", "-o", str(image_initrd), kver, timeout=300)
            shutil.copy2(image_initrd, ESP / "initrd.img")
            grub_config = temp / "grub.cfg"
            grub_config.write_text(
                "set timeout=0\nset default=0\n"
                "insmod part_gpt\ninsmod fat\n"
                "search --no-floppy --label HARBOR-ESP --set=root\n"
                "linux /vmlinuz root=LABEL=HARBORROOT rootfstype=btrfs "
                "rootflags=subvol=@ ro console=ttyS0,115200n8 init=/sbin/init\n"
                "initrd /initrd.img\nboot\n"
            )
            run(
                "grub-mkstandalone",
                "-O",
                "x86_64-efi",
                "-o",
                str(ESP / "EFI/BOOT/BOOTX64.EFI"),
                "--locales=",
                "--fonts=",
                f"boot/grub/grub.cfg={grub_config}",
                timeout=180,
            )
            # Actually INSTALL Ubuntu 24.04 into the source Btrfs subvolume:
            # a real dpkg database, systemd init, and all its dependencies.
            # This writes ONLY inside the disposable loop image on CI.
            run(
                "debootstrap",
                "--variant=minbase",
                "--include=systemd-sysv,udev,dbus,btrfs-progs,ca-certificates",
                "noble",
                str(src_root),
                "http://archive.ubuntu.com/ubuntu",
                timeout=900,
            )
            assert (src_root / "var/lib/dpkg/status").is_file()
            assert (src_root / "lib/systemd/systemd").exists()
            assert (src_root / "sbin/init").exists()
            assert "Package: systemd" in (src_root / "var/lib/dpkg/status").read_text()
            # A real installed Ubuntu needs matching kernel modules. The
            # synthetic BusyBox acceptance never attempted to mount the ESP
            # from systemd, so a missing vfat module was invisible. Install
            # the runner's exact kernel/modules pair into the disposable
            # guest; no host modules are changed.
            source_modules = Path("/lib/modules") / kver
            guest_modules = src_root / "lib/modules" / kver
            assert source_modules.is_dir()
            guest_modules.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source_modules, guest_modules, symlinks=True)
            guest_boot = src_root / "boot"
            guest_boot.mkdir(exist_ok=True)
            shutil.copy2(image_kernel, guest_boot / f"vmlinuz-{kver}")
            shutil.copy2(image_initrd, guest_boot / f"initrd.img-{kver}")
            (src_root / "etc/hostname").write_text("harbor-restored-ubuntu\n")
            unit_dir = src_root / "etc/systemd/system"
            unit_dir.mkdir(parents=True, exist_ok=True)
            (unit_dir / "harbor-restore-acceptance.service").write_text(
                "[Unit]\nDescription=Harbor Ubuntu recovery two-boot acceptance\n"
                "After=multi-user.target\n"
                "[Service]\nType=oneshot\n"
                "ExecStart=/bin/sh -c 'echo HARBOR_INSTALLED_UBUNTU_RESTORED_OK > /dev/console; sleep 2; /usr/bin/systemctl --no-block poweroff'\n"
                "[Install]\nWantedBy=multi-user.target\n"
            )
            wants = unit_dir / "multi-user.target.wants"
            wants.mkdir(exist_ok=True)
            (wants / "harbor-restore-acceptance.service").symlink_to(
                "../harbor-restore-acceptance.service"
            )
            old_uuid = run(
                "blkid", "-s", "UUID", "-o", "value", source_loop, capture=True
            )
            old_esp_uuid = run(
                "blkid", "-s", "UUID", "-o", "value", old_efi_loop, capture=True
            )
            new_uuid = run(
                "blkid", "-s", "UUID", "-o", "value", target_root, capture=True
            )
            new_esp_uuid = run(
                "blkid", "-s", "UUID", "-o", "value", target_efi, capture=True
            )
            assert new_uuid != old_uuid
            assert new_esp_uuid != old_esp_uuid
            (src_root / "etc/fstab").write_text(
                f"UUID={old_uuid} / btrfs subvol=@ 0 0\n"
                f"UUID={old_esp_uuid} /boot/efi vfat umask=0077 0 2\n"
            )
            # Real native Btrfs snapshot streaming on the OLD image.
            started = cli(
                "set-start",
                "--target",
                str(backup),
                "--source",
                str(src_root),
                "--profile-id",
                PROFILE,
                "--allow-local",
                "--experimental",
            )
            assert started["status"] == "completed_btrfs_only"
            set_id = started["set_id"]
            catalog_file = backup / f".harbor-machine-set-{set_id}.json"
            record = json.loads(catalog_file.read_text())
            member = record["members"][0]
            member["original_mount"] = "/"
            member["snapshot_path"] = (
                f"/{NATIVE_FOLDER}/{Path(member['snapshot_path']).name}"
            )
            boot = {
                "archive": f".harbor-boot-{set_id}-000.tar.gz",
                "mount_point": "/boot/efi",
                "source": old_efi_loop,
                "filesystem": "vfat",
                "status": "pending",
            }
            with MountGuard(backup, capture_mount_identity(backup)) as guard:
                captured = capture_boot_member(backup, guard, boot)
            captured["status"] = "completed"
            record.update(
                {
                    "status": "completed_btrfs_and_boot_files",
                    "coverage": "btrfs-and-boot-files",
                    "boot_members": [captured],
                    "disk_layout": {
                        "blockdevices": [
                            {
                                "path": source_loop,
                                "uuid": old_uuid,
                                "mountpoints": ["/"],
                            },
                            {
                                "path": old_efi_loop,
                                "uuid": old_esp_uuid,
                                "mountpoints": ["/boot/efi"],
                            },
                        ]
                    },
                }
            )
            catalog_file.write_text(json.dumps(record))
            # ReaR EXTERNAL restore has already recreated and mounted the NEW
            # partitions on /mnt/local; it invokes the same Harbor command.
            rescue_marker = Path("/etc/rear/rescue.conf")
            rescue_marker.parent.mkdir(exist_ok=True)
            assert not rescue_marker.exists(), "test must not replace rescue configs"
            rescue_marker.write_text("# disposable CI-only rescue marker\n")
            try:
                restored = cli(
                    "set-rear-recover",
                    "--target",
                    str(backup),
                    "--set-id",
                    set_id,
                    "--allow-local",
                    "--experimental",
                    "--confirm",
                )
            finally:
                rescue_marker.unlink()
            assert restored["copied_to_rear"] is True
            assert (ROOT / "sbin/init").is_file()
            assert (ROOT / "boot/efi/EFI/BOOT/BOOTX64.EFI").is_file()
            assert f"UUID={new_uuid}" in (ROOT / "etc/fstab").read_text()
            assert f"UUID={new_esp_uuid}" in (ROOT / "etc/fstab").read_text()
            print(
                "ACTUAL HARBOR RESTORE: new GPT Btrfs root + FAT32 EFI + fstab UUID update"
            )

            # Unmount ALL filesystems before QEMU takes ownership of the
            # fresh disk image to avoid corrupting a mounted filesystem.
            for mnt in reversed(mounted):
                run("umount", str(mnt))
            mounted.clear()
            run("losetup", "-d", source_loop)
            source_loop = ""
            run("losetup", "-d", old_efi_loop)
            old_efi_loop = ""
            run("losetup", "-d", target_loop)
            target_loop = ""
            boot_twice(destination_img, temp)
        finally:
            for mnt in reversed(mounted):
                with contextlib.suppress(Exception):
                    run("umount", "-l", str(mnt))
            for loop in (source_loop, old_efi_loop, target_loop):
                if loop:
                    with contextlib.suppress(Exception):
                        run("losetup", "-d", loop)


if __name__ == "__main__":
    main()
