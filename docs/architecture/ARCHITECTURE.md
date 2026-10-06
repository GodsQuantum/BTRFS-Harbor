# Btrfs Harbor Architecture

## Principles

1. Preserve btrfs-backup-ng as the data plane for v1.
2. Keep the desktop unprivileged.
3. Privileged actions are typed operations, never arbitrary shell execution.
4. systemd owns background scheduling.
5. Backup truth remains reconstructible from streams, metadata and manifests.
6. A local snapshot is never equivalent to an off-host backup.
7. Restore is staged and verified before activation.
8. LXC replication adapts userspace rather than cloning physical boot state.

## Components

### Inherited engine

The Python btrfs-backup-ng source tree remains at the repository root. Harbor's initial upstream-facing extension is a stable JSON status format.

### harbor-core

Shared domain types:
- engine status;
- backup health;
- destinations;
- backup profiles;
- recovery plans;
- replica plans.

No translated UI strings belong here.

### harbor-engine

Process adapter for btrfs-backup-ng. It parses versioned JSON and preserves degraded health reports even when the engine intentionally exits non-zero.

### harbor-storage

Human-readable TOML configuration, destination modeling and mount-source validation. The mount guard exists to prevent writing into an underlying local directory when an expected network mount is absent.

### harbor-agent

System D-Bus service. Read-only foundation methods expose version, system summary and backup status. Mutating methods remain unavailable until the polkit authorization path and executable profile model are complete.

### harbor-recovery

Recovery planning and future rescue CLI. Whole-system recovery requires a rescue/fresh-install context and always stages before activation.

### harbor-replica

Target-aware replication planning. LXC excludes physical boot/runtime pseudo-filesystems and requires temporary Btrfs staging when the destination rootfs is not Btrfs.

### Desktop

Tauri 2 + SvelteKit 3. The frontend talks only to explicit Tauri commands, which in turn use D-Bus. The browser fixture is visibly labeled Demo and cannot be mistaken for live protection state.

## IPC

Desktop process:

    backup_status(config?)
    agent_version()
    system_summary()

D-Bus service:

    io.github.GodsQuantum.BtrfsHarbor1
    /io/github/GodsQuantum/BtrfsHarbor1

No method accepts a shell command.

## Filesystem layout

Configuration:

    /etc/btrfs-harbor/harbor.toml
    /etc/btrfs-harbor/profiles.d/
    /etc/btrfs-harbor/destinations.d/
    /etc/btrfs-harbor/credentials/

Runtime state:

    /var/lib/btrfs-harbor/state.sqlite3
    /var/log/btrfs-harbor/events.jsonl

Backup streams and their metadata remain the durable source of truth; SQLite is a rebuildable index.

## Scheduling

A profile is ultimately represented by a systemd timer and oneshot service. Persistent timers cover missed laptop runs. Managed NFS/SMB destinations should use explicit mount/automount units and must pass source identity validation before data-plane execution.

## Recovery state machine

    Planned
      ↓
    ChainVerified
      ↓
    Staged
      ↓
    StageVerified
      ↓
    Prepared
      ↓
    AwaitingActivation
      ↓
    Activated

Failure before activation leaves the live system unchanged.

## Security boundary

The UI is a normal user process. The system agent is privileged because Btrfs snapshots, receives, mounts and recovery may require root. The bridge is deliberately narrow.

The polkit policy defines actions for future mutation paths, but foundation mutating methods are not considered enabled until caller authorization is wired and tested.
