# Btrfs Harbor v0.2.6 — Checkpointed Resume, Persistent Pause and Snapper Orchestration

**Date:** 2026-10-08
**Status:** Written specification approved 2026-10-08. Implementation plan authorized.
**Base:** `e6be1e0cd9a567d4cd6faa53036805862b24edf4` (BTRFS Harbor v0.2.5)
**Scope:** resumable raw backup transport, persistent pause/resume, Snapper/Btrfs Assistant snapshot sourcing and cadence separation, source pinning, target identity guards, timeline/recovery integration, workstation-friendly performance controls.

---

## 1. Product intent

A large workstation backup must not lose hours of remote transfer because the GUI closed, the machine rebooted, the network disappeared, or the user needed the computer back.

Btrfs Harbor v0.2.6 must therefore provide two distinct guarantees:

1. **Persistent pause/resume:** a user can pause a long backup, close Harbor or reboot, and resume later from the last committed checkpoint.
2. **Crash/reboot resume without remote retransmission:** if a resumable raw backup was 90% committed on the destination, Harbor must preserve that 90% and send only the missing tail over the destination transport after replaying the immutable source stream locally.

The same release must let Snapper/Btrfs Assistant remain the local snapshot manager while Harbor independently decides **which local snapshot to send** and **when to send it**.

Harbor remains an orchestrator around native Btrfs/Snapper primitives rather than creating another local snapshot ecosystem.

---

## 2. Research state as of 2026-10-08

### 2.1 Btrfs send/receive

Latest upstream btrfs-progs release checked during design: **v7.1**.

Native `btrfs send` still provides full/incremental streams and protocol 1/2 behavior, but no resume token, byte offset, checkpoint token, or equivalent mechanism that restarts generation at an arbitrary point in an interrupted send stream.

`btrfs receive` likewise consumes a linear send stream and does not expose an interrupted-stream resume offset.

Relevant upstream documentation:

- https://btrfs.readthedocs.io/en/latest/btrfs-send.html
- https://btrfs.readthedocs.io/en/latest/btrfs-receive.html
- https://github.com/kdave/btrfs-progs/releases/tag/v7.1

Therefore Harbor cannot honestly promise kernel-level resume of the Btrfs send generator. Resume after process loss must preserve already committed destination bytes and replay the same immutable source stream locally until it reaches the first missing checkpoint.

### 2.2 Existing btrfs-backup-ng chunked transfer work

Latest upstream checked: **btrfs-backup-ng v0.9.12**, published 2026-09-27. No commits were found on upstream master after that release at design time.

The inherited source already contains an experimental chunked transfer subsystem with:

- default 64 MiB chunks;
- SHA-256 chunk checksums;
- manifests;
- `transfers pause`;
- `transfers resume`;
- persisted transfer states.

However, its current resume path first materializes/spools the complete Btrfs send stream into local chunks before the transfer. That is unsuitable as Harbor's default for very large workstation subvolumes because it can require hundreds of GiB of local temporary space.

The current upstream also documents a known source-pin concurrency/interruption issue in v0.9.12, with a lock rework planned for 0.10.0. Harbor must not build persistent resumability on the v0.9.12 pin implementation without fixing/replacing that part.

Reference:

- https://github.com/berrym/btrfs-backup-ng/releases/tag/v0.9.12

### 2.3 Snapper

Latest upstream checked: **Snapper v0.13.2**, published 2026-09-07.

Snapper remains the canonical local snapshot manager where configured. Current Snapper supports modifying snapshot metadata, including the cleanup algorithm. Official Snapper documentation explicitly documents clearing a snapshot's cleanup algorithm to keep it out of automatic cleanup.

Relevant references:

- https://github.com/openSUSE/snapper/releases/tag/v0.13.2
- https://manpages.opensuse.org/Tumbleweed/snapper/snapper.8.en.html
- https://manpages.opensuse.org/Tumbleweed/snapper/snapper-configs.5.en.html

This gives Harbor a native mechanism to keep a source snapshot available for an interrupted transfer: temporarily clear its automatic cleanup assignment, remember the original value, and restore it when the resumable transfer is completed or explicitly discarded.

This prevents automatic Snapper cleanup; it cannot prevent a deliberate manual delete. Resume must therefore always revalidate source UUID/path before replay.

### 2.4 Zstandard framing

Zstandard supports multiple concatenated frames. A normal zstd decompressor processes concatenated frames as one logical byte stream. The seekable format builds on independently compressed frames plus an index.

Harbor can therefore make each checkpoint an independent zstd frame while still publishing one ordinary final `.btrfs.zst` stream that can be manually decoded with standard zstd tools.

Reference:

- https://github.com/facebook/zstd/blob/dev/contrib/seekable_format/zstd_seekable_compression_format.md

v0.2.6 does **not** require implementation of the complete zstd seekable-format footer. Harbor's own checkpoint index is authoritative. The final data file remains a valid concatenation of zstd frames.

