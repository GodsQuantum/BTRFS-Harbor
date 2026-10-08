# Btrfs Harbor v0.2.5 — Recovery, Real Transfer Progress, Engine Manager and Distro Portability Design

**Date:** 2026-10-08
**Status:** Design approved in chat; written specification awaiting explicit review before implementation
**Base:** `39bffd485268d54dda2abef6792f7dec93ae1c37` (v0.2.4)
**Scope:** Btrfs Harbor desktop/control plane plus the inherited btrfs-backup-ng integration

## 1. Product intent

Btrfs Harbor must behave like a distro-agnostic Btrfs backup/recovery application rather than a CachyOS-specific wrapper. It must make the current backup engine visible before a backup starts, show honest live transfer telemetry, and make disaster recovery understandable for both a replacement disk in the same machine and a migration/clone onto different hardware.

The design favors existing Linux/Btrfs/systemd/btrfs-backup-ng primitives over duplicate Harbor-specific mechanisms. Harbor orchestrates, validates and presents them.

## 2. Non-negotiable UX and safety rules

1. Never claim a backup is complete until the engine has atomically finalized its streams/metadata and verification has completed.
2. Never display a fabricated transfer percentage. Bytes, elapsed time and rate may be exact even when the final total is unknown.
3. Never overwrite the live root filesystem from the running desktop application.
4. Never silently transplant machine identity when restoring onto different hardware.
5. Never silently write to an underlying local directory when an expected NFS/SMB mount is absent.
6. A manual backup stays manual unless the user explicitly installs automation.
7. The Overview must disclose the exact btrfs-backup-ng origin, version and path before any run.
8. Distro handling must be capability-based, not CachyOS/Arch allowlist-based.
9. The portable AppImage must remain useful without a permanent Harbor installation.
10. UI first-level copy uses human concepts; subvolume names, UUIDs and process details live under Details.

## 3. Research findings that constrain the design

### 3.1 Existing engine progress should be reused

The inherited engine already contains `core/progress.py`, snapshot-size estimation, Rich transfer rate/ETA support, transaction byte accounting, chunked-transfer counters and transfer manifests. Its own source explicitly documents the remaining direct-pipe gap and points to out-of-band byte measurement such as `pv` or Linux `/proc/<pid>/io`.

Therefore Harbor will not create an independent byte-copy engine.

A live read-only check on the reference workstation with the official v0.2.4 AppImage also validates the cheapest raw-target measurement: during a compressed NFS backup, the two `.part` files grew by 567,975,936 bytes over 12 seconds (about 45.1 MiB/s aggregate) while `btrfs send` and `zstd` remained active. No system `btrfs-backup-ng` binary was installed, so this run used Harbor's bundled engine. This is direct evidence that raw/NFS target-file size can drive exact target-byte and throughput telemetry without touching the data path.

### 3.2 AppImage is portable, not magically universal

AppImage guidance requires building against an old-enough base and testing on every claimed base family. Harbor will replace marketing such as “CachyOS-ready” with capability/support tiers and test those tiers in CI.

Reference:
- https://docs.appimage.org/reference/best-practices.html
- https://docs.appimage.org/introduction/concepts.html

### 3.3 Upstream engine releases and distro packages can differ

Harbor must not equate “package manager says up to date” with “latest upstream”. Engine state needs three separate concepts:

- active version;
- latest version known from the active distribution/channel;
- latest compatible upstream release known to Harbor.

As of this design, upstream tags include v0.9.12. Harbor still requires at least 0.9.12.

Reference:
- https://github.com/berrym/btrfs-backup-ng
- https://pypi.org/project/btrfs-backup-ng/

### 3.4 Machine identity must be regenerated for migration

For a restored tree mounted offline, systemd provides supported mechanisms for generating a new machine identity. Harbor should orchestrate those mechanisms rather than inventing identifiers.

Reference:
- https://www.freedesktop.org/software/systemd/man/systemd-firstboot.html
- https://www.freedesktop.org/software/systemd/man/systemd-machine-id-setup.html

## 4. Overview information architecture

Overview becomes a cockpit, not a duplicate settings page.

### 4.1 Protection summary

Show:

- latest valid backup time;
- age;
- destination name and health;
- last verification result;
- recovery readiness.

### 4.2 Primary actions

Primary: **Back up now**
Secondary: **Recover**

Scheduling configuration does not occupy the main workflow. Overview only summarizes automation state:

- Not installed;
- Installed, next run …;
- Disabled/error.

The detailed job list remains under **Scheduled Jobs**.

### 4.3 Permanent engine line

