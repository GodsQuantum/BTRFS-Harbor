# Distribution portability and public release gates

Btrfs Harbor is intended for Linux systems backed by Btrfs, not just its developers' computers. Do not hardcode a distribution, host, mount path, package manager, snapshot layout or destination. Detect capabilities and explain missing ones. Official binaries are currently Linux x86_64/glibc; all distros is a design target rather than an unconditional binary-compatibility claim.

## Status at v0.2.6-rc.4

| Capability | Current status |
| --- | --- |
| x86_64 AppImage on compatible glibc desktop | Published and smoke tested locally |
| Manual Btrfs send from Snapper to local/NFS/SMB | RC preview, end-to-end receive/restore acceptance outstanding |
| Full base then incremental using verifiable parent chain | Implemented; real full/incremental restore gates outstanding |
| Interrupted checkpoint Resume | Implemented; long stream and real NFS outage tests outstanding |
| Native Btrfs source without Snapper | Not yet supported in unified checkpoint engine |
| Other Btrfs subvolumes without Snapper | Explicitly excluded from v2 sends |
| Automatic timers and SSH | Legacy engine, checkpoint migration outstanding |
| Non-systemd Linux | Manual portable goal; persistent scheduling needs supported mechanism |
| ARM64, musl/Alpine, immutable/Nix-like variants | Need separate build/packaging and real acceptance; no blanket support claim |

## Required before a universally advertised general release

1. Discover actual Btrfs filesystem/mount/subvolume identities, including nested subvolumes, root/home and excluded filesystems; do not infer from a distro name.
2. Abstract snapshot providers: use Snapper when available; otherwise create/adopt a safe native read-only Btrfs snapshot with correct ownership, lifetime and UUID. Do not reconfigure Snapper or mounts without consent.
3. Unify all source types under one checkpoint engine with full initial base, parent-verified incrementals, atomic/no-clobber commit, persistent interrupted Resume and single-writer destination locking.
4. Qualify actual full/incremental Btrfs receive onto disposable scratch Btrfs storage, hash/content verification, interruption at arbitrary progress, NFS/SMB disconnect and reconnect, mount-identity changes, incomplete journals, parent retention and cleanup.
5. Run functional installations on Arch/CachyOS/Manjaro, Debian/Ubuntu/Mint, Fedora and openSUSE, covering GUI/WebKitGTK/polkit, portable mode with no agent, installed-mode automation, source discovery, save/restore, and uninstall. The current four-distro CI only runs shell syntax/package-manager checks, not Btrfs send/receive.
6. Test oldest supported glibc/WebKitGTK runtime, AppImage FUSE/extract fallback, target architecture and dependencies. Build/test aarch64 and musl separately before claiming support. Manual engine must not depend on systemd.
7. Require branch CI, packaging CI and real-kernel restore acceptance to pass before labeling universal support. Stable Rust/Arch package version and desktop RC version can differ intentionally and must be validated independently.

Maintain actual tested distro+architecture+libc, Btrfs layout, snapshot provider, destination, full/incremental, resume, receive/restore, and pass/fail evidence. Two machines running the same distribution may have different Btrfs layouts. RC4 is supervised preview, not an independently validated cross-distribution backup guarantee.
