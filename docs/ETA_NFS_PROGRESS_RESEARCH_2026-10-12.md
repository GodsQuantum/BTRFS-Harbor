# ETA and NFS progress research — 12 October 2026

Only disposable Cloud9 CT700/CI tests; production disks, mounts and real backups unchanged.

## Reproducible microbenchmark
48 Btrfs members and 3,000 unrelated destination files, local temporary directory:
- rc3: 88.42 ms, 48 scandir calls per sample.
- rc4: 2.00 ms, 1 scandir call per sample.
With synthetic 4 ms/readdir delay: rc3 318.61 ms; rc4 6.21 ms.
These results simulate latency; they are not measurements against a real NFS mount.

## ETA decision
The compressed total of a full or incremental Btrfs send stream is not exposed in advance. Source logical bytes, exclusive/qgroup values or the count of Btrfs members cannot reliably estimate the final byte total. btrfs send --no-data emits metadata-only instructions and does not predict compressed data bytes. A second full send to precompute size would duplicate source work. Historical archive-size prediction plus smoothed recent rate requires calibration against diverse full/incremental workloads and error bounds; no such model currently qualifies. ETA remains NULL, not a fictitious percentage.

Executable evidence: tests/integration/tier2/test_send_eta_evidence_real.py uses the existing disposable privileged Btrfs loopback fixture to compare equal logical sizes with compressible and random content, tiny vs full-overwrite incrementals, zstd stream output, and --no-data. Treat its eventual CI outputs as observations, not a universal model.

## Upstream chronology (consulted 2026-10-12)
- btrfs-progs latest GitHub release visible: v7.1 released 2026-07-14 (not October): https://github.com/kdave/btrfs-progs/releases
- btrfs-progs PR #1168 send-stream 64-KiB-buffer opened 2026-09-07, not a production ETA solution: https://github.com/kdave/btrfs-progs/pull/1168
- Snapper GitHub latest listed v0.13.1 (released 2026-04-22), while the packaging changelog includes 0.13.2 dated 2026-06-16: https://github.com/openSUSE/snapper/releases and https://github.com/openSUSE/snapper/blob/master/package/snapper.changes
- Official send flags and --no-data semantics: https://btrfs.readthedocs.io/en/latest/btrfs-send.html (publication date not established).
- Tauri hidden-window events issue #15652 opened 2026-07-05; not proof that our Channel fails on systray: https://github.com/tauri-apps/tauri/issues/15652

## Runtime caveat
Tauri Channel<Value> is required as a parameter and bound to the active invoke and process. Closing the GUI window to tray retains the existing Rust runtime; a full process crash/quit cannot retain the IPC subscription. Source/destination leases and resume journal remain authoritative. KDE Wayland systray and final-SHA native runtime require separate validation.

Version rc4 changes only sampling safety and UI wording; never rewrites snapshot or restore formats.
