# Btrfs Harbor v0.2.5 Recovery / Telemetry / Engine / Portability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Harbor expose its actual backup engine before every run, report honest byte/rate/ETA telemetry, distinguish same-machine recovery from migration to different hardware, and remove CachyOS-specific assumptions from the primary product path.

**Architecture:** Extend existing Harbor domain types and the inherited btrfs-backup-ng progress machinery instead of creating parallel backup/recovery engines. The Python engine emits versioned structured transfer events; the Rust control plane resolves engine/recovery/capability state into typed models; focused Svelte components present those models. Capability detection is command/interface based, while distro identity is used only for compatibility and package-manager context.

**Tech Stack:** Rust/Tauri 2, Svelte/TypeScript/Vitest, Python 3.12/pytest, btrfs-backup-ng, systemd/polkit, GitHub Actions, AppImage/DEB/RPM/Arch packaging.

**Spec:** `docs/superpowers/specs/2026-10-08-recovery-progress-engine-portability-design.md`

## Global Constraints

- Minimum compatible btrfs-backup-ng is **0.9.12**.
- Manual backup/recovery must remain useful from the portable AppImage without permanent installation.
- Persistent automation remains systemd-backed; non-systemd environments get explicit capability messaging instead of a fake universal scheduler.
- Never write to an underlying local directory when a declared NFS/SMB mount is absent.
- Never overwrite the live root filesystem from the running desktop application.
- Never show an exact-looking byte percentage or ETA when the total is only unknown/unreliable.
- A migration to different hardware must not silently duplicate machine identity.
- Harbor bundled engine updates must not overwrite `/usr/bin/btrfs-backup-ng`.
- Existing v0.2.4 tray/background backup behavior and SIGINT stop semantics must remain green.
- Public UI/docs must be distro-neutral; CachyOS/Arch may appear only as one tested family or compatibility shim.
- No live backup may be started on Pegasus by this implementation work; the user's existing backup is read-only evidence only.

## Review Focus

- **Compressed/incremental stream with unknown final size:** UI must show real bytes/rate/elapsed while withholding exact percentage/ETA until a defensible total exists; covered by Task 3/4 tests.
- **System engine present but stale/unowned:** Auto must fall back to bundled, System policy must explain/block, and update action must never overwrite an unknown manual install; covered by Task 1/2 tests.
- **Cross-family system recovery:** root transplant must be blocked/downgraded to data migration, not merely warned; covered by Task 5 tests.
- **Migration while source machine still exists:** hostname/machine-id/SSH host keys must be regenerated or explicitly reviewed so both machines can coexist; covered by Task 5/6 tests.
- **Non-systemd distro with Btrfs:** manual backup/recovery remains available while automatic scheduling is marked unavailable because of the missing capability, not “unsupported distro”; covered by Task 7 tests.

---

### Task 1: Engine Policy and Persistent Engine Status

**Files:**
- Modify: `harbor/crates/harbor-core/src/lib.rs`
- Modify: `harbor/crates/harbor-storage/src/lib.rs`
- Modify: `harbor/crates/harbor-engine/src/lib.rs`
- Modify: `harbor/crates/harborctl/src/main.rs`
- Modify: `harbor/desktop/src/lib/config.ts`
- Test: existing Rust module tests and `harbor/desktop/src/lib/config.test.ts`

**Interfaces:**
- Produces Rust `EnginePolicy { Auto, System, Bundled }`.
- Produces serializable `EngineCandidateStatus` and `EngineSelectionStatus`.
- Extends `HarborConfig` with `engine_policy`, defaulting to `auto` for old configs.
- Produces `resolve_engine(policy, system_candidate, bundled_candidate) -> Result<EngineSelectionStatus, EngineError>`.

- [ ] **Step 1: Write failing Rust tests for policy semantics.**
  - Auto prefers compatible system >=0.9.12.
  - Auto falls back to bundled when system is 0.9.11.
  - System policy returns an actionable incompatible/missing error instead of falling back.
  - Bundled policy ignores a newer system binary.
  - Status preserves both discovered candidates even when only one is active.

- [ ] **Step 2: Run the new engine tests and verify RED.**

Run:
`cargo test --manifest-path harbor/Cargo.toml -p harbor-engine --locked engine_policy`

Expected: FAIL because policy/status APIs do not yet exist.