Always visible even when no backup is running.

Examples:

> **Backup engine · Harbor bundled 0.9.12 · active · up to date**  [Manage]

> **Backup engine · System 0.9.12 · /usr/bin/btrfs-backup-ng · active**  [Manage]

> **Backup engine · System 0.9.8 incompatible → Harbor bundled 0.9.12 active**  [Manage]

This is populated by an explicit engine-status command, not by waiting for the next backup event.

## 5. Engine Manager

Add **Settings → Backup engine**.

### 5.1 Selection policy

Modes:

- **Auto (recommended)** — compatible system engine first, otherwise bundled.
- **System** — require compatible system engine, otherwise block with actionable explanation.
- **Harbor bundled** — explicitly pin the embedded engine.

The selected mode is Harbor configuration, not an environment-variable trick.

### 5.2 Engine status model

Introduce a serializable status object carrying:

- requested policy;
- active origin: `system | bundled`;
- active version;
- active executable path;
- minimum compatible version;
- discovered system version/path even when inactive;
- bundled version/path;
- compatibility reason;
- update availability state;
- update channel/provenance.

### 5.3 Update semantics

**Bundled engine:** immutable inside an AppImage. “Update” updates Btrfs Harbor itself through the signed Harbor release/updater path. The UI must not pretend to mutate the mounted AppImage.

**System engine:** Harbor detects ownership/provenance before proposing any update:

- distro package: pacman, apt/dpkg, dnf/rpm or zypper/rpm;
- `uv tool`;
- `pipx`;
- unknown/manual.

For recognized package managers Harbor may offer an explicit privileged update action and then re-probe the version. If the distro channel remains below Harbor's compatibility floor, Harbor recommends switching to the bundled engine rather than compiling over the system.

Unknown/manual installs get detection information and safe guidance; Harbor does not overwrite them.

### 5.4 Harbor application updater

Use Tauri's official updater for signed Harbor updates. On Linux the UI explains that the application must be relaunched after installation.

Reference:
- https://v2.tauri.app/reference/javascript/updater/

## 6. Real transfer telemetry

### 6.1 Definitions

Harbor distinguishes:

- **source bytes processed** — bytes emitted by the source stream where measurable;
- **target bytes written** — bytes sent into/written at the destination side of the Harbor transfer;
- **total estimate** — an estimate, never mislabeled as exact;
- **rate** — target bytes delta over monotonic elapsed time;
- **ETA** — only present when a defensible total estimate exists and transfer rate has stabilized.

### 6.2 Progress protocol

Extend the engine's machine-readable output with versioned JSONL transfer events. Example logical schema:

```json
{
  "schema": 1,
  "event": "transfer_progress",
  "volume": "/home",
  "snapshot": "home-20261008-...",
  "destination": "backup-local",
  "bytes_target": 28412059648,
  "bytes_source": 33124519117,
  "total_estimate": 74200000000,
  "estimate_kind": "snapshot-logical",
  "bytes_per_second": 47331328,
  "elapsed_seconds": 1284,
  "eta_seconds": 868,
  "measurement": "raw-target-file",
  "certainty": "estimated-total"
}
```

All fields that cannot be measured are nullable. The consumer must tolerate partial telemetry.

### 6.3 Measurement strategy by target

**Raw/local/NFS/SMB file targets:** use the actual growing temporary target stream/file size after compression. This is exact for target bytes and matches what the user sees consuming destination capacity.

**Chunked/raw resumable transfer:** use existing chunk manifest counters.

**Local Btrfs receive / direct stream:** use existing engine progress primitives where bytes already pass through a measurable path. Where the direct OS pipeline avoids Python, use a Linux out-of-band counter. Prefer an already-present `pv` pipeline when available; otherwise use Linux process I/O counters with an explicitly named measurement kind.

**SSH:** measure the transfer pipeline rather than guessing from destination capacity. Never pretend protocol/network overhead equals Btrfs payload bytes.

### 6.4 UI behavior

During a transfer the progress panel shows, in priority order:

1. current human source and target;
2. **actual bytes written/transferred**;
3. current throughput;
4. elapsed time;
5. ETA when available;
6. percentage only when a usable total exists.

Examples:

> **28.4 GiB copied · 45.1 MiB/s · 21:24 elapsed · ≈ 14:28 remaining**

If total is unavailable:

> **28.4 GiB copied · 45.1 MiB/s · 21:24 elapsed**
> Total size cannot be known reliably for this incremental/compressed stream.

If total is estimated, the UI prefixes percentage/ETA with `≈` and labels it **Estimated**.