### 2.5 Linux mount identity

On current Linux, `statx` exposes mount IDs, including `STATX_MNT_ID_UNIQUE` on sufficiently recent kernels. This is stronger than testing only whether a destination path exists.

Reference:

- https://man7.org/linux/man-pages/man2/statx.2.html

Harbor will use stable mount identity where available, and fall back to `/proc/self/mountinfo` identity when not.

### 2.6 Limine

Limine integration remains metadata/discovery only. Limine is not the owner of Btrfs snapshots; local snapshot creation/deletion stays with Btrfs/Snapper. Harbor may mark a local snapshot as bootable/discoverable by Limine when that can be correlated by Btrfs UUID.

No backup-format dependency on Limine is introduced.

---

## 3. Non-negotiable safety rules

1. A partial resumable backup is never listed as a valid restore point.
2. A checkpoint is not considered committed until destination data is durable enough for the endpoint and its manifest commit has completed.
3. A crash may lose only the **current uncommitted checkpoint**, never a previously committed checkpoint.
4. Resume never appends to a partial stream whose source snapshot identity, parent identity, destination identity, send mode, or checkpoint history cannot be validated.
5. Resume never silently rewrites already committed remote checkpoints.
6. If replayed raw checkpoint bytes differ from the committed raw checkpoint hash, Harbor stops resume immediately and offers a clean restart; it never concatenates mismatched send streams.
7. The source snapshot remains protected from automatic Snapper cleanup while resumable state exists.
8. A missing expected NFS/SMB mount must never cause writes into the underlying local directory.
9. Closing or crashing the GUI must not abort the backup worker.
10. Live root is never overwritten by desktop recovery.
11. Legacy completed backups remain recoverable.
12. Legacy `.part` files without a v2 resume manifest are **not** claimed to be resumable.
13. Harbor does not automatically enable Btrfs qgroups merely to calculate ETA.
14. No new mandatory daemon is introduced for manual backups.
15. Persistent automation continues to use native systemd units where systemd exists.

---

## 4. Terminology

**Checkpoint**
A fixed-size segment of the raw Btrfs send byte stream. Each committed checkpoint corresponds to one independent compressed zstd frame in the destination `.part` file.

**Committed prefix**
The ordered sequence of checkpoints recorded in the resume manifest and durably present at the beginning of the destination partial stream.

**Replay / fast-forward**
After process loss, Harbor restarts `btrfs send` from byte zero for the same immutable source snapshot, reconstructs the checkpoint boundaries, hashes each raw checkpoint, and discards checkpoints that match the committed prefix instead of transmitting them again.

**Persistent pause**
A user-requested state in which Harbor finishes the current checkpoint, commits it, terminates the active send worker, and retains resume state and source pin. No backup worker consumes CPU/I/O while paused.

**Discard**
Explicit destruction of partial resume state. It removes the partial stream and resume manifest, then releases the source pin.

---

## 5. Harbor Checkpointed Raw v2 format

### 5.1 Compatibility goal

A completed v2 raw backup must still look like an ordinary raw Harbor backup:

- one final stream file;
- one authoritative `.meta` sidecar;
- standard zstd decompression works;
- decompressed bytes are one valid Btrfs send stream.

No directory containing thousands of chunk files is used.

### 5.2 Temporary files during transfer

For an existing final stream name conceptually:

`<snapshot>.btrfs.zst`

the incomplete v2 transfer owns:

`<snapshot>.btrfs.zst.<transfer-id>.part`

and a resume manifest stored outside ordinary backup discovery, under Harbor/btrfs-backup-ng bookkeeping for that target.

The exact bookkeeping path will follow the endpoint's existing hidden-state conventions rather than inventing a user-facing backup directory.

The final stream name is created only by atomic publish after complete validation.

### 5.3 Default checkpoint size

Initial product default:

**128 MiB raw send-stream bytes per checkpoint.**

Rationale:

- bounded pause latency;
- bounded transient memory/spool size;
- a 300 GiB raw stream produces only a few thousand manifest entries;
- substantially fewer compression-process boundaries than 64 MiB;
- simple fixed boundaries make replay deterministic to verify.

The implementation plan must include a local microbenchmark comparing 64, 128 and 256 MiB on the reference development fixtures before freezing the constant. The user-facing default remains 128 MiB unless evidence shows a meaningful regression.

Advanced configuration may later expose checkpoint size, but v0.2.6 does not need to expose it in ordinary UI.

### 5.4 Checkpoint record

Every committed checkpoint records at least:

- checkpoint sequence number;
- raw byte start offset;
- raw byte length;
- compressed byte start offset;
- compressed byte length;
- SHA-256 of raw checkpoint bytes;
- SHA-256 of compressed frame bytes;
- commit timestamp.

The final short checkpoint is permitted.

### 5.5 Transfer identity

The resume manifest records immutable transfer identity:

- schema version;
- transfer UUID;
- profile/job UUID;
- source volume identity;
- source snapshot Btrfs UUID;
- source snapshot path at start;
- Snapper config and number when applicable;
- Snapper type/pre-number metadata when applicable;
- incremental parent Btrfs UUID, or null;
- Btrfs send protocol/options fingerprint;
- compression algorithm and level;
- encryption mode;
- checkpoint size;
- destination endpoint type;
- destination identity fingerprint;
- initial kernel release;
- initial btrfs-progs version;
- initial Harbor/btrfs-backup-ng version;
- state;
- checkpoint records;
- total committed raw/compressed bytes.

Kernel/tool versions are evidence and diagnostics, not sufficient by themselves to decide compatibility. Raw checkpoint hashes are authoritative for replay compatibility.

### 5.6 Manifest integrity

The manifest is canonical JSON.

It stores a digest over the ordered checkpoint index, computed from canonical checkpoint records. This protects the index itself from accidental mutation.

The manifest update sequence is:

1. validate target identity;
2. finish one independent zstd frame;
3. flush and fsync the stream file as supported by the endpoint;
4. record the frame/checkpoint hashes and lengths;
5. atomically write a new manifest temp file;
6. fsync the manifest;
7. rename manifest temp to active manifest;
8. fsync the containing directory when supported.

A checkpoint is committed only after step 7 succeeds.

### 5.7 Crash repair rule

At open/resume:

- if the `.part` file is longer than the last committed compressed end offset, truncate it back to that offset;
- if it is shorter than the manifest's committed end offset, mark resume state damaged and refuse append;
- if a committed frame hash does not match when explicitly verified, refuse resume;
- if the manifest is missing or invalid, do not infer resumability from a `.part` filename alone.

This converts arbitrary process loss into at most one discarded checkpoint.

---

## 6. Streaming implementation model

### 6.1 No full local spool

The complete Btrfs send stream is never materialized locally by default.

Harbor reads the send pipe incrementally and holds at most one raw checkpoint plus bounded pipeline buffering.

A single checkpoint may use RAM or a bounded temporary file according to implementation benchmarks, but the product must never require local free space proportional to the whole snapshot.

### 6.2 Compression frames

Each raw checkpoint is compressed as a complete independent zstd frame and appended to the destination partial stream.

Concatenating the decompressed frames must reproduce the exact original Btrfs send byte stream.

### 6.3 Why fixed byte checkpoints, not Btrfs-command parsing

v0.2.6 deliberately does **not** parse the send protocol merely to choose checkpoint boundaries.

Reasons:

- frame boundaries are invisible after decompression;
- Btrfs receive consumes the reconstructed continuous byte stream;
- fixed byte boundaries are simpler to replay and test;
- existing Btrfs command CRC remains intact inside the reconstructed stream;
- command-aware splitting would add parser/protocol coupling without improving restore semantics.

If future Btrfs-progs exposes native stream checkpoint/resume primitives, Harbor should prefer them over this replay layer.

---

## 7. Resume after crash/reboot

### 7.1 Preconditions

A resume attempt requires all of:

- valid resume manifest;
- destination partial stream present;
- expected target identity present;
- source snapshot exists and is readonly;
- source Btrfs UUID matches;
- configured parent snapshot still exists when incremental;
- parent UUID matches;
- send protocol/options are reproducible;
- compression/encryption mode is supported for v2 resume.

### 7.2 Replay algorithm

Harbor starts the same Btrfs send operation from the beginning.

For checkpoint N:

1. read exactly the configured raw checkpoint boundary from the new send stream;
2. compute raw SHA-256;
3. compare to committed checkpoint N in the resume manifest;
4. if equal:
   - update replay progress;
   - discard the raw bytes;
   - perform **no destination write** and no recompression of that checkpoint;
5. if different:
   - abort resume as incompatible;
   - leave committed remote prefix untouched;
   - offer clean restart or keep partial for manual diagnosis.

After every committed checkpoint has been matched, Harbor switches from **replay** to **upload** and starts producing new zstd frames from the first missing raw checkpoint.

### 7.3 Environment changes

A kernel, btrfs-progs or Harbor version change does not automatically destroy resume state.

Harbor may attempt replay, but must label that the environment changed. Matching raw checkpoint hashes prove whether the regenerated prefix is compatible.

Any mismatch is a hard stop.

### 7.4 Replay visibility

The UI must distinguish local replay from remote transfer.

Example:

**Replaying source locally — 72% of previous progress validated**
`0 B/s to destination`

then:

**Uploading — 188.2 GiB already secured, continuing from checkpoint 1471**

No UI may imply that the already secured prefix is being retransmitted.

---

## 8. Pause, stop and cancel semantics

### 8.1 Pause

User-facing **Pause** means persistent pause.

When requested:

1. set `pause_requested`;
2. finish the current checkpoint;
3. commit stream + manifest;
4. stop the Btrfs send process group gracefully;
5. set manifest state to `paused`;
6. keep the source pin;
7. terminate the worker.