- [ ] **Step 3: Write failing config round-trip tests.**
  - Missing `engine_policy` decodes as Auto.
  - `engine_policy = "bundled"` round-trips.
  - TypeScript default configuration also emits `auto`.

- [ ] **Step 4: Run Rust storage + frontend config tests and verify RED.**

- [ ] **Step 5: Implement the typed policy/status models and config migration.**
  Keep discovery side-effect free; no package update behavior belongs in this task.

- [ ] **Step 6: Make harborctl use the configured policy for run/status/verify resolution.**
  All existing helper fallback safety remains unchanged.

- [ ] **Step 7: Run task suite GREEN.**

Run:
`cargo test --manifest-path harbor/Cargo.toml -p harbor-core -p harbor-storage -p harbor-engine -p harborctl --locked && cd harbor/desktop && pnpm test -- --run src/lib/config.test.ts`

Expected: all pass.

- [ ] **Step 8: Commit.**

Commit: `feat: add explicit backup engine policy`

---

### Task 2: Engine Status Row, Engine Manager and Safe Update Provenance

**Files:**
- Create: `harbor/desktop/src/lib/EngineStatusRow.svelte`
- Create: `harbor/desktop/src/lib/EngineManager.svelte`
- Create: `harbor/desktop/src/lib/engine.test.ts`
- Modify: `harbor/desktop/src/lib/status.ts`
- Modify: `harbor/desktop/src/lib/agent.ts`
- Modify: `harbor/desktop/src/lib/i18n.ts`
- Modify: `harbor/desktop/src/routes/+page.svelte`
- Modify: `harbor/desktop/src-tauri/src/lib.rs`
- Modify: `harbor/desktop/src-tauri/Cargo.toml`
- Modify: `harbor/desktop/src-tauri/tauri.conf.json`

**Interfaces:**
- Consumes Task 1 `EngineSelectionStatus`.
- Produces Tauri commands `engine_status()`, `set_engine_policy(policy)`, `engine_update_options()`.
- Produces frontend `EngineSelectionStatus` mirror and `loadEngineSelectionStatus()`.
- Produces update provenance `distro_package | uv_tool | pipx | manual_unknown | bundled_harbor`.

- [ ] **Step 1: Write failing Rust tests for provenance detection.**
  Test pacman-owned path, dpkg-owned path, rpm-owned path, uv/pipx recognizable paths, and unknown/manual path.

- [ ] **Step 2: Verify RED.**

- [ ] **Step 3: Implement provenance detection using local package ownership commands with bounded timeouts.**
  No network access and no mutation in status detection.

- [ ] **Step 4: Write failing frontend tests for permanent Overview wording.**
  Assert examples:
  - bundled active, system missing;
  - compatible system active with path;
  - stale system fallback to bundled;
  - System policy blocked.

- [ ] **Step 5: Verify RED and implement `EngineStatusRow.svelte`.**
  It is a compact Overview row, not a nested card.

- [ ] **Step 6: Write failing tests for update action selection.**
  - Bundled => Harbor application update path.
  - Known distro-owned system binary => explicit package-manager action.
  - Unknown/manual => no destructive update button; guidance only.

- [ ] **Step 7: Integrate Tauri updater for Harbor application releases and Engine Manager.**
  Signed Harbor update metadata only; never execute commands sourced from network text.
  System package-manager update runs only after local ownership/provenance detection and explicit user action.

- [ ] **Step 8: Run task suite GREEN.**

Run:
`cargo test --manifest-path harbor/Cargo.toml -p btrfs-harbor-desktop --locked && cd harbor/desktop && pnpm test && pnpm check`

Expected: all pass, Svelte 0 errors/0 warnings.

- [ ] **Step 9: Commit.**

Commit: `feat: expose and manage backup engine`

---

### Task 3: Versioned Real Transfer Telemetry in btrfs-backup-ng

**Files:**
- Modify: `src/btrfs_backup_ng/core/progress.py`
- Modify: `src/btrfs_backup_ng/core/operations.py`
- Modify: `src/btrfs_backup_ng/endpoint/raw.py`
- Modify: `src/btrfs_backup_ng/endpoint/ssh.py`
- Modify as required: direct receive/chunked transfer callsites
- Create: `tests/test_progress_jsonl.py`
- Extend: `tests/test_progress.py`, `tests/test_chunked_transfer.py`, raw/SSH progress tests

