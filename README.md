<p align="center">
  <img src="branding/logo.svg" alt="Btrfs Harbor" width="560">
</p>

<h1 align="center">Btrfs Harbor</h1>

<p align="center">
  <strong>⚓ Snapshots in. Systems back.</strong><br>
  Native Linux backup, verification, recovery and system replication for Btrfs.
</p>

<p align="center">
  <a href="https://github.com/GodsQuantum/btrfs-harbor/actions/workflows/harbor.yml"><img alt="Harbor CI" src="https://github.com/GodsQuantum/btrfs-harbor/actions/workflows/harbor.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT license" src="https://img.shields.io/badge/license-MIT-3dd7cf"></a>
  <img alt="Linux" src="https://img.shields.io/badge/platform-Linux-0B1622">
  <img alt="Btrfs" src="https://img.shields.io/badge/filesystem-Btrfs-58D6C5">
  <img alt="Tauri" src="https://img.shields.io/badge/desktop-Tauri%202-24C8DB">
  <img alt="systemd" src="https://img.shields.io/badge/automation-systemd-5C6BC0">
</p>

<p align="center">
  <a href="README.md">English</a> ·
  <a href="README.fr.md">Français</a> ·
  <a href="README.zh-CN.md">简体中文</a>
</p>

<p align="center">
  <img src="docs/assets/screenshots/overview.png" width="100%" alt="Btrfs Harbor overview with generic demo backup data">
</p>

Btrfs Harbor turns **local Btrfs/Snapper snapshots into real off-host backups**. It adds mount-aware destinations, full + incremental Btrfs send streams, checksum verification, systemd scheduling, staged recovery and safe Proxmox LXC replication behind one native desktop UI.

A local snapshot on the same disk is useful, but it is **not** an off-host backup. Harbor keeps that distinction visible everywhere.

## v0.2.6-rc.1 — checkpointed Resume preview

**Experimental native desktop controls:** Send latest stable Snapper snapshot, select a specific snapshot number, create a new snapshot, and Pause / Stop / Resume / Discard partial. History now reads real manifests, including private journals with polkit when required. Snapper cleanup pins protect unfinished source snapshots. The legacy Back up now button and existing systemd timers are still on the original backup engine; they are NOT checkpointed-v2 sends.

**Experimental CLI:** raw checkpoint-v2 start/resume/list/status/pause/stop/discard. Start and Resume require --experimental, non-network local targets require --allow-local, and Discard requires explicit confirmation. Resume regenerates and checks the raw Btrfs send locally from byte zero, but NEVER retransmits committed destination frames. Legacy v0.2.5 partials without manifests cannot be resumed.

**Release-candidate limitations:** Real interrupted Btrfs full/incremental send/receive, a physical NFS-outage test, independent v2 systemd timers, and all package/distribution acceptance remain pending. Use only for supervised tests, not as your only disaster-recovery backup. Stable legacy raw/SSH behavior is preserved.

## ✨ Why Harbor?

- **Real full + incremental Btrfs backups** — preserve snapshot lineage instead of recopying everything.
- **NFS / SMB / local / SSH destinations** — including raw streams on ordinary filesystems.
- **No silent NAS fallback** — network destinations are positively identified before any write.
- **Verification built in** — optional checksum verification after backup.
- **Persistent scheduling** — UUID-scoped native systemd services and timers.
- **Recovery Kits** — portable topology, package, Snapper and boot information beside backups.
- **Staged recovery** — restore and verify away from the live root before activation.
- **Proxmox LXC replication** — deploy userspace into a stopped container with native snapshot/rollback.
- **Least-privilege desktop** — read-only D-Bus agent plus a fixed privileged helper; no generic root shell.
- **English / Français / 简体中文** — first-class UI and documentation.
- **No Docker runtime** — Harbor installs as a native Linux desktop application.