The GUI may then exit. Resume survives logout/reboot.

UI copy must explain:

**Pausing after the current checkpoint. Already secured data will be kept.**

### 8.2 Stop now

An emergency **Stop now**:

- terminates the worker immediately;
- discards/truncates only the uncommitted tail;
- keeps all previously committed checkpoints;
- leaves state resumable unless the user explicitly chooses discard.

### 8.3 Discard partial backup

A separate destructive action:

**Discard partial backup**

requires confirmation and:

- removes resumable partial stream;
- removes resume manifest;
- removes transfer transaction state;
- releases/restores the source pin;
- never deletes a previously finalized restore point.

### 8.4 Tray behavior

During active transfer, tray menu adds:

- Show Harbor
- Pause backup
- Stop now

During pause no transfer process is active, therefore Harbor does not need to remain running solely to preserve resume state.

---

## 9. Snapper / Btrfs Assistant source model

### 9.1 Btrfs Assistant

Btrfs Assistant is treated as a UI around the underlying Btrfs/Snapper state.

Harbor does not create a special Btrfs Assistant adapter when the actual source is already discoverable through Snapper/Btrfs.

### 9.2 Source policy per job/volume

Each source can choose:

**A. Create snapshot for this backup**
Existing Harbor/native source behavior.

**B. Latest stable Snapper snapshot**
Harbor reuses the newest eligible local Snapper snapshot.

**C. Selected Snapper snapshot**
User explicitly chooses one existing snapshot.

The first-run UI may recommend B when a healthy Snapper config already manages the source.

### 9.3 Stable Snapper selection

For automatic “latest stable” selection:

- a `single` snapshot is eligible after its configured settle age;
- a `post` snapshot is eligible when its matching `pre` exists;
- a lone `pre` snapshot is not chosen automatically;
- a snapshot already represented on the destination by source UUID is skipped;
- manual selection may deliberately choose a pre snapshot, with explicit wording.

Default settle age for automatic non-paired snapshots will be decided by implementation tests; it must not introduce the existing one-hour catch-up problem as a hidden global requirement.

### 9.4 Canonical identity

Snapper number is presentation metadata only.

Canonical identity uses:

- Btrfs snapshot UUID;
- parent/received UUID metadata where relevant;
- Snapper config;
- snapshot type/pre-number metadata.

This remains correct even if snapshot numbers differ or old Snapper versions reuse numbers.

---

## 10. Independent snapshot cadence and send cadence

Snapshot creation and remote backup scheduling are independent settings.

### 10.1 Snapshot source cadence

Harbor may consume whatever local Snapper cadence already exists. It does not need to create another local timeline.

### 10.2 Remote send cadence

Supported job policies:

- Manual
- Hourly
- Daily
- Weekly
- Custom calendar
- After every eligible new Snapper snapshot

### 10.3 Daily latest mode

This is a first-class use case.

If many Snapper snapshots are created during software updates during the day, a daily Harbor job may select only the latest stable snapshot not yet represented remotely.

It does **not** send every intermediate local snapshot merely because they exist.

Where possible, it sends that latest snapshot incrementally from a compatible snapshot already present remotely.

### 10.4 After-snapshot mode

Existing Harbor systemd.path-style Snapper trigger behavior remains valid.

The trigger must apply the same eligibility rules and must not race a pre/post pair by sending a lone `pre` automatically.

### 10.5 Pending Resume versus newer Snapper snapshots

A transfer already in resumable state is bound to its original immutable snapshot UUID and parent fingerprint. Resume always targets this originally pinned source, even if Snapper has created newer snapshots. Daily Latest and after-snapshot schedulers cannot silently replace a paused or failed-resumable transfer with a newer snapshot in the same job. If they conflict, Harbor offers an explicit choice to finish the previous transfer or discard it separately before a new backup. Pending partial state is never silently deleted or retargeted.

---

## 11. Snapper source pinning

### 11.1 Problem

A resumable transfer becomes useless if automatic Snapper cleanup deletes its source snapshot before resume.

The inherited v0.9.12 source pin implementation has a known concurrency/interruption weakness and is not sufficient for this feature unchanged.

### 11.2 Harbor pin behavior

For a Snapper-managed source snapshot, Harbor records:

- original cleanup algorithm;
- original relevant user-data needed for restoration;
- config name;
- snapshot number;
- snapshot UUID;
- transfer ID.

When the resumable transfer begins, Harbor uses the native Snapper operation equivalent to:

`snapper modify --cleanup-algorithm "" <snapshot>`

to remove that snapshot from automatic cleanup eligibility.

On successful completion or explicit discard, Harbor restores the original cleanup algorithm if the same source snapshot still exists and still has the expected UUID.

### 11.3 Crash reconciliation

Pin state is persistent.

On Harbor startup/status scan:

- resumable transfer exists + source UUID exists + cleanup cleared => healthy pin;
- resumable transfer exists + source exists but cleanup restored externally => warn and offer re-pin;
- resume state exists + source was manually deleted => resume impossible, keep remote partial until user chooses discard;
- no transfer exists + stale Harbor pin record => restore original cleanup algorithm after validating identity.

### 11.4 Scope of protection

This protects against Snapper's automatic cleanup.

Harbor cannot and must not claim to prevent deliberate root/manual deletion of the source snapshot.

---

## 12. Limine integration

Harbor Timeline may annotate a local snapshot as:

**Bootable locally via Limine**

when Limine tooling is installed and Harbor can correlate the local snapshot by Btrfs UUID.

Limine status never determines backup validity.

Deleting, creating or restoring local snapshots remains Btrfs/Snapper work.

Recovery may later regenerate Limine entries using the target system's native tooling, consistent with the v0.2.5 machine-aware recovery model.

---

## 13. Destination identity and mount disappearance guard

### 13.1 Raw filesystem targets

At transfer start Harbor records destination identity including, where available:

- mount unique ID;
- mount ID;
- filesystem type;
- backing source/export;
- device major/minor where meaningful;
- normalized destination root.

### 13.2 Checkpoint guard

Before appending and before committing every checkpoint, Harbor revalidates destination identity.

For an expected NFS/SMB destination:

- mount vanished;
- filesystem type changed;
- export/source changed;
- mount unique identity changed unexpectedly;

=> Harbor stops before the next write and leaves the previous committed checkpoint resumable.

It never writes into the now-exposed local directory.

### 13.3 Kernel compatibility

Prefer `STATX_MNT_ID_UNIQUE` where supported.

Fallback uses `/proc/self/mountinfo` plus filesystem/source/device identity.

A missing modern statx field is not a reason to reject an otherwise supported Linux distro.

---

## 14. Destination support tiers for resume v2

### 14.1 Required in v0.2.6

**raw filesystem destination**

This includes:

- local filesystem;
- mounted NFS;
- mounted SMB/CIFS;
- other mounted filesystems that pass Harbor's ordinary destination guards.

This is the priority path because it matches the large-workstation use case.

### 14.2 Existing non-resumable transports remain supported

Existing direct native Btrfs receive and other endpoints continue to work exactly as before.

If a target does not implement checkpoint append/truncate semantics, UI says:

**Resume after reboot is not available for this destination type.**

It must not silently fall back while showing resumable UI.

### 14.3 Future/optional staging adapter

Architecture should allow a future “resumable staging” adapter:

checkpointed raw file first, then local `btrfs receive` after completion.

This can make a native Btrfs destination network-transfer resumable at the cost of temporary destination space.

It is not required to block v0.2.6 if raw filesystem resume is complete and correct.

### 14.4 raw+ssh / SSH

The existing experimental chunk/SSH code is reusable research, but full SSH append/truncate/manifest support is not required for the first public resumable format unless implementation proves it can meet the same crash semantics without weakening security.

The manifest and sink interfaces must be designed so SSH can be added without a format migration.

---

## 15. Encryption compatibility

Whole-stream encryption modes that cannot independently authenticate/decrypt checkpoint frames are **not resumable in v0.2.6**.

Harbor must display this before starting such a job.

Existing encrypted backups remain fully supported for backup/recovery under their current non-resumable behavior.

A future encrypted resumable format requires a separately reviewed framed AEAD design. v0.2.6 must not invent ad-hoc cryptography.

---

## 16. Performance profiles

Large workstation backups must not default to maximum resource pressure.

### 16.1 Profiles

**Balanced — recommended for desktop/workstation**
- conservative process priority;
- low I/O priority where supported;
- one large source transfer at a time by default;
- bounded compression threads;
- resume/checkpoint enabled where compatible.

**Fast**
- higher concurrency;
- ordinary priority;
- intended for idle/server use.

**Custom**
- source concurrency;
- compression level/threads within supported limits;
- rate limit;
- checkpoint behavior where advanced controls are exposed.

### 16.2 Native resource controls

On systemd/cgroup v2 systems Harbor may use a transient scope for the worker with CPU/I/O weights.

This must not require permanent custom services.

Portable/manual operation must still work using normal process priorities/rate limiting when systemd scopes are unavailable.

### 16.3 No hidden architectural mount changes

Performance controls never alter storage mounts, mount options or established filesystem topology.

---

## 17. Progress and ETA UX

### 17.1 Three phases

A resumed transfer presents:

1. **Replay / fast-forward**
2. **Upload**
3. **Finalize / verify**

### 17.2 What is always exact

When measurable:

- committed raw bytes;
- committed compressed bytes;
- destination write rate;
- elapsed time;
- number of committed checkpoints;
- replayed checkpoints;
- current source.

### 17.3 Percentage/ETA

Exact-looking percentage/ETA remains forbidden when total final stream size is not defensible.

Harbor may show:

- exact percentage when a trustworthy total exists;
- **estimated range** when an estimate exists;
- historical/lower-bound forecast when resume state gives a useful bound;
- otherwise throughput + elapsed + secured bytes.

The UI must prefer:

**ETA unavailable — 188 GiB secured at 31 MiB/s**

over a fabricated percentage.

### 17.4 Optional size sources

Harbor may use without mutation:

- existing qgroups, if already enabled;
- engine estimate data;
- historical completed transfer statistics;
- resumable manifest lower bounds.

Harbor does not enable qgroups automatically.

---

## 18. Timeline / inventory UX

Timeline becomes a unified protection inventory.

A snapshot row can carry independent badges:

- Local
- Snapper
- Bootable
- Remote
- Verified
- Sending
- Paused
- Resume available
- Failed
- Needs source

Examples:

**2026-10-08 18:00 — Snapper post — Local · Remote · Verified**

**2026-10-09 08:00 — Snapper single — Local · Paused 73% · Resume available**

**2026-10-09 12:00 — Snapper single — Local only**

User actions depend on state:

- Send now
- Resume
- Pause
- Verify
- Recover
- Discard partial
- Show technical details

---

## 19. Recovery compatibility

### 19.1 Final v2 raw streams

Completed v2 backups restore through the existing raw recovery path.

Standard manual interoperability remains:

`zstd -dc <stream>.btrfs.zst | btrfs receive <destination>`

for non-encrypted streams.

### 19.2 Partial streams

A partial v2 stream is never a restore point.

Harbor may inspect/verify its committed checkpoints but Recovery does not offer it as a completed snapshot.

### 19.3 Final metadata

The final `.meta` sidecar extends existing metadata with:

- resumable format version;
- checkpoint size;
- committed checkpoint index or its canonical integrity record;
- source raw stream SHA-256 when available;
- checkpoint-index digest;
- transfer lineage.

Existing fields remain readable by legacy code where possible.

### 19.4 Verification

v2 verify supports:

**Quick structural verify**
- manifest/meta parse;
- lengths/offsets contiguous;
- checkpoint index digest;
- file length consistency.

**Full checkpoint verify**
- read compressed frames;
- compare recorded compressed hashes;
- optionally decompress and compare raw checkpoint hashes.

**Stream semantic verify**
- decompressed full stream can be passed through Btrfs stream dump/validation tooling when available.

A full at-rest read is explicit and may be scheduled when the machine/link is idle rather than forcing an immediate second full NFS read before publish.

---

## 20. Legacy compatibility and migration

### 20.1 Existing completed streams

All v0.2.5 and older finalized `.btrfs[.zst]` streams and `.meta` sidecars remain discoverable and recoverable.

No rewrite/migration is required.

### 20.2 Existing partial files

A legacy `.part` with no checkpoint manifest cannot be resumed safely.

Harbor may show:

**Legacy incomplete transfer — resume metadata unavailable**

Actions:

- Keep for diagnosis
- Delete partial

It must never offer fake Resume.

### 20.3 No in-place conversion of arbitrary partial streams

Harbor does not infer checkpoint hashes by slicing an old compressed stream and claiming that it corresponds to deterministic raw checkpoints.

The current old partial format therefore cannot be upgraded into v2 resume state without restarting that source.

---

## 21. Worker/UI separation

The backup worker's correctness is independent of GUI lifetime.

The UI consumes progress/state through a channel, but a closed/broken channel is never a reason for the worker to abort.

Resume state is persisted by the engine/worker, not held only in Svelte/Tauri memory.

A background active transfer may continue under the existing tray lifecycle.

A paused transfer needs no live Harbor process.

---

## 22. Core state machine

Required durable transfer states:

- `preparing`
- `uploading`
- `pause_requested`
- `paused`
- `replaying`
- `finalizing`
- `verifying`
- `completed`
- `failed_resumable`
- `failed_unrecoverable`
- `discarded`

Allowed important transitions:

`uploading -> pause_requested -> paused`

`uploading -> failed_resumable`

`paused -> replaying -> uploading`

`failed_resumable -> replaying -> uploading`

`uploading -> finalizing -> verifying -> completed`

`paused|failed_resumable -> discarded`

A process crash does not write a state transition; next startup reconciles manifest/file reality into `paused`, `failed_resumable` or `failed_unrecoverable`.

---

## 23. Crash consistency matrix

### Crash before any checkpoint commit

Expected:
- partial may contain garbage/tail;
- manifest contains zero checkpoints;
- restart truncates to zero and resumes from start.

### Crash while writing checkpoint N

Expected:
- manifest still ends at N-1;
- restart truncates stream to compressed end of N-1;
- N is regenerated.

### Crash after stream fsync but before manifest rename

Expected:
- extra frame exists;
- manifest still ends at N-1;
- restart truncates extra frame and regenerates N.

### Crash after manifest commit

Expected:
- checkpoint N survives and is replay-skipped.

### Destination disappears between checkpoints