**Interfaces:**
- Produces Python `TransferProgressEvent` schema version 1.
- Produces a machine-readable JSONL emitter enabled explicitly for Harbor/helper runs.
- Event nullable fields: `bytes_target`, `bytes_source`, `total_estimate`, `bytes_per_second`, `elapsed_seconds`, `eta_seconds`.
- Event identifies `volume`, `snapshot`, `destination`, `measurement`, `certainty`.

- [ ] **Step 1: Write failing unit tests for JSONL event serialization.**
  Ensure exact integer byte values, null unknown totals, version=1, and no ANSI/Rich output mixed into JSON records.

- [ ] **Step 2: Verify RED.**

- [ ] **Step 3: Implement the versioned event model/emitter in `core/progress.py`.**

- [ ] **Step 4: Write failing raw-target tests using a temporary growing `.part` file.**
  Assert:
  - target bytes equal actual file size;
  - rate is derived from monotonic delta;
  - elapsed increases;
  - unknown final compressed size leaves percentage/ETA absent;
  - an estimated total marks certainty as estimated.

- [ ] **Step 5: Verify RED and connect raw/local/NFS/SMB telemetry to the actual temporary target file.**
  Polling must be bounded/lightweight and must not alter the data path.

- [ ] **Step 6: Add RED→GREEN coverage for chunked and SSH/direct pipelines.**
  Reuse existing counters where bytes already traverse Python; otherwise expose a clearly named out-of-band measurement. Never label SSH protocol overhead as Btrfs payload bytes.

- [ ] **Step 7: Add parallel-volume telemetry test.**
  Events from two active transfers must remain independently identifiable and not share mutable counters.

- [ ] **Step 8: Run focused Python suite GREEN.**

Run:
`uv run --frozen --python 3.12 pytest -q tests/test_progress.py tests/test_progress_jsonl.py tests/test_chunked_transfer.py tests/test_raw_endpoint.py tests/test_ssh_endpoint.py`

Expected: all pass.

- [ ] **Step 9: Commit.**

Commit: `feat: emit real transfer telemetry`

---

### Task 4: Harbor Transfer Event Bridge and Honest Progress UI

**Files:**
- Modify: `harbor/crates/harborctl/src/main.rs`
- Modify: `harbor/desktop/src/lib/status.ts`
- Replace/refactor: `harbor/desktop/src/lib/BackupProgress.svelte`
- Create: `harbor/desktop/src/lib/TransferProgress.svelte`
- Create: `harbor/desktop/src/lib/transfer-progress.test.ts`
- Modify: `harbor/desktop/src/lib/ProtectionEditor.svelte`
- Modify: `harbor/desktop/src/routes/+page.svelte`
- Modify: `harbor/desktop/src/lib/i18n.ts`

**Interfaces:**
- Consumes Task 3 JSONL progress events.
- Produces typed Rust/TS `TransferProgressEvent`.
- Produces `aggregateTransferProgress(events)` with exact byte/rate aggregation and optional estimated percentage/ETA.

- [ ] **Step 1: Write failing harborctl tests for parsing/forwarding engine telemetry.**
  Human stdout/stderr remains ordinary output; recognized JSONL progress becomes structured Harbor progress events.

- [ ] **Step 2: Verify RED and implement bridge.**

- [ ] **Step 3: Write failing frontend aggregation tests.**
  Cover:
  - two simultaneous sources;
  - unknown total => no percentage/ETA;
  - estimated totals => prefix semantics and estimated flag;
  - exact byte/rate formatting;
  - zero/negative time delta safety.

- [ ] **Step 4: Verify RED and implement aggregation helpers.**

- [ ] **Step 5: Implement `TransferProgress.svelte`.**
  Main row: actual bytes + rate + elapsed + optional estimated ETA.
  Expandable source rows.
  Existing workflow stage bar stays for preparation/verification/recovery-kit phases only.

- [ ] **Step 6: Run frontend + helper tests GREEN.**

Run:
`cargo test --manifest-path harbor/Cargo.toml -p harborctl --locked && cd harbor/desktop && pnpm test && pnpm check && pnpm lint`

Expected: all pass, 0 Svelte warnings.

- [ ] **Step 7: Commit.**

Commit: `feat: show real backup throughput and eta`

---

### Task 5: Recovery Intent, Compatibility and Identity Reconciliation Model