The current stage bar remains only for non-transfer phases (prepare, mount guard, verify, Recovery Kit). It no longer masquerades as byte progress.

### 6.5 Parallel volumes

Harbor must aggregate concurrent volume transfers without hiding them.

Top line: aggregate current target rate and bytes.
Expandable detail: one row per volume/snapshot.

Aggregate percentage is shown only when each active transfer has a compatible total estimate.

## 7. Recovery intents

Recovery starts with **What are you trying to do?**

### 7.1 Replace this machine

Use case: new NVMe, same physical computer, reinstall after disk failure.

Defaults:

- preserve hostname;
- preserve normal user data/configuration;
- restore compatible system subvolumes;
- do not copy partition UUIDs blindly;
- rebuild `fstab`/`crypttab` against the actual target;
- regenerate initramfs/bootloader for the fresh disk;
- preserve machine identity only when the recovery plan confirms this is truly replacement rather than a concurrent clone.

The review page explicitly shows which identity values are preserved.

### 7.2 Migrate to another machine

Use case: restore one workstation environment onto another PC that may coexist with the source workstation.

Require a **new hostname** by default.

Default reconciliation:

- generate a new `/etc/machine-id`;
- regenerate SSH host keys;
- regenerate/reconcile initramfs for detected hardware;
- rebuild bootloader/EFI for target storage;
- rebuild `fstab` and `crypttab` from target UUIDs;
- avoid restoring hardware-bound NetworkManager assumptions blindly;
- flag detected third-party machine identities that require re-enrollment.

Harbor never silently duplicates machine-local identity.

### 7.3 Identity adapter registry

Machine-specific cleanup/reconciliation is modeled as adapters with:

- detection;
- risk explanation;
- default policy;
- supported action;
- verification result.

Initial required adapters:

1. hostname;
2. systemd machine-id;
3. OpenSSH host keys;
4. fstab/crypttab UUID reconciliation;
5. initramfs generator;
6. bootloader/EFI;
7. NetworkManager hardware-bound profiles (detect/review).

Third-party applications such as Tailscale and Syncthing are initially **detected and flagged for re-enrollment** rather than destructively rewritten without a dedicated adapter.

### 7.4 Distro-family compatibility

System/root recovery compares source Recovery Kit `os-release` to target `os-release`.

- same distro/family or explicitly compatible family: full system recovery path;
- materially different family: Harbor blocks blind root transplantation and offers **data + user configuration migration** instead.

This is a safety gate, not a branding allowlist.

## 8. Recovery wizard UX

Three top-level steps:

### Step 1 — Intent

- Replace this machine
- Migrate to another machine

For migration, enter new hostname here.

### Step 2 — Restore point

Human source name, destination, snapshot date, lineage, size when known, validation status.

Technical details remain expandable.

### Step 3 — Review & recover

Four concise groups:

- **Restored**
- **Adapted**
- **Regenerated**
- **Needs your attention**

Root/system activation still requires rescue/fresh-install context. Non-root restore may be staged from the running system.

## 9. Distro portability

### 9.1 Remove CachyOS as the primary product identity

README badges and primary headings become Linux/Btrfs oriented.

`install-cachyos.sh` remains only as a compatibility shim for existing links and prints a deprecation message pointing to the canonical Linux installer/release path.

The canonical installation story is:

1. Portable AppImage for manual backup/recovery.
2. Universal `.run` for persistent systemd automation.
3. Native DEB/RPM/Arch packages where available.
4. Source build for developers.

### 9.2 Capability detection, not distro branching

Runtime capabilities are discovered by command/interface availability:

- Btrfs tools;
- Snapper;
- systemd;
- package manager;
- initramfs generator;
- bootloader tooling;
- polkit;
- AppIndicator tray support.

Distro ID is metadata and compatibility context, not the central dispatch mechanism.

### 9.3 Support tiers

**Tier A — fully tested:** glibc + systemd + Btrfs families represented by Arch/CachyOS, Debian/Ubuntu, Fedora, openSUSE.

**Tier B — supported by capability:** other glibc/systemd/Btrfs distributions when required commands are present.

**Tier C — portable/manual best effort:** non-systemd or unusual libc environments. Manual backup/recovery may work, but persistent scheduling is not advertised until an init-system adapter exists.

The UI must tell the user which capability is missing rather than “unsupported distro”.

### 9.4 Recovery Kit package intent

Keep existing package intent capture but normalize provenance:

- pacman explicit + foreign;
- apt manual + dpkg inventory;
- rpm inventory plus dnf/zypper context when available.

