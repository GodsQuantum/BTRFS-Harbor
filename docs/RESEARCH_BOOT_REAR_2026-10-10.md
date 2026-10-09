# Bootable recovery backend investigation — 2026-10-10

Status: **architecture investigation, NOT implemented or qualified**.
Do NOT market any existing machine-set as a bootable disk/system image.

## Primary candidate: ReaR as existing mature offline recovery machinery

Sources:
- https://github.com/rear/rear/blob/master/doc/user-guide/10-integrating-external-backup.md
- https://github.com/rear/rear/blob/master/usr/share/rear/restore/EXTERNAL/default/500_restore_with_external.sh
- https://github.com/rear/rear/blob/master/doc/rear.8.md
- https://github.com/rear/rear/blob/master/doc/user-guide/03-configuration.md
- https://relax-and-recover.org/documentation/release-notes-2-9
- https://github.com/rear/rear/issues/3523

ReaR supports an OUTPUT=ISO rescue image and a BACKUP=EXTERNAL plugin strategy.
Its actual master source restore/EXTERNAL/default/500_restore_with_external.sh
performs `eval "${EXTERNAL_RESTORE[@]}"` and fails nonzero exit. Do not interpolate
untrusted target paths into this Bash value; use one static Harbor wrapper
reading an authenticated/validated rescue configuration. ReaR can reconstruct
partition tables, filesystems and GRUB/UEFI under a rescue environment, while
Harbor supplies the Btrfs snapshots. ReaR 2.9 documents Arch/Manjaro support,
but this does NOT certify CachyOS, arbitrary systemd-boot/UKI, Secure Boot,
LUKS-on-Btrfs or users' custom nested subvolume layouts.

A 2025 issue noted EXTERNAL_RESTORE not executing in one configuration; do
not assume the extension is reliable without QEMU end-to-end tests. ReaR
provides a native BACKUP integration hierarchy under
/usr/share/rear/{prep,rescue,restore,...}/PLUGIN; an optional Harbor plugin
could avoid new custom long-running services. Use a version-pinned ReaR
package through the distro, not a copied fork or host modifications.

## Required end-to-end acceptance BEFORE enabling "Recover entire machine"

1. Source VM with UEFI/ESP, Btrfs root+home (+ optional separate /boot),
   GRUB and/or systemd-boot; real Harbor set-start, incremental, SIGKILL resume.
2. Preserve ESP and ALL persistent non-Btrfs boot mount content in an atomic,
   SHA-256 verified archive; metadata like GPT, fstab, crypttab and hardware
   IDs are already captured by Harbor recovery-kit but files are not.
3. Create ReaR rescue ISO with Harbor binary/runtime embedded, without
   modifying host ReaR global config or Linux mounts.
4. Boot ISO under QEMU/OVMF with a fresh blank virtio disk, detect target disk,
   require explicit destructive confirmation in test, create partitions and
   mount placeholders with ReaR, restore Harbor Btrfs snapshots to the
   correct writable subvolumes, restore ESP and /boot content, reconcile
   PARTUUID/UUID, fstab/crypttab and regenerate bootloader/initramfs.
5. Boot resulting *restored* VM, verify correct machine-id, files, subvolume
   mapping and at least one successful reboot; test different target disk size.
6. Fail closed and mark 'data-only' for missing boot artifacts, unsupported
   encryption/UKI/secure boot, unmounted subvolumes, or missing device.
7. Test Arch/CachyOS + Debian/Ubuntu + Fedora where practicable. No universal
   boot support without platform-specific real tests.

ReaR **alone** does not solve missing EFI content or the mapping of Harbor's
volume-NNN staged archives to ReaR's freshly mounted root/home: implement and
test that bridge. The legacy Harbor recovery planner's
RecoveryCompatibility::FullSystem is an OS-family *compatibility* signal,
not an actual bootable restoration certificate. Avoid advertising it as proof.

## Anti-goals

No new Docker daemon, no mounts changed on Cloud9/Pegasus, no disk formatting
outside disposable CI images, no extra full duplicate tar of the Btrfs root,
no speculative automatic Secure Boot key provisioning.
