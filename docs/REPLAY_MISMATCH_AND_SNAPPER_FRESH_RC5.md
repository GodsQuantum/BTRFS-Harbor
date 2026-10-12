# Replay SHA-256 divergence — rc.5 engineering record

## Root cause
The reported replay checkpoint 14 raw SHA-256 mismatch originates in core/replay_v2.py. After an interrupted upload, Harbor reruns Btrfs send from an unchanged readonly snapshot and compares committed raw bytes. A different byte stream cannot safely be concatenated with the compressed partial, but this does not alone prove source or target data corruption.

## Recovery
ReplaySourceMismatch signals a typed source mismatch; resume_worker persists failed_unrecoverable for that incomplete transaction without removing or truncating the original compressed partial or manifest. The machine-set driver, under its exclusive lock, confirms this exact terminal state and records the original archive and transfer ID. It changes only the failed member to an archive suffixed -retry1 and generates a new stream from the original readonly snapshot (same source UUID and path). Already completed members stay unchanged. Exactly one automatic retry per member is permitted.

An application crash after the old journal enters failed_unrecoverable and before the machine-set catalog is updated is recoverable by Resume, which sees the durable terminal state. When all replacements complete, release the old Snapper pins; any cleanup failures are warned. Neither SHA checks nor destination mount identity guards are bypassed.

## Snapper
The default whole-machine Back up now requests a NEW Snapper readonly snapshot for each configured Btrfs source; unmanaged Btrfs data subvolumes get a Harbor-native capture. A separate mode exports the latest existing Snapper snapshots. A failed Snapper snapshot creation is fatal rather than silently falling back to a stale snapshot. New snapshots have Snapper number-retention policy, while the checkpoint pin protects snapshots being transferred. Snapshot creation occurs before catalog persistence, so a crash in that short window can leave a Snapper-owned unreferenced snapshot requiring review.

## Limits
No byte-perfect mid-stream resume is claimed if a new Btrfs send differs; only the failing member is transferred from zero. Previous partials remain as audit evidence until explicit user discard. Global byte percentage and ETA remain unavailable because the final incremental/compressed stream size cannot be known reliably in advance. Bootable universal cross-hardware restore and LUKS/UKI/Wayland scenarios are not certified. Existing mount, NFS, ACL, swap and real backup data were not modified in development.

## References consulted October 12, 2026
- https://btrfs.readthedocs.io/en/latest/btrfs-send.html
- https://btrfs.readthedocs.io/en/latest/dev/dev-send-stream.html
- https://man.archlinux.org/man/extra/snapper/snapper.8.en
- https://manpages.opensuse.org/Tumbleweed/snapper-backup/snbk.8.en.html