Do not assume AUR concepts outside Arch-family systems.

### 9.5 Initramfs

Detect actual tools rather than infer from distro:

- mkinitcpio;
- dracut;
- update-initramfs;
- future adapters as needed.

Recovery review states exactly which tool will be used.

## 10. Packaging and test matrix

Release CI must include smoke tests representing:

- Arch Linux;
- Debian/Ubuntu;
- Fedora;
- openSUSE.

Required cross-family assertions:

- source discovery;
- engine discovery/fallback;
- Recovery Kit capture;
- package-manager detection;
- initramfs detection;
- portable helper invocation;
- no invalid UTF-8/mojibake.

AppImage builds must target an old-enough glibc baseline and be tested against the minimum claimed environments.

The universal installer must not regress pacman/apt/dnf/zypper support.

## 11. Data and API boundaries

### Rust/control plane

Add typed models for:

- `EngineStatus`;
- `EnginePolicy`;
- `EngineUpdateStrategy`;
- `RecoveryIntent`;
- `RecoveryIdentityAction`;
- `RecoveryCompatibility`;
- transfer telemetry events.

The desktop never parses arbitrary human engine text when a structured model can be used.

### Python engine

Add a versioned JSONL progress emitter around existing transfer state. Existing CLI text/Rich progress remains compatible.

No Harbor-specific UI concern enters the core transfer algorithm.

### Frontend

Create focused presentation components rather than enlarging `+page.svelte`:

- `EngineStatusRow.svelte`;
- `EngineManager.svelte`;
- `TransferProgress.svelte`;
- recovery intent/review components.

Keep existing CSS tokens and responsive breakpoints; avoid nested-card proliferation.

## 12. Update/security rules

1. Engine/app update checks use HTTPS and have explicit timeout/caching.
2. Harbor application updates use signed Harbor release/updater metadata.
3. Never execute an arbitrary update command derived from network text.
4. System engine package ownership must be locally detected before update.
5. No downgrade unless explicitly requested.
6. After every update, re-probe binary path/version/compatibility before marking success.
7. Bundled engine updates never overwrite `/usr/bin/btrfs-backup-ng`.

## 13. Failure states

The UI needs explicit states for:

- no Btrfs;
- no compatible engine;
- bundled fallback;
- stale/incompatible system engine;
- upstream version check unavailable/offline;
- no systemd;
- recovery source/target distro mismatch;
- no trustworthy total-size estimate;
- stalled transfer;
- target mount disappeared;
- update failed;
- restore identity reconciliation incomplete.

Each state names an action the user can take.

## 14. Acceptance criteria

The v0.2.5 work is complete only when all of the following are proven:

1. Overview shows active engine origin/version/path before any backup starts.
2. real-workstation raw compressed backup telemetry reports real growing target bytes, rate and elapsed time.
3. No exact-looking percentage/ETA is shown without a defensible total.
4. Parallel source telemetry is preserved and aggregate telemetry is honest.
5. Engine Manager supports Auto/System/Bundled selection.
6. Bundled-engine update routes through Harbor application update; recognized system engines have safe provenance-aware update handling.
7. Recovery begins with Replace this machine vs Migrate to another machine.
8. Migration requires/accepts a different hostname and regenerates core machine identity.
9. Cross-family root restore is blocked or downgraded to data migration.
10. Recovery review lists Restored / Adapted / Regenerated / Needs attention.
11. Primary product docs no longer present CachyOS as the default/required distro.
12. Arch/Debian/Fedora/openSUSE smoke matrix is green.
13. Existing v0.2.4 tray/background-backup behavior remains green.
14. Existing mount-disappearance protection remains green.
15. Existing staged-root recovery safety remains green.
16. Public tracked text passes UTF-8/mojibake/privacy gates.
17. Release artifacts pass checksums/attestation/package dependency checks.

## 15. Explicit non-goals for this version

- Reimplementing Btrfs send/receive inside Harbor.
- Building a new scheduler for OpenRC/runit/dinit.
- Pretending musl/non-FUSE AppImage environments are fully validated without test evidence.
- Automatically rewriting every third-party application's device identity.
- Sector-by-sector disk imaging.
- Restoring partition tables/EFI by blindly cloning source-disk metadata.

## 16. Implementation approach

Use the existing engine and control-plane architecture. Extend structured telemetry and recovery planning first, then expose them through small frontend components. Every change is test-first. No live reference-workstation backup is launched by development automation; the currently running user backup is observed read-only and may be used only as a telemetry reality check.

The implementation plan will be written only after this specification is explicitly reviewed, per the requested Superpowers workflow.