The latest experimental preview is **v0.2.6-rc.1**, available under [prereleases](https://github.com/GodsQuantum/BTRFS-Harbor/releases/tag/v0.2.6-rc.1). Its checkpoint panel is distinct from the stable Back up now workflow.

## 📦 Download & install

The easiest way to **try Harbor or run manual backups** is the **Portable AppImage**. It stays portable: opening it does not install Harbor. It can detect the current Btrfs layout, choose sources and destinations, validate the target, browse backup snapshots, run **Back up now**, and stage a restore. Privileged Btrfs operations use polkit only when needed.

If you close the Harbor window while a manual backup is running, Harbor explains that the backup will continue, hides to a **temporary system-tray icon**, and keeps the transfer alive. The tray menu can reopen Harbor or **stop the active backup**. The tray disappears automatically when the run finishes.

Install Harbor on the system only when you want **scheduled/background backups that start by themselves** while Harbor is not open: systemd scheduling, startup integration and “after each Snapper snapshot” triggers.

The release provides:

- **Portable AppImage** — complete manual backup + recovery UI without system installation.
- **Universal `.run` installer** — x86_64 glibc + systemd Linux; installs persistent automation and may install required host tools through pacman, apt, dnf or zypper.
- **DEB** — Debian / Ubuntu and derivatives.
- **RPM** — Fedora / RHEL-family / openSUSE-compatible workflows.
- **SHA256SUMS** — checksums for every Linux release artifact.

Universal installer:

```bash
curl -LO https://github.com/GodsQuantum/BTRFS-Harbor/releases/download/v0.2.5/BTRFS-Harbor-0.2.5-linux-x86_64.run
chmod +x BTRFS-Harbor-0.2.5-linux-x86_64.run
./BTRFS-Harbor-0.2.5-linux-x86_64.run
```

Harbor **prefers a compatible system `btrfs-backup-ng` (>= 0.9.12)** when one is already installed. Otherwise it uses the compatible engine bundled with Harbor. Harbor no longer replaces or conflicts with a user-installed upstream engine.

## 🐧 Linux support

Harbor is capability-driven rather than tied to a distro name.

- **Manual backup + recovery:** requires Linux, Btrfs userspace tools and the capabilities used by the selected workflow. The Portable AppImage does not require Harbor to be installed.
- **Persistent automatic scheduling:** additionally requires systemd. A non-systemd Btrfs system remains usable for manual backup/recovery; Harbor simply reports automatic scheduling as unavailable.
- **Tier-A release families:** Arch/CachyOS, Debian/Ubuntu, Fedora and openSUSE are exercised as representative glibc/systemd/Btrfs families.
- **Other glibc/systemd distributions:** supported by detected capabilities rather than an allowlist.
- **Package/install channels:** the universal `.run` detects pacman, apt, dnf or zypper. Native DEB/RPM/Arch recipes remain available where appropriate.

The legacy `install-cachyos.sh` name is retained only as a compatibility shim and delegates to the distro-neutral `install-linux.sh`. It contains no CachyOS-specific installation logic.

## 🛠️ Build from source

Source builds are for contributors and packagers. They use the same Python, Rust and Svelte/Tauri toolchain documented under **Development** below. Arch users may additionally inspect `packaging/arch/PKGBUILD`; that recipe is an Arch packaging option, not Harbor's portability layer.

> Harbor uses polkit for privileged operations. Do not run the desktop application as root.

## 🛟 Your first backup

1. Open Harbor. It identifies the current computer and scans its mounted **Btrfs subvolumes**, Snapper configurations and existing sendable read-only snapshots.
2. Under **What will be backed up**, review the detected persistent subvolumes. Harbor presents human labels first and keeps `@`, mount paths and Snapper details secondary.
3. Choose a destination:
   - **Folder** — select exactly one directory and press **Validate**. Harbor detects local/NFS/SMB internally and keeps mount-safety details out of the normal UI.
   - **SSH server** — enter host, user, port and remote folder, then test the connection.
4. Click **Back up now**. This works from the Portable AppImage without installing Harbor.
5. If you want recurring/background backups, choose the schedule and click **Install Harbor for automatic backups**. Installation exists for persistence/automation, not for manual backup.
6. Confirm the first run becomes **BACKED UP** and **VERIFIED**.
7. Open **Recover**, select a real backup snapshot and perform a staged restore test.

Nothing is pre-filled with a demo IP, machine name, recent directory or private source path in the native first-run flow.

## 🧭 What the status means

Harbor deliberately separates local snapshots from off-host protection:

| State | Meaning |
| --- | --- |
| **LOCAL** | Snapshot exists only on the source machine |
| **BACKED UP** | A destination contains the snapshot stream |
| **VERIFIED** | Stored raw stream passed verification |
| **FULL** | Independent full send |
| **INCREMENTAL** | Send references a valid parent chain |
| **RESTORE TESTED** | A staged recovery rehearsal completed |

## ♻️ Recover a snapshot

The **Recover** tab reads the real backup repository and lists available snapshots newest first. Recovery starts by asking whether you are **replacing this machine** (for example a fresh NVMe in the same computer) or **migrating to another machine**. A migration asks for a new hostname and plans regeneration/adaptation of machine identity, SSH host keys, storage UUID references, initramfs and boot state so the source and restored computers can coexist safely.

Choose the protected subvolume, destination and exact snapshot, then restore it into a separate **Btrfs staging location**. Harbor verifies the received data before considering the rehearsal complete.

- A non-root subvolume can be staged from the running system.
- The live `/` root is never overwritten in place. Root/system recovery is intentionally redirected to a **rescue/live-ISO workflow** using the Recovery Kit.
- The Recovery Kit records system topology, `fstab`/`crypttab`, Snapper configuration, package inventories, boot/initramfs information and Harbor intent beside the backup.
- Btrfs Assistant can still be useful afterwards for inspecting or rolling back local Snapper snapshots, but Harbor does not require it for receiving the off-host backup.

A Btrfs snapshot does not automatically include EFI partitions, partition tables or secrets outside the selected Btrfs subvolumes. Harbor therefore reports recovery coverage instead of promising “whole-computer recovery” when those prerequisites are not covered.

## 🧬 System Replica

Harbor can reuse Linux userspace on another target without pretending hardware state is portable.

- **Physical machine** — userspace plus explicit boot/hardware reconciliation.
- **VM** — userspace plus virtual-hardware reconciliation.
- **LXC** — userspace only; physical EFI/kernel state is excluded.

For a stopped Proxmox LXC, Harbor can create a native PVE rollback snapshot, mount the target rootfs, preserve target networking/hostname state, respect the actual LXC UID/GID namespace, reset machine identity and SSH host keys, then roll back automatically if deployment fails.

The LXC path has been rehearsed end-to-end on a disposable unprivileged ZFS-backed container.

## 🛡️ Safety model

### No silent local fallback

For NFS and SMB, Harbor stores the expected mount point **and** server/share. The helper checks the live mount table before a backup starts. The generated engine configuration also uses `require_mount` as a second guard.

### No arbitrary privileged shell

The desktop stays unprivileged. Read-only state comes from a narrow system D-Bus service. Mutating actions invoke only fixed, validated Harbor helper commands through polkit.

### Recovery is staged

```text
plan → verify chain → receive into staging → verify staging → reconcile → activate
```

Harbor refuses to overwrite the running root filesystem in place.

### Credentials stay out of Recovery Kits

Recovery Kits contain system intent and topology, not secret credential files.

## 🏗️ Architecture

```mermaid
flowchart LR
    UI["Tauri 2 + SvelteKit"] -->|read-only D-Bus| Agent["Rust harbor-agent"]
    UI -->|polkit + fixed helper| Ctl["Rust btrfs-harborctl"]
    Agent --> Engine["btrfs-backup-ng engine"]
    Ctl --> Engine
    Ctl --> Systemd["systemd timer/path units"]
    Engine --> Btrfs["Btrfs / Snapper"]
    Engine --> Raw["raw:// streams"]
    Raw --> Target["NFS / SMB / local filesystem"]
    Recovery["Rust harbor-recovery"] --> Raw
```

Harbor is based on [berrym/btrfs-backup-ng](https://github.com/berrym/btrfs-backup-ng). The inherited Python engine keeps its mature send/receive, retention, raw metadata, locking and restore logic; Harbor adds a Rust control plane and native desktop.

## ⚙️ Example configuration

A fully generic example is provided at [examples/harbor.toml](examples/harbor.toml).

Harbor writes its live configuration under:

```text
/etc/btrfs-harbor/harbor.toml
```

Generated engine profiles live under:

```text
/var/lib/btrfs-harbor/generated/
```

## 🧪 Development

```bash
uv sync --frozen --extra test --extra dev
uv run --frozen pytest -q

cd harbor
cargo test --workspace --exclude btrfs-harbor-desktop --locked

cd desktop
pnpm install --frozen-lockfile
pnpm test
pnpm check
pnpm build
```

The repository also includes CI for the Python engine, Rust control plane, desktop frontend, Tauri build and packaging files.

## 📦 Project status

Btrfs Harbor is an early public **v0.2**. Core backup, verification, staged recovery and stopped-target LXC replication paths have been exercised with disposable integration rehearsals.

**Do not make an unreleased backup tool the only copy of important data.** Keep another backup and prove a restore before trusting any backup workflow.

## 🤝 Upstream heritage

Btrfs Harbor is based on **btrfs-backup-ng** by Michael Berry and contributors. The inherited engine remains MIT licensed and the upstream history is preserved. The original upstream README is kept at [docs/upstream/UPSTREAM_ENGINE_README.md](docs/upstream/UPSTREAM_ENGINE_README.md).

## 📄 License

MIT — see [LICENSE](LICENSE).
