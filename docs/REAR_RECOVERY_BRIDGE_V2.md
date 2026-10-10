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

**NOT RELEASE-CERTIFIED FOR BOOTABLE DISASTER RECOVERY.** Essential gates:
a complete QEMU/OVMF UEFI virtual disk restore with successful TWO boots;
independent distro trials (CachyOS/Arch, Ubuntu, Fedora); nested subvolume
layouts and separate /boot; incompatible encryption/UKI/Secure Boot must
fail clearly. SSH-v2 and automatic retention require separate completion.
Never write to physical backup/source drives for tests.
