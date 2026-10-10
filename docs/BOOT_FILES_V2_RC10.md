# EFI/boot files with the same Harbor v2 machine-set journal

For a machine-wide set-start, include the separately mounted boot filesystems
/boot, /boot/efi, /boot/firmware and /efi (non-Btrfs filesystems only).
Read-only source mount identity must match at start/end. Before replacing a
temporary file with its atomic archive, destination mount identity is checked,
bytes fsynced, SHA-256 captured; the same set catalog records the result.

Resuming after an interruption preserves completed Btrfs streams and already
completed boot archives. Compressed tar captures restart if interrupted.
Restore works to a new empty folder, NOT to any partition or block device.
The catalog also retains read-only lsblk layout metadata to bridge a future
ReaR/OVMF recovery. If fstab expects EFI but it is unmounted, fail closed.

**The current point is still not a bootable full-machine backup.**
Missing: automatic disk reconstruction, full non-Btrfs data volumes,
UEFI/GRUB/Limine/systemd-boot and initramfs repair, tested QEMU OVMF recovery,
checkpointed SSH and transactional automatic retention.
See docs/RESEARCH_BOOT_REAR_2026-10-10.md.
