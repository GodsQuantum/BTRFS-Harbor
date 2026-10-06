//! Safe system-replica planning and Proxmox LXC deployment primitives.

use harbor_core::{ReplicaPlan, ReplicaTargetKind};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TargetFilesystem {
    Btrfs,
    Zfs,
    Ext4,
    Xfs,
    Other,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReplicaRequest {
    pub source_machine: String,
    pub target_name: String,
    pub target_kind: ReplicaTargetKind,
    pub target_filesystem: TargetFilesystem,
    pub regenerate_ssh_host_keys: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct LxcReplicaRequest {
    pub vmid: u32,
    pub confirm_vmid: u32,
    pub staging_path: PathBuf,
    pub target_name: String,
    #[serde(default = "default_true")]
    pub create_rollback_snapshot: bool,
    #[serde(default)]
    pub start_after: bool,
    #[serde(default = "default_true")]
    pub regenerate_ssh_host_keys: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IdMapSegment {
    pub namespace_start: u32,
    pub host_start: u32,
    pub count: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LxcIdMap {
    pub uid: Vec<IdMapSegment>,
    pub gid: Vec<IdMapSegment>,
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ReplicaValidationError {
    #[error("target VMID {0} is outside the Proxmox container range")]
    InvalidVmid(u32),
    #[error("target VMID confirmation does not match {expected}")]
    VmidConfirmation { expected: u32 },
    #[error("replica staging path must be absolute")]
    StagingPathNotAbsolute,
    #[error("target hostname is empty or contains unsupported characters")]
    InvalidTargetName,
    #[error("target container must be stopped, got {0:?}")]
    TargetNotStopped(String),
    #[error("invalid lxc.idmap line {0:?}")]
    InvalidIdMap(String),
}

const LXC_EXCLUSIONS: &[&str] = &[
    "/boot",
    "/efi",
    "/sys",
    "/proc",
    "/dev",
    "/run",
    "/usr/lib/modules",
    "/lib/modules",
];

/// Target-owned identity/network state that must survive a userspace transplant.
///
/// These files are copied aside from the stopped target before its rootfs is
/// replaced, then restored afterward. PVE remains authoritative for the guest's
/// network/identity rather than inheriting the source machine's settings.
pub const LXC_TARGET_PRESERVE_PATHS: &[&str] = &[
    "/etc/hostname",
    "/etc/hosts",
    "/etc/resolv.conf",
    "/etc/fstab",
    "/etc/network",
    "/etc/netplan",
    "/etc/systemd/network",
    "/etc/NetworkManager/system-connections",
];

fn default_true() -> bool {
    true
}

pub fn validate_lxc_request(request: &LxcReplicaRequest) -> Result<(), ReplicaValidationError> {
    if !(100..=999_999_999).contains(&request.vmid) {
        return Err(ReplicaValidationError::InvalidVmid(request.vmid));
    }
    if request.confirm_vmid != request.vmid {
        return Err(ReplicaValidationError::VmidConfirmation {
            expected: request.vmid,
        });
    }
    if !request.staging_path.is_absolute() {
        return Err(ReplicaValidationError::StagingPathNotAbsolute);
    }
    if !valid_hostname(&request.target_name) {
        return Err(ReplicaValidationError::InvalidTargetName);
    }
    Ok(())
}

pub fn validate_stopped_status(status: &str) -> Result<(), ReplicaValidationError> {
    let normalized = status.trim().to_ascii_lowercase();
    if normalized == "status: stopped" || normalized == "stopped" {
        Ok(())
    } else {
        Err(ReplicaValidationError::TargetNotStopped(
            status.trim().to_owned(),
        ))
    }
}

pub fn parse_lxc_idmap(config: &str) -> Result<Option<LxcIdMap>, ReplicaValidationError> {
    let unprivileged = config.lines().any(|line| {
        let line = line.trim();
        line == "unprivileged: 1" || line == "unprivileged: true"
    });
    if !unprivileged {
        return Ok(None);
    }

    let mut uid = Vec::new();
    let mut gid = Vec::new();
    for raw in config.lines() {
        let line = raw.trim();
        let Some(value) = line.strip_prefix("lxc.idmap:") else {
            continue;
        };
        let parts = value.split_whitespace().collect::<Vec<_>>();
        if parts.len() != 4 {
            return Err(ReplicaValidationError::InvalidIdMap(line.to_owned()));
        }
        let kind = parts[0];
        let segment = IdMapSegment {
            namespace_start: parts[1]
                .parse()
                .map_err(|_| ReplicaValidationError::InvalidIdMap(line.to_owned()))?,
            host_start: parts[2]
                .parse()
                .map_err(|_| ReplicaValidationError::InvalidIdMap(line.to_owned()))?,
            count: parts[3]
                .parse()
                .map_err(|_| ReplicaValidationError::InvalidIdMap(line.to_owned()))?,
        };
        match kind {
            "u" => uid.push(segment),
            "g" => gid.push(segment),
            _ => return Err(ReplicaValidationError::InvalidIdMap(line.to_owned())),
        }
    }

    if uid.is_empty() && gid.is_empty() {
        uid.push(IdMapSegment {
            namespace_start: 0,
            host_start: 100_000,
            count: 65_536,
        });
        gid.push(IdMapSegment {
            namespace_start: 0,
            host_start: 100_000,
            count: 65_536,
        });
    } else if uid.is_empty() || gid.is_empty() {
        return Err(ReplicaValidationError::InvalidIdMap(
            "both uid and gid mappings are required".into(),
        ));
    }

    Ok(Some(LxcIdMap { uid, gid }))
}

pub fn usernsexec_map_args(idmap: &LxcIdMap) -> Vec<String> {
    let mut args = Vec::new();
    for segment in &idmap.uid {
        args.extend([
            "-m".to_owned(),
            format!(
                "u:{}:{}:{}",
                segment.namespace_start, segment.host_start, segment.count
            ),
        ]);
    }
    for segment in &idmap.gid {
        args.extend([
            "-m".to_owned(),
            format!(
                "g:{}:{}:{}",
                segment.namespace_start, segment.host_start, segment.count
            ),
        ]);
    }
    args
}

pub fn plan_replica(request: &ReplicaRequest) -> ReplicaPlan {
    let excluded_paths = match request.target_kind {
        ReplicaTargetKind::Lxc => LXC_EXCLUSIONS.iter().map(PathBuf::from).collect(),
        ReplicaTargetKind::BareMetal | ReplicaTargetKind::Vm => {
            vec![
                PathBuf::from("/proc"),
                PathBuf::from("/sys"),
                PathBuf::from("/dev"),
                PathBuf::from("/run"),
            ]
        }
    };

    let requires_btrfs_staging = matches!(request.target_kind, ReplicaTargetKind::Lxc)
        && request.target_filesystem != TargetFilesystem::Btrfs;

    ReplicaPlan {
        id: Uuid::new_v4(),
        target_kind: request.target_kind,
        source_machine: request.source_machine.clone(),
        target_name: request.target_name.clone(),
        excluded_paths,
        reset_machine_id: true,
        regenerate_ssh_host_keys: request.regenerate_ssh_host_keys,
        requires_btrfs_staging,
    }
}

pub fn path_is_excluded(plan: &ReplicaPlan, candidate: &Path) -> bool {
    plan.excluded_paths
        .iter()
        .any(|excluded| candidate == excluded || candidate.starts_with(excluded))
}

pub fn lxc_copy_exclusions(extra_mounts: &[PathBuf]) -> Vec<PathBuf> {
    let mut paths = LXC_EXCLUSIONS.iter().map(PathBuf::from).collect::<Vec<_>>();
    for path in extra_mounts {
        if path.is_absolute() && path != Path::new("/") && !paths.contains(path) {
            paths.push(path.clone());
        }
    }
    paths.sort();
    paths.dedup();
    paths
}

fn valid_hostname(value: &str) -> bool {
    let value = value.trim();
    !value.is_empty()
        && value.len() <= 253
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'.')
        && !value.starts_with('-')
        && !value.ends_with('-')
        && !value.contains("..")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn physical_system_to_zfs_lxc_excludes_hardware_state_and_requires_staging() {
        let plan = plan_replica(&ReplicaRequest {
            source_machine: "SourceHost".into(),
            target_name: "replica-target".into(),
            target_kind: ReplicaTargetKind::Lxc,
            target_filesystem: TargetFilesystem::Zfs,
            regenerate_ssh_host_keys: true,
        });

        assert!(path_is_excluded(&plan, Path::new("/boot")));
        assert!(path_is_excluded(&plan, Path::new("/boot/efi/EFI")));
        assert!(path_is_excluded(&plan, Path::new("/usr/lib/modules/7.2.9")));
        assert!(!path_is_excluded(&plan, Path::new("/home/demo")));
        assert!(plan.requires_btrfs_staging);
        assert!(plan.reset_machine_id);
        assert!(plan.regenerate_ssh_host_keys);
    }

    #[test]
    fn btrfs_lxc_does_not_need_intermediate_btrfs_staging() {
        let plan = plan_replica(&ReplicaRequest {
            source_machine: "SourceHost".into(),
            target_name: "replica-target".into(),
            target_kind: ReplicaTargetKind::Lxc,
            target_filesystem: TargetFilesystem::Btrfs,
            regenerate_ssh_host_keys: false,
        });

        assert!(!plan.requires_btrfs_staging);
    }

    #[test]
    fn vm_keeps_boot_payload_but_excludes_live_virtual_filesystems() {
        let plan = plan_replica(&ReplicaRequest {
            source_machine: "Workstation".into(),
            target_name: "workstation-vm".into(),
            target_kind: ReplicaTargetKind::Vm,
            target_filesystem: TargetFilesystem::Btrfs,
            regenerate_ssh_host_keys: true,
        });

        assert!(!path_is_excluded(&plan, Path::new("/boot")));
        assert!(path_is_excluded(&plan, Path::new("/proc/1")));
    }

    #[test]
    fn lxc_request_requires_exact_vmid_confirmation() {
        let request = LxcReplicaRequest {
            vmid: 420,
            confirm_vmid: 421,
            staging_path: PathBuf::from("/mnt/staging"),
            target_name: "replica-target".into(),
            create_rollback_snapshot: true,
            start_after: false,
            regenerate_ssh_host_keys: true,
        };

        assert_eq!(
            validate_lxc_request(&request),
            Err(ReplicaValidationError::VmidConfirmation { expected: 420 })
        );
    }

    #[test]
    fn lxc_request_rejects_relative_staging_and_unsafe_hostname() {
        let mut request = LxcReplicaRequest {
            vmid: 420,
            confirm_vmid: 420,
            staging_path: PathBuf::from("relative"),
            target_name: "Replica Target".into(),
            create_rollback_snapshot: true,
            start_after: false,
            regenerate_ssh_host_keys: true,
        };
        assert_eq!(
            validate_lxc_request(&request),
            Err(ReplicaValidationError::StagingPathNotAbsolute)
        );

        request.staging_path = PathBuf::from("/mnt/staging");
        assert_eq!(
            validate_lxc_request(&request),
            Err(ReplicaValidationError::InvalidTargetName)
        );
    }

    #[test]
    fn target_must_be_stopped() {
        assert!(validate_stopped_status("status: stopped\n").is_ok());
        assert!(matches!(
            validate_stopped_status("status: running"),
            Err(ReplicaValidationError::TargetNotStopped(_))
        ));
    }

    #[test]
    fn unprivileged_default_map_matches_proxmox_default_namespace() {
        let map = parse_lxc_idmap("unprivileged: 1\nrootfs: local-zfs:subvol-420-disk-0")
            .unwrap()
            .unwrap();
        assert_eq!(
            map.uid,
            vec![IdMapSegment {
                namespace_start: 0,
                host_start: 100_000,
                count: 65_536
            }]
        );
        assert_eq!(map.gid, map.uid);
    }

    #[test]
    fn explicit_idmap_is_parsed_without_using_bind_mount_idmap_options() {
        let config = "unprivileged: 1\n\
                      lxc.idmap: u 0 100000 1000\n\
                      lxc.idmap: u 1000 1000 1\n\
                      lxc.idmap: u 1001 101001 64535\n\
                      lxc.idmap: g 0 100000 1000\n\
                      lxc.idmap: g 1000 1000 1\n\
                      lxc.idmap: g 1001 101001 64535\n\
                      mp0: /srv/data,mp=/srv/data,idmap=u:1000:1000:1;g:1000:1000:1\n";
        let map = parse_lxc_idmap(config).unwrap().unwrap();

        assert_eq!(map.uid.len(), 3);
        assert_eq!(map.uid[1].host_start, 1000);
        assert_eq!(map.gid[1].host_start, 1000);

        let args = usernsexec_map_args(&map);
        assert!(args.contains(&"u:1000:1000:1".to_string()));
        assert!(args.contains(&"g:1000:1000:1".to_string()));
    }

    #[test]
    fn privileged_container_needs_no_user_namespace_transform() {
        assert_eq!(parse_lxc_idmap("unprivileged: 0\n").unwrap(), None);
    }

    #[test]
    fn nested_mounts_are_added_to_copy_exclusions() {
        let paths = lxc_copy_exclusions(&[
            PathBuf::from("/srv/storage"),
            PathBuf::from("/home/demo/data"),
        ]);
        assert!(paths.contains(&PathBuf::from("/boot")));
        assert!(paths.contains(&PathBuf::from("/usr/lib/modules")));
        assert!(paths.contains(&PathBuf::from("/srv/storage")));
        assert!(paths.contains(&PathBuf::from("/home/demo/data")));
    }

    #[test]
    fn target_identity_paths_include_network_and_hostname_state() {
        assert!(LXC_TARGET_PRESERVE_PATHS.contains(&"/etc/hostname"));
        assert!(LXC_TARGET_PRESERVE_PATHS.contains(&"/etc/network"));
        assert!(LXC_TARGET_PRESERVE_PATHS.contains(&"/etc/systemd/network"));
        assert!(LXC_TARGET_PRESERVE_PATHS.contains(&"/etc/NetworkManager/system-connections"));
    }
}
