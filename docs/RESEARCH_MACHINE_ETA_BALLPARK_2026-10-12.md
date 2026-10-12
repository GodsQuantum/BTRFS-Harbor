# BTRFS Harbor rc.6 — advisory size and ETA, 12 October 2026

## Technical problem
Btrfs send full/incremental streams are not equal to filesystem usage because they include metadata operations, clone/reflink behavior and changed extents; the stream is further zstd-framed/compressed. Neither qgroups nor Btrfs filesystem du measures the exact final wire length, and btrfs send --no-data contains no file contents. A true exact total would require producing the actual stream ahead of time, imposing heavy I/O. Therefore an advisory estimate with a broad range is more useful than an indefinite 'unavailable', without misrepresenting certainty.

Upstream documentation:
- https://btrfs.readthedocs.io/en/latest/btrfs-send.html
- https://btrfs.readthedocs.io/en/latest/btrfs-qgroup.html
- https://btrfs.readthedocs.io/en/latest/btrfs-filesystem.html
- https://man.archlinux.org/man/btrfs-filesystem.8.en

## Design, instrumentation and safety
1. When a NEW backup or resumed backup is launched on rc.6, run btrfs filesystem du -s --raw PATH, a read-only FIEMAP summary, via a separate daemon thread and an external process bounded to 4 seconds. This measures allocated extents (may overcount nested shared subvolumes) and does not scan contents.
2. On FIEMAP failure try btrfs qgroup show --raw -f PATH with 2 second timeout ONLY if quotas are already active. Accept only a single unambiguous level-0 referenced-size record; no quota enable/rescan, mounts or ACL changes.
3. A 0.9 compressed/reference central factor is used initially, with a deliberately broad range (0.62..1.65 of central per measured member); finished volumes recalibrate ratio using actual compressed destination bytes/reference bytes. Unknown source volumes use a conservative peer-size estimate marked low confidence. Progress cannot hit 100 percent before all members complete.
4. Destination telemetry remains one os.scandir per sample, target files only; an in-progress canonical UUID partial and completed archive are never double-counted. ETA uses smoothed positive observed throughput and a range; after 25 seconds of no growth it is suspended, while size forecast remains.
5. Source measurement is best effort and the slow sampler cannot delay final JSON. The optional source reference size, when read from an existing catalog, is validated as a positive bounded integer; the new background sampler keeps its samples in a separate ephemeral map, never mutating the journal data or writing anything to the destination. Resume safely resamples.
6. Existing exact percent field remains null (except no changes to existing semantics); approximate percentage is a separately labeled field. Final byte total and ETA are never promised as certain. No changes to data integrity, NFS mounts or real backups.

## Deployment
Do not upgrade the active user transfer; it is on an older binary and must finish undisturbed. The code is for newly launched rc.6 transfers, after the active backup has finished. No universal cross-hardware recovery certification follows from this change.
