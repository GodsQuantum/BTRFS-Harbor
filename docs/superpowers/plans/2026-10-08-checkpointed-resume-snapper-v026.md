# Btrfs Harbor v0.2.6 — Checkpointed Resume and Snapper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans or superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve every remotely committed checkpoint across crash/reboot without a full local spool, with independent Snapper snapshot sourcing and remote cadence.

**Architecture:** Extend the existing raw endpoint with a separate v2 framed checkpointing sink and manifest; replay the immutable btrfs send locally and compare SHA-256 checkpoints before appending. Orchestrate persistent pause, Snapper native cleanup pinning, cadence, mount-identity safety, existing Rust control and Svelte desktop UX. Maintain compatibility with final legacy raw streams.

**Tech Stack:** Python 3.11–3.13, pytest, native btrfs-progs, zstd, Snapper, Rust/Tauri 2, Svelte 5, systemd where available.

**Spec:** docs/superpowers/specs/2026-10-08-checkpointed-resume-snapper-design.md (written spec approved 2026-10-08; priority rule appended with approval).

## Global Constraints

- All builds/tests/edits in Cloud9 CT700, as arezki, within existing canonical worktree; never change mounts or dev machines.
- Release base v0.2.5 e6be1e0cd9a567d4cd6faa53036805862b24edf4; preserve all its safety and recovery contracts.
- Checkpoint raw default 134217728 bytes (128 MiB) pending recorded 64/128/256 MiB benchmark.
- Final non-encrypted output is a single concatenated-zstd .btrfs.zst + .meta; ordinary zstd -dc | btrfs receive interoperability required.
- At most one raw checkpoint and bounded buffers/temp; absolutely no full local spool.
- Manifest is only authoritative after durable data+manifest+directory fsync; no fake Resume, no fake ETA, no partial as restore point.
- Every append and commit requires fail-closed mount identity; pin open destination directory descriptor, never local fallback.
- GUI may exit without aborting worker; pause ends active worker and preserves source; no mandatory custom daemon.
- Snapshot source policy: create_new / latest_snapper / selected_snapper; send cadence independent: manual/hourly/daily/weekly/calendar/after_snapshot.
- Resume must use original immutable snapshot. If newer Snapper snapshot appears, daily-latest never replaces pending transfer silently.
- Native Snapper cleanup metadata pin; identity/lock/refcount validation before release; manual source deletion keeps partial as needs-source.
- Existing whole-stream encryption and unsupported endpoints remain non-resumable, clearly indicated.
- Do not release or run a large Pegasus backup until synthetic/real fixture crash/replay tests pass.
- One TDD red→green→refactor cycle and focused commit per reviewable deliverable; full regression before final claim.

## Source/File Ownership Map

