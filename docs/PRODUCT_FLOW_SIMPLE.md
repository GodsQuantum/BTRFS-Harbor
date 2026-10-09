# Btrfs Harbor: one simple flow (October 2026)

This is the non-negotiable product contract. **No service dashboard on the home screen.**
Keep power-user diagnostics, scheduling, topology, retention and engine internals in separate
pages, without removing useful capabilities.

## Home screen

1. **Snapshot** — Latest (default), choose from actual host snapshot list (date + description),
   or create a new readonly snapshot. Identify the actual subvolume, not only the distro name.
   Use Snapper if available; otherwise Btrfs native readonly snapshots (provider pending).
2. **Destination** — Choose local folder, NFS, SMB or SSH as supported and verified. Validate
   the actual mount identity/free space; never auto-remount, invent aliases or silently change
   a mount. Destination should be chosen inline (not yet implemented in this preview).
3. **Send** — A single primary button. First send is a full standalone base. Later sends are
   incremental only with the complete parent chain and matching readonly local parent.
   One durable transaction and no duplicate sends.
4. **Progress** — Big legible status, committed bytes, time/speed when trustworthy; do not
   fabricate a percentage or ETA when Btrfs cannot supply a reliable total. Pause/Stop.
   Closing the window hides to systray while the sender runs; if tray is unavailable,
   keep the window open. Force-quit/reboot must preserve checkpoints/source snapshots
   so a later Resume replays locally but never reuploads committed destination blocks.
5. **Restore** — Choose destination, backup point, target disk/filesystem, and staged receive.
   This must work on the original system, a clean reinstall or different computer
   **without requiring the original Harbor profile**. Detect original OS, identity,
   EFI/bootloader and missing non-Btrfs partitions before offering bootable recovery;
   permit file-only staged recovery separately. **Fresh-machine independent import
   is still pending acceptance.**

## What is implemented in preview source after rc.4

- Overview reduced to primary backup and restore actions; technical panels collapsed.
- Actual Snapper snapshot numbers/date/description read through the bundled engine
  JSON, with strict native config-name validation, avoiding manual numeric entry.
- Transfer manifests keep resumable state and committed bytes. An indeterminate
  progress bar represents unknown total honestly.
- The v2 worker now uses the existing Tauri tray/close/Stop mechanism, a dedicated
  process group and correct stdout/stderr handling; no added custom daemon.
  Close hides without a modal interruption; no tray means no silent worker kill.
- Automatic complete/incremental selection and snapshot pinning from rc.4 remain.
- Source/destination change no longer allows a stale old-target refresh to
  masquerade as progress of a different snapshot.

## Must be completed before a general, all-distro claim

- Native Btrfs snapshot adapter when Snapper is missing; comprehensive multi-subvolume
  coverage, including /home and separately mounted persistent subvolumes.
- One engine for installed timers/SSH and portable local/NFS/SMB; no legacy fallback
  without Resume while claiming full Resume support.
- Inline destination chooser with safe persist/validation of user-chosen location.
- Destination-only recovery on a fresh machine without local profile. End-to-end
  disposable full/incremental receive and file/system restore, and bootability tests
  across different hardware/layouts.
- Actual systray close and force-stop/relaunch Resume test while sending a stream.
- Arch/CachyOS, Ubuntu/Debian, Fedora, openSUSE functional Btrfs acceptance; additional
  libc/CPU support only when separately built and tested.

## Sources checked during October 2026 pass

- Btrfs docs: btrfs-send(8), btrfs-receive(8), send stream format (upstream docs);
  source and parent must be readonly and incrementals require an available base.
- openSUSE/snapper: native Snapper CLI and D-Bus protocol (ListSnapshots,
  CreateSingleSnapshotV2, read-only and snapshot cleanup).
- kdave/btrfs-progs releases: current upstream v7.1; no native interrupted
  output stream append/restore guarantee should be invented.
- Official Tauri v2 System Tray docs: TrayIconBuilder and on_menu_event on Linux;
  Linux tray-click notifications are not portable, so rely on menu actions.