**Files:**
- Modify: `harbor/crates/harbor-recovery/src/lib.rs`
- Modify: `harbor/crates/harborctl/src/recovery_kit.rs`
- Modify: `harbor/crates/harborctl/src/main.rs`
- Add fixtures under `harbor/crates/harbor-recovery/fixtures/`

**Interfaces:**
- Produces `RecoveryIntent { ReplaceMachine, MigrateMachine }`.
- Produces `RecoveryCompatibility { FullSystem, DataMigrationOnly, Blocked }`.
- Produces `RecoveryIdentityAction` entries with groups `restored | adapted | regenerated | attention`.
- Produces `RecoveryEnvironment` parsed from source Recovery Kit and target capability/os-release probes.
- Produces `plan_machine_recovery(request) -> MachineRecoveryPlan`.

- [ ] **Step 1: Write failing tests for same-machine replacement.**
  Preserve hostname by default, reconcile disk UUID/config, regenerate initramfs/bootloader, require rescue for system root.

- [ ] **Step 2: Write failing tests for migration.**
  A new hostname is required when source may coexist; machine-id and SSH host keys are regenerated; fstab/crypttab and initramfs are adapted.

- [ ] **Step 3: Write failing cross-family compatibility tests.**
  CachyOS/Arch-family → same family permits full-system path.
  Arch-family → Fedora-family blocks root transplant and returns DataMigrationOnly.
  File/directory data recovery remains available.

- [ ] **Step 4: Verify RED.**

- [ ] **Step 5: Implement intent, distro-family compatibility and identity-action planning.**
  Family matching uses parsed `ID`/`ID_LIKE`, not hardcoded CachyOS branches.

- [ ] **Step 6: Add initramfs/boot capability adapter tests.**
  Detect `mkinitcpio`, `dracut`, `update-initramfs` by executable availability.

- [ ] **Step 7: Extend Recovery Kit metadata only as needed for deterministic planning.**
  Preserve backward compatibility with schema v1 kits where possible; if schema must bump, add explicit migration/compatibility tests.

- [ ] **Step 8: Run task suite GREEN.**

Run:
`cargo test --manifest-path harbor/Cargo.toml -p harbor-recovery -p harborctl --locked recovery`

Expected: all pass.

- [ ] **Step 9: Commit.**

Commit: `feat: distinguish replacement and migration recovery`

---

### Task 6: Three-Step Recovery Wizard

**Files:**
- Refactor: `harbor/desktop/src/lib/RecoveryEditor.svelte`
- Create: `harbor/desktop/src/lib/RecoveryIntent.svelte`
- Create: `harbor/desktop/src/lib/RecoveryReview.svelte`
- Create: `harbor/desktop/src/lib/recovery.test.ts`
- Modify: `harbor/desktop/src/lib/agent.ts`
- Modify: `harbor/desktop/src/lib/status.ts`
- Modify: `harbor/desktop/src/lib/i18n.ts`

**Interfaces:**
- Consumes Task 5 machine recovery plan.
- Wizard steps: Intent → Restore point → Review & recover.
- Review groups: Restored / Adapted / Regenerated / Needs your attention.

- [ ] **Step 1: Write failing UI/model tests.**
  Cover default replacement mode, migration hostname requirement, cross-family data-only warning, and identity review grouping.

- [ ] **Step 2: Verify RED.**

- [ ] **Step 3: Implement intent step and hostname validation.**

- [ ] **Step 4: Implement restore-point step using existing listing/lineage data.**

- [ ] **Step 5: Implement review step and safety copy.**
  No root “execute now” action on a running root; show rescue/fresh-install requirement.

- [ ] **Step 6: Run UI suite GREEN.**

Run:
`cd harbor/desktop && pnpm test && pnpm check && pnpm lint && pnpm build`

Expected: all pass.

- [ ] **Step 7: Commit.**

Commit: `feat: add machine-aware recovery wizard`

---

### Task 7: Capability-Based Multi-Distro Product Path and CI Matrix

**Files:**
- Create/modify capability module in `harbor/crates/harborctl/src/` or focused crate file
- Modify: `harbor/crates/harborctl/src/recovery_kit.rs`
- Modify: `README.md`, `README.fr.md`, `README.zh-CN.md`
- Modify: `install-cachyos.sh`
- Modify: universal installer/package docs as needed
- Modify: `.github/workflows/harbor.yml` and/or add focused smoke workflow
- Add distro capability fixtures/tests