| Unit | Responsibility |
|---|---|
| core/checkpoint_v2.py | Strict schema, immutable source/target fingerprints, checkpoint index digest and atomic state journal |
| endpoint/resumable_raw.py | Single partial stream, durable bounded append, recovery and final publish |
| core/checkpoint_stream.py | Fixed raw boundaries and independent zstd frames |
| core/replay_v2.py | Match committed prefix with zero remote writes |
| core/resumable_worker.py | Pause/resume/stop/discard lifecycle |
| snapper/source_policy.py and pin.py | Native snapshot selection, guarded cleanup pin |
| core/snapper_cadence.py | Separate daily-latest/after-snapshot scheduling and collision policy |
| endpoint/mount_guard_v2.py | Mount identity and pinned directory FD |
| harbor/* and desktop | Native controls, state/reporting, timeline |
| core/performance_v2.py | Balanced/Fast/Custom resource limits |
| core/verify_v2.py | Indexed/raw/semantic verify, legacy compatibility |

## Material Design Clarifications

1. `STATX_MNT_ID_UNIQUE` is scoped to the live mount and may change across reboot. Persistent remote identity must use separately revalidated server/export/fs identity; current mount ID prevents live fallthrough.
2. Checkpoint durability requires final directory sync where meaningful, not only manifest rename. On a network filesystem unable to guarantee durability, refuse to promise checkpoint persistence.
3. Stream and sidecar renames are not one atomic transaction. Discovery must treat metadata/commit marker as publish authority and reconcile crash between either rename.
4. Snapper pin activation precedes source send. Pin release is identity-checked and concurrency-safe when transfers share the same snapshot.
5. Once a transfer is pending, subsequent Daily Latest jobs must not rebase it onto a newer snapshot. The user must explicitly finish or discard the pending job; no hidden substitution.

## Review Focus (failure-mode to test mapping)

- NFS mount disappears immediately after validation: pinned fd must not write to underlying local path (Task 9).
- System reboots with same NAS export but new kernel mount ID: safe revalidation permits recovery, not unconditional rejection (Task 9).
- Crash between successful stream rename and final sidecar: missing/unfinished commit must never appear in Recover (Tasks 3 and 12).
- Two jobs pin the same Snapper snapshot: finishing one never unpins the other (Task 7).
- Newer Snapper snapshot appears while 90% of prior transfer is paused: Resume uses original UUID and Daily Latest does not replace it (Tasks 6 and 8).

## Implementation Order

Each task is TDD and individually reviewable. In every task, add only one failing behavior at a time; do not implement subsequent behavior before observing its test fail.

### Task 1: Manifest v2 — identity, schema, canonical digest

**Files:** Create src/btrfs_backup_ng/core/checkpoint_v2.py; Test tests/test_checkpoint_v2.py

**Interfaces:** Checkpoint(seq:int, raw_offset:int, raw_length:int, compressed_offset:int, compressed_length:int, raw_sha256:str, compressed_sha256:str, committed_at:str); ResumeManifest(schema_version:int, transfer_id:str, identity:dict[str,str|int|None], checkpoint_size:int, state:str, checkpoints:list[Checkpoint]); parse_manifest(data:bytes)->ResumeManifest; serialize_manifest(manifest:ResumeManifest)->bytes

- [ ] **Step 1: Add failing behavior test(s)** — test_manifest_round_trip_canonical_digest; test_manifest_rejects_noncontiguous_offsets; test_manifest_rejects_bad_sha_or_index_digest; test_manifest_rejects_untrusted_paths_and_unsupported_version. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_checkpoint_v2.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Canonical stable JSON with sorted keys; index SHA256 must cover canonical ordered records; validate positive bounded lengths/strict ordering, 1..n numbering, supported state, source/parent/destination fingerprints. Never parse manifest filesystem paths into arbitrary write destinations.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_checkpoint_v2.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: manifest v2 — identity, schema, canonical digest"`.

### Task 2: Durable manifest journal and crash repair

**Files:** Modify src/btrfs_backup_ng/core/checkpoint_v2.py; Test tests/test_checkpoint_v2.py

**Interfaces:** commit_manifest(manifest_path:Path, manifest:ResumeManifest, *, validate_destination:Callable[[],None])->None; reconcile_part(part_path:Path, manifest:ResumeManifest, *, validate_destination:Callable[[],None])->int

- [ ] **Step 1: Add failing behavior test(s)** — test_checkpoint_commit_fsync_before_rename; test_crash_after_stream_fsync_discards_tail; test_short_part_refuses_resume; test_missing_manifest_never_infers_resume; test_fsync_failure_refuses_durable_commit. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_checkpoint_v2.py -k 'commit or crash or part or fsync'`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Atomic temp create O_EXCL/O_NOFOLLOW and mode 0600, fsync manifest file, os.replace, directory fsync; commit success only after required directory fsync. Validate mount both before writes and before commit. Any fsync failure is fail closed; explicit surface for unsupported durability on network storage.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_checkpoint_v2.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: durable manifest journal and crash repair"`.

### Task 3: Resumable raw destination sink

**Files:** Create src/btrfs_backup_ng/endpoint/resumable_raw.py; Modify src/btrfs_backup_ng/endpoint/raw.py (feature-gated path only); Test tests/test_resumable_raw_sink.py

**Interfaces:** ResumableRawSink.open_new(root:Path, name:str, transfer_id:str, guard:MountGuard)->ResumableRawSink; append_frame(raw_sha256:str, frame:bytes, raw_length:int)->Checkpoint; recover()->ResumeManifest; publish(meta:bytes)->Path

- [ ] **Step 1: Add failing behavior test(s)** — test_open_uses_exclusive_nofollow; test_append_is_durable_only_after_manifest; test_resume_never_overwrites_checkpoint; test_publish_requires_metadata_coverage; test_recover_between_final_and_meta_does_not_discover_final. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_resumable_raw_sink.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Keep a pinned destination directory descriptor and use dirfd-relative operations; no symlink traversal; keep final stream and meta as transaction with recovery after either rename; reuse raw endpoint existing integrity and discovery.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_resumable_raw_sink.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: resumable raw destination sink"`.

### Task 4: Bounded zstd checkpoint stream

**Files:** Create src/btrfs_backup_ng/core/checkpoint_stream.py; Test tests/test_checkpoint_stream.py

**Interfaces:** raw_checkpoints(reader:BinaryIO, chunk_size:int=134217728)->Iterator[bytes]; compress_frame(checkpoint:bytes, *, level:int, threads:int)->bytes; feed_checkpointed_stream(source:BinaryIO,sink:ResumableRawSink,settings:CheckpointSettings)->TransferSummary

- [ ] **Step 1: Add failing behavior test(s)** — test_fixed_raw_boundaries_and_short_tail; test_zstd_frames_concatenate_and_decompress_exactly; test_reader_is_bounded_no_full_spool; test_64_128_256_mib_microbenchmark_records. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_checkpoint_stream.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Use independent zstd frames; never compress entire source at once. Benchmark 64/128/256 MiB using generated small Btrfs-like fixtures and capped memory; record reproducible data before freezing 128 MiB.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_checkpoint_stream.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: bounded zstd checkpoint stream"`.

### Task 5: Replay immutable source / fast-forward

**Files:** Create src/btrfs_backup_ng/core/replay_v2.py; Test tests/test_replay_v2.py

**Interfaces:** validate_resume_identity(manifest:ResumeManifest,source:SourceFingerprint,destination:DestinationFingerprint)->None; replay_committed(reader:BinaryIO,manifest:ResumeManifest,progress:Callable[[ReplayProgress],None])->int

- [ ] **Step 1: Add failing behavior test(s)** — test_replay_matches_checkpoint_prefix_without_writes; test_first_mismatch_stops_before_append; test_missing_source_or_incremental_parent_refused; test_changed_kernel_replay_requires_hash_match; test_replay_no_recompression. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_replay_v2.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Require readonly source and exact UUID/parent identity; raw SHA256 comparison for every committed checkpoint; no destination writes during replay; version differences diagnostic, not a bypass.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_replay_v2.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: replay immutable source / fast-forward"`.

### Task 6: Persistent pause / stop / discard worker

**Files:** Create src/btrfs_backup_ng/core/resumable_worker.py; Modify src/btrfs_backup_ng/core/operations.py; Modify harbor/crates/harborctl/src/*.rs relevant command parser; Test tests/test_resumable_worker.py and Rust controller tests

**Interfaces:** request_pause(transfer_id:str)->None; stop_now(transfer_id:str)->None; resume_transfer(transfer_id:str)->TransferState; discard_transfer(transfer_id:str,confirmed:bool)->None

- [ ] **Step 1: Add failing behavior test(s)** — test_pause_commits_current_then_worker_exits; test_stop_now_preserves_committed_prefix; test_restart_recovers_paused; test_resume_stays_on_original_source_even_if_new_snapper_snapshot_exists; test_discard_requires_confirmation. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_resumable_worker.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Pause has zero worker CPU/I/O after exit; preserve pin. Existing v0.2.5 tray and broken-pipe isolation must remain intact. No permanent daemon.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_resumable_worker.py; cargo test -p harborctl --manifest-path harbor/Cargo.toml`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: persistent pause / stop / discard worker"`.

### Task 7: Snapper policy, immutable identity and pin

**Files:** Create src/btrfs_backup_ng/snapper/source_policy.py and src/btrfs_backup_ng/snapper/pin.py; Extend current snapper scanner; Test tests/test_snapper_source_policy.py and tests/test_snapper_pin.py

**Interfaces:** select_eligible(snapshots:list[SnapperSnapshot],policy:SourcePolicy,remote_uuids:set[str],now:datetime)->SnapperSnapshot|None; pin_source(snapshot:SnapperSnapshot,transfer_id:str)->PinRecord; release_pin(pin:PinRecord)->None

- [ ] **Step 1: Add failing behavior test(s)** — test_lone_pre_is_not_auto_selected; test_matched_pre_post_chooses_post; test_remote_uuid_skip; test_pin_before_send; test_success_discard_uuid_safe_cleanup_restore; test_stale_concurrent_pin_retained. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_snapper_source_policy.py tests/test_snapper_pin.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Snapper native cleanup clearing only; serialize same-source pin lifecycle across concurrent jobs with locks/refcounts. Never restore if another active resume depends on snapshot. Manual/root deletion => needs-source, partial retained.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_snapper_source_policy.py tests/test_snapper_pin.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: snapper policy, immutable identity and pin"`.

### Task 8: Cadence separation and Daily Latest

**Files:** Create src/btrfs_backup_ng/core/snapper_cadence.py; Modify src/btrfs_backup_ng/config/schema.py; Update native systemd path/timer adapters; Test tests/test_snapper_cadence.py

**Interfaces:** plan_remote_send(policy:SourcePolicy,cadence:SendCadence,candidates:list[SnapperSnapshot],pending:list[ResumeManifest],remote_uuids:set[str])->SendDecision

- [ ] **Step 1: Add failing behavior test(s)** — test_daily_picks_only_latest_stable; test_pre_post_incomplete_is_skipped; test_pending_resume_never_retargeted; test_conflict_requires_explicit_finish_or_discard; test_after_snapshot_preserves_source_policy. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_snapper_cadence.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — User-approved rule: Resume always targets originally pinned immutable snapshot; new Daily Latest never silently substitutes a newer snapshot into an unfinished job; provide explicit finish/discard choice. No extra polling daemon.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_snapper_cadence.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: cadence separation and daily latest"`.

### Task 9: Mount identity fail-closed

**Files:** Create src/btrfs_backup_ng/endpoint/mount_guard_v2.py; Integrate in resumable raw sink; Test tests/test_mount_guard_v2.py

**Interfaces:** capture_mount_identity(directory:Path)->MountIdentity; MountGuard.validate_fd(fd:int)->None; MountGuard.validate_before_append_and_commit()->None

- [ ] **Step 1: Add failing behavior test(s)** — test_mount_removed_never_writes_underlying_path; test_same_share_remounted_after_reboot_revalidates; test_changed_export_rejected; test_toctou_uses_pinned_directory_fd; test_statx_fallback_mountinfo. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_mount_guard_v2.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Unique mount ID is boot-scoped; combine persistent export/server/fs identity with current mount ID. Reacquire/pin fd safely on restart; refuse failed mount identity and no-underlying-local writes.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_mount_guard_v2.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: mount identity fail-closed"`.

### Task 10: Progress, status, settings, unified timeline

**Files:** Modify harbor/crates/harborctl/src/*.rs and harbor/desktop/src/lib/*.ts / route components confirmed by live inventory; Add Rust/TS tests

**Interfaces:** Expose durable transfer state, phases replay/upload/finalize, source pin and checkpoint counters over existing typed harborctl JSON schema

- [ ] **Step 1: Add failing behavior test(s)** — test_progress_replay_zero_remote_write; test_pause_reload_shows_resume; test_legacy_partial_no_resume; test_daily_snapper_copy; test_unknown_eta_not_fabricated. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `cargo test -p harborctl --manifest-path harbor/Cargo.toml; pnpm --dir harbor/desktop test -- --run`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — No false completion/progress; preserve portable AppImage and tray; explicit non-resumable transport/encryption badge; state-based actions and human-readable source choice.
- [ ] **Step 4: Verify GREEN** — Run: `cargo test -p harborctl --manifest-path harbor/Cargo.toml; pnpm --dir harbor/desktop test -- --run`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: progress, status, settings, unified timeline"`.

### Task 11: Balanced / Fast / Custom profiles

**Files:** Create src/btrfs_backup_ng/core/performance_v2.py; Wire through engine control; Test tests/test_performance_v2.py

**Interfaces:** resolve_performance_profile(name:str,overrides:dict|None=None)->PerformanceSettings; apply_resource_policy(worker_pid:int,settings:PerformanceSettings)->None

- [ ] **Step 1: Add failing behavior test(s)** — test_balanced_one_source_and_bounded_threads; test_fast_never_mutates_mounts; test_portable_fallback_without_systemd; test_profile_benchmark_balanced_lower_pressure. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_performance_v2.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Prefer native nice/ionice/systemd transient cgroup; no daemon; benchmark resource pressure on CT700 synthetic fixture; no changes to existing mount options.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_performance_v2.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: balanced / fast / custom profiles"`.

### Task 12: Verification, recovery, legacy compatibility

**Files:** Extend src/btrfs_backup_ng/endpoint/raw.py and raw_metadata.py; Create src/btrfs_backup_ng/core/verify_v2.py; Test tests/test_verify_v2.py

**Interfaces:** verify_checkpoint_index(meta_path:Path,stream_path:Path,full:bool=False)->VerificationResult; classify_legacy_partial(path:Path)->LegacyPartialState

- [ ] **Step 1: Add failing behavior test(s)** — test_old_completed_backups_restore; test_v2_final_standard_zstd_and_btrfs_receive; test_manifest_missing_is_not_resumable; test_full_frame_hash_detects_tamper; test_two_file_publish_crash_is_hidden_and_reconciled. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/test_verify_v2.py tests/test_raw_atomic_write.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Checksum compressed and raw; never promote partial. Provide safe legacy partial keep/delete actions without tampering with finalized restore points.
- [ ] **Step 4: Verify GREEN** — Run: `pytest -q tests/test_verify_v2.py tests/test_raw_atomic_write.py`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: verification, recovery, legacy compatibility"`.

### Task 13: Integration and release gates

**Files:** Test tests/integration/test_checkpoint_v2_real.py and distro smoke CI; Update public docs/packaging only after test gates

**Interfaces:** Integration fixture: small readonly Btrfs source + compatible parent + local/NFS namespace sink; crash injection and restore via btrfs receive

- [ ] **Step 1: Add failing behavior test(s)** — test_real_crash_all_boundaries; test_real_resume_no_remote_rewrite; test_reboot_simulation; test_no_mount_fallback; test_full_incremental_chain_recovery. Assert real observable invariants on fixtures, not mocked implementation details.
- [ ] **Step 2: Verify RED** — Run: `pytest -q tests/integration/test_checkpoint_v2_real.py`. Expect a failure caused by missing v2 behavior.
- [ ] **Step 3: Implement minimum code** — Only test fixtures initially; no Pegasus backup without explicit request. No public private paths/names, UTF-8 scan, provenance and release security checks. Cleanup generated artifacts/worktrees after verification.
- [ ] **Step 4: Verify GREEN** — Run: `Full Python 3.12 suite; Ruff; mypy; Rust tests/clippy; pnpm check/lint/build/test; Arch/Ubuntu/Fedora/openSUSE smoke; packages SHA256/attestation`. Expect all selected tests to pass; run neighbors/previous task tests and remove temporary fixtures.
- [ ] **Step 5: Commit** — Stage only files in this task; `git diff --check` then `git commit -m "feat: integration and release gates"`.

## Verification and Release Discipline

- After every functional subsystem: full python / Rust / frontend regression as appropriate; never call a partial green suite a green release.
- Crash hooks must cover mid-frame, after stream write, after fsync, during manifest temp write, after rename, before directory fsync and during final metadata publish.
- Compare reconstructed send stream byte-for-byte to uninterrupted golden stream and validate manual zstd/Btrfs receive on disposable target.
- Benchmark time, memory, CPU and I/O for checkpoint sizes 64/128/256 MiB and profiles Balanced/Fast; document observed data, not hypothetical numbers.
- Run release privacy, UTF-8, docs, checksums, attestation and distro gates. Do not publish if any fail.
- Clean up test build/temp assets; retain only canonical worktree and evidence. No Pegasus workload until user explicitly authorizes it.