Expected:
- guard fails before next append;
- no underlying-local write;
- state becomes resumable failure.

### Source snapshot deleted while paused

Expected:
- resume refused;
- remote committed prefix retained;
- UI says source snapshot no longer exists;
- user may discard partial.

### Replay hash mismatch

Expected:
- append forbidden;
- no committed destination bytes changed;
- clean restart offered.

---

## 24. Config model changes

Configuration must represent independently:

### Snapshot source policy

`create_new | latest_snapper | selected_snapper`

For selected Snapper:
- config name;
- selected snapshot UUID/number as appropriate.

### Send cadence

Existing schedule model extended/presented as:
- manual;
- hourly;
- daily;
- weekly;
- calendar;
- after_snapshot.

### Resume policy

`auto_resume | ask | disabled`

Default for compatible raw filesystem destinations:

**ask** for an interrupted manual job, while scheduled automation may use **auto_resume** after safety validation.

### Performance profile

`balanced | fast | custom`

Old configs default to a backward-compatible explicit value during deserialization; UI may recommend Balanced without silently changing existing jobs.

---

## 25. Engine / endpoint boundaries

### 25.1 Reuse before invention

The inherited `chunked_transfer.py` concepts should be reused where appropriate:

- transfer IDs;
- checkpoint/chunk checksums;
- manifest serialization concepts;
- pause/resume command semantics.

The implementation must not blindly route Harbor through the existing full-spool `--use-chunked` path.

### 25.2 New resumable sink abstraction

A focused transport abstraction is required for destinations that can support:

- open/create partial;
- append frame;
- durable flush;
- get length;
- truncate to committed offset;
- atomic manifest write;
- atomic final publish;
- identity revalidation.

The raw local/mounted-filesystem endpoint implements this first.

Backup generation/checkpointing does not contain NFS-specific code.

### 25.3 No duplicate raw endpoint

v2 extends the existing raw endpoint contract rather than creating a second “Harbor raw” backup type that would fragment discovery/recovery.

---

## 26. Security rules

1. Resume manifests are semi-trusted target metadata; all paths/names are validated before filesystem use.
2. Never follow symlinks for partial/manifest writes where existing raw endpoint safety already forbids them.
3. A manifest cannot redirect Harbor outside the configured destination.
4. Transfer IDs are generated locally and not accepted as arbitrary paths.
5. Checkpoint lengths/offsets are range-checked and monotonic.
6. No shell command is constructed from unchecked manifest strings.
7. Mount identity mismatch is fail-closed.
8. Resume source is matched by Btrfs UUID, not only pathname.
9. Snapper pin restore modifies metadata only after confirming the original snapshot UUID.
10. Existing root/polkit boundary remains; frontend cannot directly mutate privileged backup state.
11. Encryption limitations are explicit; no weakened crypto mode is introduced merely to gain resume.

---

## 27. Test strategy

### 27.1 Deterministic replay tests

Synthetic readonly Btrfs fixture:

- two sends of same snapshot, same parent/options => checkpoints replay-match;
- modify environment/options fingerprint and produce changed stream => mismatch is detected before append;
- incremental send parent identity mismatch => resume refused.

The test suite must not assume global Btrfs send determinism beyond what checkpoint hash comparison actually proves.

### 27.2 Crash injection

Inject crashes at every ordering point:

- mid-frame;
- after frame write;
- after stream fsync;
- during manifest temp write;
- after manifest rename;
- before directory fsync;
- during final publish.

Every case must prove that at most one uncommitted checkpoint is lost and finalized backups are never damaged.

### 27.3 Destination disappearance

Use mount-identity fixtures and, where CI allows, a temporary mount namespace to prove an expected network mount cannot silently fall through to its underlying directory.

### 27.4 Pause/reboot simulation

- start transfer;
- commit several checkpoints;
- request pause;
- confirm worker exited;
- recreate process from manifest;
- replay prefix;
- assert zero writes while replaying committed checkpoints;
- continue upload;
- final decompressed stream equals uninterrupted reference stream byte-for-byte.

### 27.5 Snapper pin tests

- preserve original cleanup algorithm;
- clear cleanup while resumable;
- restore on success;
- preserve pin on pause/crash;
- restore on discard;
- UUID mismatch prevents metadata mutation;
- missing manually deleted snapshot reports resume impossible.

### 27.6 Snapper selection tests

- latest single;
- completed pre/post selects post;
- lone pre skipped automatically;
- daily run with several snapshots sends only latest eligible;
- already-remote snapshot is no-op;
- incremental parent chosen only when valid.

### 27.7 Backward compatibility

- old raw finalized streams list/verify/restore;
- old metadata still parses;
- old partial does not advertise Resume;
- v2 final stream manually decompresses as one valid send stream.

### 27.8 UI tests

- Pause means checkpoint-safe persistent pause;
- Replay and Upload are visually distinct;
- secured bytes remain visible after restart;
- resume unsupported transport is explicit;
- ETA unknown remains honest;
- daily Snapper send is understandable without exposing UUIDs by default.