**Interfaces:**
- Produces `PlatformCapabilities` including Btrfs, Snapper, systemd, package manager, initramfs tool, bootloader tools and tray availability.
- Produces support state based on capabilities, not distro allowlisting.

- [ ] **Step 1: Write failing capability detection tests.**
  Fixtures for Arch/CachyOS, Debian/Ubuntu, Fedora, openSUSE, and a non-systemd Btrfs distro.

- [ ] **Step 2: Verify RED and implement command/capability detection.**
  Distro `ID`/`ID_LIKE` is context only.

- [ ] **Step 3: Write failing Recovery Kit package-intent tests.**
  pacman explicit/foreign; apt manual/dpkg inventory; rpm inventory with dnf/zypper context. No AUR language outside Arch family.

- [ ] **Step 4: Verify RED and implement.**

- [ ] **Step 5: Make `install-cachyos.sh` a compatibility shim.**
  It must delegate to the canonical universal/Linux installer path and print a deprecation notice; no duplicate install logic.

- [ ] **Step 6: Rewrite primary README install/support sections.**
  Product badge/text becomes Linux + Btrfs; CachyOS appears as one tested Arch-family example only.

- [ ] **Step 7: Add CI smoke matrix for Arch, Debian/Ubuntu, Fedora and openSUSE.**
  Verify capability parsing, engine fallback, Recovery Kit probes, portable helper invocation where feasible, and UTF-8 tracked-text gate.

- [ ] **Step 8: Run local distro fixture and docs/package tests GREEN.**

Run:
`cargo test --manifest-path harbor/Cargo.toml -p harborctl --locked platform && uv run --frozen --python 3.12 pytest -q tests/test_harbor_portable_wrapper.py tests/test_packaging_data_files.py && git diff --check`

Expected: all pass.

- [ ] **Step 9: Commit.**

Commit: `feat: make harbor capability-driven across linux distros`

---

### Task 8: Version, Full Regression, Packaging and Release Evidence

**Files:**
- Version sources for next release
- Handoff/research docs only after fresh evidence
- No unrelated source changes

**Interfaces:**
- Consumes all previous tasks.
- Produces a release candidate whose artifacts are independently verifiable.

- [ ] **Step 1: Bump the release version consistently only after Tasks 1–7 are green.**

- [ ] **Step 2: Run full Python 3.12 gates.**

Run:
`uv sync --frozen --extra test --extra dev --python 3.12 && uv run --frozen --python 3.12 pytest -q && uv run --frozen --python 3.12 ruff check src/ tests/ && uv run --frozen --python 3.12 ruff format --check src/ tests/ && uv run --frozen --python 3.12 mypy src/btrfs_backup_ng --ignore-missing-imports`

Expected: zero failures.

- [ ] **Step 3: Run full Rust gates.**

Run:
`cargo fmt --manifest-path harbor/Cargo.toml --all -- --check && cargo test --manifest-path harbor/Cargo.toml --workspace --locked && cargo clippy --manifest-path harbor/Cargo.toml --workspace --all-targets --locked -- -D warnings`

Expected: zero failures/warnings.

- [ ] **Step 4: Run full frontend gates.**

Run:
`cd harbor/desktop && pnpm test && pnpm check && pnpm lint && pnpm build`

Expected: all pass, Svelte 0 errors/0 warnings.

- [ ] **Step 5: Run text/privacy/version/package contract gates.**
  Explicitly scan tracked text for invalid UTF-8, U+FFFD, mojibake and private machine/path tokens introduced by this branch.

- [ ] **Step 6: Build local AppImage/DEB/RPM/.run via the existing release chain.**
  Verify checksums and inspect AppImage runtime dependencies.

- [ ] **Step 7: Perform no-write portable smoke tests.**
  Engine status must show the bundled engine when no system engine exists; no backup is started.

- [ ] **Step 8: Whole-branch review package and one independent review pass.**
  If no reviewer/subagent tool is available, perform the documented separate self-review and record that limitation.

- [ ] **Step 9: Commit release/docs evidence.**

Commit: `chore: prepare btrfs harbor release`

- [ ] **Step 10: Push/tag/release is an external side effect.**
  Stop for explicit confirmation unless the user has already explicitly authorized publication of this release after review.
