# BTRFS Harbor security research — October 10, 2026

### Confirmed upstream vulnerabilities relevant to a resumable SSH design

- btrbk CVE-2026-62943 / GHSA-pf45-7g54-65h5: versions below
  0.32.7 using ssh_filter_btrbk.sh as authorized_keys forced-command
  could allow a trailing shell command after an allowed prefix. Fixed in
  v0.32.7 (2026-09). Harbor must not copy the vulnerable filter or rely on
  a regex-only forced command. Source:
  https://bugs.debian.org/cgi-bin/bugreport.cgi?bug=1148559
- SSHFS CVE-2026-47187 and CVE-2026-48711: fixed in sshfs 3.7.6,
  May 2026 (symlink escape and SSH argument injection). If a future Harbor
  optional SSHFS adapter is implemented, reject older binaries and enforce
  symlink containment, strict host key checking and private mount namespaces.
  Sources:
  https://github.com/libfuse/sshfs/releases/tag/sshfs-3.7.6
  https://www.openwall.com/lists/oss-security/2026/05/30/3

### Engineering choice

Do NOT force users into a new SSHFS mount merely to make v2 appear
complete. First prove remote append/chunk checkpoint persistence,
the same host-key/identity validation across reboot, and real SSH outage
+ re-authentication replay on an isolated account. The existing inherited
SSH raw backup remains available but is NOT advertised as resumable v2.

For EFI bare-metal restore, prefer ReaR 2.9 native disk reconstruction,
then a Harbor-specific restore bridge; ReaR itself clearly separates
storage reconstruction, data restore and bootloader deployment:
https://relax-and-recover.org/documentation/release-notes-2-9
A rescue ISO without ESP and /boot data captured by Harbor cannot provide
a working clone; QEMU/OVMF clean disk boot verification is obligatory.

## Verified web follow-up — 2026-10-10 (post-rc.11)

This is a research and gate review, **not a claim of newly certified features**.

- ReaR official GA remains **2.9 (January 2025)** as of this check:
  https://relax-and-recover.org/download/ . In the official configuration
  documentation, `OUTPUT=ISO` builds rescue media while
  `BACKUP=EXTERNAL` delegates restoring data to an external hook:
  https://relax-and-recover.org/rear-user-guide/basics/configuration.html .
  Merely booting that rescue ISO **does not** prove its restore hook ran.
  CI must exercise the hook in the disposable guest before an installed-OS
  recovery can be advertised as certified.
- Btrfs send and receive require readonly sources and an exact usable parent
  for incrementals: https://btrfs.readthedocs.io/en/latest/btrfs-send.html .
  The checkpoint-v2 stream replay is application-level; no upstream protocol
  promise of arbitrary byte-offset resumption should be inferred.
- btrbk 0.32.7 explicitly fixes **CVE-2026-62943**, arbitrary command
  execution through the `ssh_filter_btrbk.sh` forced-command filter:
  https://github.com/digint/btrbk/blob/master/ChangeLog . For Harbor SSH-v2,
  do not copy shell-filtering expressions. Require a dedicated fixed-action
  remote receiver with validated identities and host keys, exact-size
  chunk commitments, no caller-provided shell fragment and server-side
  bounded manifests; qualify real network interruption on CI ephemeral SSH.
  Until then, checkpointed SSH stays blocked.
- Snapper's `number` and `timeline` cleanup operates on **source
  snapshots**, not Harbor remote archives:
  https://snapper.io/manpages/snapper.html ;
  https://snapper.io/manpages/snapper-configs.html . Harbor v2 remote pruning
  must keep the complete ancestor closure of retained incremental archives.
  Existing retention-plan is **dry-run only**; do not enable deletion without
  complete catalog, foreign-object and power-loss transaction tests.
- ArchWiki Limine documents the ESP/system partition and the UEFI fallback
  `EFI/BOOT/BOOTX64.EFI`; kernels/initramfs must be accessible via supported
  filesystems: https://wiki.archlinux.org/title/Limine . The September 10,
  2026 archinstall report about a small Limine+Snapper ESP is a specific
  user report, not an all-host benchmark:
  https://github.com/archlinux/archinstall/issues/4769 .
  Test GRUB, Limine and systemd-boot separately, with installed guest OSes.
- A June 1, 2026 Tauri/KDE Wayland report notes native titlebar controls
  becoming unresponsive after restoring a window from tray:
  https://github.com/tauri-apps/tauri/issues/15460 . This supports adding
  KDE Wayland and X11 close/reopen tests; it **does not** demonstrate a
  Harbor-specific failure.
- GitHub Actions path filters must cover source code that affects a gate:
  https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax .
  rc.11's ReaR ISO workflow previously triggered only on edits to its own
  YAML. The source/package recovery paths are now tracked explicitly with
  a regression test.

**Validated so far:** disposable NFS outage/Resume, Tier2 Btrfs + FAT32,
ReaR 2.9 rescue ISO boot, and two OVMF UEFI boots of the restored BusyBox
minimal Linux. **Not validated:** full installed OS restoration, SSH-v2,
automatic v2 retention, KDE tray on actual graphical desktops, LUKS/UKI and
Secure Boot.

### Community cross-check (not security evidence)

- **2026-08-27, r/cachyos:** a user describes a Snapper rollback
  breaking after the corresponding kernel disappeared from a separately
  managed boot partition. This is anecdotal, but it illustrates why
  Harbor must capture and restore compatible /boot, ESP and kernel together,
  then test the bootloader on an installed OS, not just a synthetic guest:
  https://www.reddit.com/r/cachyos/comments/1vzte82/welp_snapper_and_my_ignorance_broke_my_system/
- **2026-07-11, r/btrfs:** an openSUSE-on-Btrfs user with a separate ZFS
  pool asked how to make root/home snapshot archives restorable after
  replacing the OS drive. Responses discuss incremental raw stream
  complexity; user reports are not technical verification:
  https://www.reddit.com/r/btrfs/comments/1utfoee/snapshots_to_a_different_drive/