---

## 28. Acceptance criteria

v0.2.6 is complete only when all of the following are proven:

1. A raw mounted-filesystem backup interrupted after multiple checkpoints resumes without rewriting committed remote bytes.
2. Reboot/process-loss simulation preserves all committed checkpoints.
3. Replay compares every committed raw checkpoint hash before skipping it.
4. Any replay mismatch stops before append.
5. No full-snapshot local spool is required.
6. Completed v2 output is one standard concatenated-zstd Btrfs send stream plus metadata.
7. `zstd -dc final.btrfs.zst | btrfs receive ...` compatibility is preserved for non-encrypted streams.
8. Pause leaves no active send/compression worker and survives Harbor exit/reboot.
9. Stop-now loses at most the current uncommitted checkpoint.
10. Legacy completed backups remain restorable.
11. Legacy partials are never falsely called resumable.
12. NFS/SMB disappearance cannot write into the underlying local mountpoint.
13. Snapper source is protected from automatic cleanup while resumable state exists.
14. Cleanup assignment is restored safely on completion/discard.
15. “Latest stable Snapper snapshot” correctly handles pre/post pairs.
16. Daily mode can send only the latest eligible snapshot rather than every local snapshot.
17. After-snapshot mode remains available independently of daily/weekly modes.
18. Btrfs Assistant-created Snapper snapshots are discovered through normal Snapper discovery.
19. Timeline shows local/remote/verified/paused/resume state coherently.
20. Balanced mode demonstrably lowers workstation pressure relative to Fast in benchmark evidence.
21. GUI crash/disconnect cannot terminate the worker through its progress channel.
22. No public docs/tests contain private workstation names or paths.
23. Full Python/Rust/frontend regression suites pass.
24. Arch/Debian/Fedora/openSUSE smoke matrix remains green.
25. Release artifacts pass checksums/attestation/package inspection.

---

## 29. Explicit non-goals for v0.2.6

- Kernel-level Btrfs send resume at an arbitrary offset; Linux does not expose it.
- Claiming zero local replay work after a reboot without a complete local spool.
- Requiring local temporary capacity equal to the backup size.
- Parsing Btrfs send commands solely to align checkpoints.
- Automatically enabling Btrfs qgroups.
- Replacing Snapper as the local snapshot manager.
- Preventing a root user from manually deleting a pinned Snapper snapshot.
- New ad-hoc cryptographic framing.
- Mandatory SSH resumable transport if it cannot meet the same crash guarantees.
- Changing user mounts or mount options.
- A permanent Harbor daemon for manual backup pause/resume.

---

## 30. Implementation sequencing constraints

Implementation must proceed test-first.

Recommended subsystem order after this spec is approved:

1. v2 manifest/checkpoint state and crash-ordering primitives;
2. resumable raw sink for local/mounted filesystems;
3. streaming checkpoint compressor and final stream compatibility;
4. replay/fast-forward engine;
5. persistent pause/stop/discard lifecycle;
6. Snapper source selection + automatic-cleanup pinning;
7. send cadence separation and daily-latest behavior;
8. target mount identity checkpoint guard;
9. progress/timeline UI;
10. performance profiles;
11. recovery/verification v2 support;
12. full regression/package/release gates.

No live workstation backup is required during early implementation. Synthetic/small Btrfs fixtures must prove correctness first.

A real large backup is a final integration gate only after all crash/replay tests are green.

---

## 31. Current real-world partial artifact

A legacy v0.2.5-era partial raw stream retained from the interrupted reference workstation run is useful as diagnostic evidence, but it has no v2 checkpoint manifest.

It must **not** be treated as resumable by v0.2.6.

After v0.2.6 resume behavior is proven, it may be removed through the ordinary legacy-partial cleanup flow before the next real full backup.

---

## 32. Source references used for this design

- Btrfs send documentation: https://btrfs.readthedocs.io/en/latest/btrfs-send.html
- Btrfs receive documentation: https://btrfs.readthedocs.io/en/latest/btrfs-receive.html
- btrfs-progs releases: https://github.com/kdave/btrfs-progs/releases
- btrfs-backup-ng v0.9.12: https://github.com/berrym/btrfs-backup-ng/releases/tag/v0.9.12
- Snapper v0.13.2: https://github.com/openSUSE/snapper/releases/tag/v0.13.2
- Snapper CLI: https://manpages.opensuse.org/Tumbleweed/snapper/snapper.8.en.html
- Snapper config/cleanup: https://manpages.opensuse.org/Tumbleweed/snapper/snapper-configs.5.en.html
- Zstd seekable format: https://github.com/facebook/zstd/blob/dev/contrib/seekable_format/zstd_seekable_compression_format.md
- Linux statx mount identity: https://man7.org/linux/man-pages/man2/statx.2.html
- Linux cgroup v2: https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html
