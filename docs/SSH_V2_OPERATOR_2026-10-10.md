# BTRFS Harbor SSH-v2 restricted receiver — experimental preview after rc.11

Status: implementation in `preview/checkpoint-resume-v026`, NOT in the already
published `v0.2.6-rc.11` binaries or the stable `main` branch. This adapter
does not replace Harbor's existing Btrfs checkpoint-v2 sender.

## Trust boundary and architecture

A complete, already VERIFIED local/NFS machine-set is mirrored over actual
OpenSSH to a dedicated unprivileged server account. The server executes a
fixed Harbor receiver only. Each 8 MiB-or-smaller block is SHA-256 checked,
fsynced, then journaled atomically. An interrupted SSH connection is retried
by querying the server's committed byte offset. Local Btrfs
send/replay/checkpoint remains the SINGLE stream-generating engine.

A disconnected SSH server **does not** cause fallback writes to a local
directory masquerading as the remote target. A destination mount fingerprint
is recorded at explicit receiver initialization and checked on every request.
A changed server, remote host key, target root, machine file name, total length
or SHA-256 is rejected. The receiver cannot invoke shell snippets or accept
arbitrary remote paths. Do NOT use `btrbk ssh_filter_btrbk.sh` versions before
0.32.7 (CVE-2026-62943). Exact forced command `harbor-v2` is required.

## Setup (admin/operator, not automatic)

1. On the RECEIVER, create a dedicated non-root `harborrecv` Unix account,
   a preexisting account-owned backup directory, and install the matching
   Harbor engine. Explicitly initialize that directory:

   `btrfs-backup-ng raw checkpoint-v2 ssh-receiver-init --root /EXISTING/BACKUP --allow-local`

   Remove `--allow-local` on NFS/SMB network mounts; it will reject other
   filesystem types unless the operator explicitly opts in. Initialize only
   while the intended NAS is actually mounted. Never create that directory
   implicitly when a NAS disappears.

2. Restrict a dedicated SSH public key with `restrict` and this FIXED
   authorized_keys `command="..."` (substitute the **absolute, audited**
   installed paths from the receiver, never user input):

   `command="/usr/bin/python3 /usr/lib/btrfs-harbor/btrfs-backup-ng.pyz raw checkpoint-v2 ssh-receiver --root /EXISTING/BACKUP",restrict ssh-ed25519 AAAA... harbor-backup`

   No sudo or shell filter; normal SSH access through this key is forbidden.
   Keep the receiver directory private, with a different Unix account
   from any valuable data unrelated to Harbor.

3. On the SENDER, make a normal completed, verified checkpoint-v2 machine
   backup to an existing local/NFS staging destination. Then mirror the set
   using the pinned SSH host key in an existing `known_hosts` file:

   `btrfs-backup-ng raw checkpoint-v2 ssh-mirror --target /BACKUP/STAGING --set-id UUID --host backup.example.org --user harborrecv --identity-file /ABSOLUTE/PRIVATE_KEY --known-hosts /ABSOLUTE/KNOWN_HOSTS --experimental --allow-local`

   The client sends verified ancestor streams FIRST, then metadata, boot
   archives and finally the machine-set catalog. The remote receiver does
   not need Snapper, source mounts or the sender's original profile.
   If interrupted, repeat the SAME command; SHA-256 and full-file identity
   are checked at completion before atomic publication.

   **Important:** This is intentionally a two-stage safe mirror for now;
   it needs enough local staging space for the completed set. The primary
   one-click desktop control does not yet create a pure SSH backup by itself.

## Safety & acceptance

- Real SSH server acceptance: `.github/workflows/ssh-v2-acceptance.yml` —
  GitHub-hosted isolated server, dedicated forced key, strict known_hosts,
  truncated chunk, killed sshd daemon, restart, resumed offset, SHA-256.
- Unit acceptance: `tests/test_ssh_checkpoint_v2_receiver.py` —
  symlink/path guard, wrong chunk SHA, uncommitted tail truncation, crash
  between hardlink publish and partial deletion, invalid SSH original command.
- Server key restrictions must stay exact; never interpolate arbitrary remote
  shell commands. Make the receiver a dedicated account, not root.
- Archive retention is still **read-only**; independently retain one known-good
  backup until complete recovery on a second machine is demonstrated.
