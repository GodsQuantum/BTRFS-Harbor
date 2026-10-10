# ReaR/Harbor recovery bridge status (2026-10-10)

ReaR 2.9 BACKUP=EXTERNAL provides disk/partition/bootloader orchestration.
Harbor provides source-only Btrfs+FAT32 boot archival, checksum validation,
non-destructive staging, received-UUID mapping and original/new fstab UUID
reconciliation. ReaR EXTERNAL_RESTORE invokes only the **static installed**
/usr/lib/btrfs-harbor/recovery/harbor-rear-restore hook. No custom daemon.

**Build rescue ISO:** After a completed system-wide machine set with mounted
FAT32 EFI and /, choose **Créer ISO de secours** in the application. Requires
the distro-provided ReaR >=2.9 installed as an optional dependency and a
working host boot kernel. Harbor invokes ReaR with its OWN installed config
directory (/usr/share/btrfs-harbor/rear), not /etc/rear; builds locally and
publishes the ISO on the chosen mount through a directory-FD guard. The ISO
gets SHA-256 and ISO9660 format verification. It is NOT boot-tested.

**Recover:** Boot the rescue ISO; let ReaR recreate mounts from its recorded
layout. Its static EXTERNAL restore hook prompts for a mounted backup
destination, enumerates completed machine sets, and asks for explicit user
confirmation. The preflight refuses unknown mounts, wrong fs types,
encrypted crypttab entries, and altered source UUIDs; it copies the actual
received Btrfs subvolumes and archived EFI files, remaps fstab UUID and
then defers to ReaR's bootloader/initramfs finalization. Does not itself
partition or format disks and fails outside rescue.

**UEFI blank-disk recovery TESTED for one minimal Linux case.** The
[disposable GitHub UEFI acceptance](https://github.com/GodsQuantum/BTRFS-Harbor/actions/runs/38058403995)
passed on 2026-10-10: a real Btrfs source was sent to Harbor, a
separate FAT32 EFI was archived, both were restored onto an entirely
new GPT virtual disk, old/new UUID references were reconciled, and
QEMU/OVMF booted the restored Linux root twice all the way into
/sbin/init. The independent ReaR rescue ISO QEMU boot test also passed.

**NOT YET RELEASE-CERTIFIED FOR UNIVERSAL BARE-METAL RECOVERY:**
the acceptance uses a small BusyBox Linux system with the host
kernel/initramfs and a standalone GRUB EFI. Independent CachyOS/Arch,
Ubuntu/Fedora installed-system trials, nested subvolume layouts,
separate /boot, LUKS, Secure Boot, UKI, systemd-boot and Limine still need
matrix verification. SSH-v2 and automatic retention require their own
safe implementation. This preliminary preview must not be used as the
only backup/recovery copy.
Never write to physical backup/source drives for tests.
