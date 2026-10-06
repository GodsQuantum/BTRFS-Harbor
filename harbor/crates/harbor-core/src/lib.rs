//! Domain types shared by the Btrfs Harbor control plane.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;
use uuid::Uuid;

pub const ENGINE_STATUS_SCHEMA_VERSION: u32 = 1;

pub fn profile_unit_stem(id: Uuid) -> String {
    format!("btrfs-harbor-profile-{id}")
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EngineStatusReport {
    pub schema_version: u32,
    pub config: Option<PathBuf>,
    pub healthy: bool,
    #[serde(default)]
    pub parallelism: Option<Parallelism>,
    #[serde(default)]
    pub volumes: Vec<VolumeStatus>,
    #[serde(default)]
    pub error: Option<EngineReportedError>,
    #[serde(default)]
    pub transactions: Option<serde_json::Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct Parallelism {
    pub volumes: u32,
    pub targets: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EngineReportedError {
    pub code: String,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct VolumeStatus {
    pub path: PathBuf,
    pub source: SourceStatus,
    #[serde(default)]
    pub targets: Vec<TargetStatus>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct SourceStatus {
    pub status: String,
    pub snapshot_count: u64,
    pub latest_snapshot: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TargetStatus {
    pub path: String,
    pub status: String,
    pub backup_count: u64,
    #[serde(default)]
    pub pending: u64,
}

impl EngineStatusReport {
    /// Harbor only calls a machine protected when at least one configured target
    /// has an off-host backup and the engine considers the report healthy.
    pub fn protection_state(&self) -> ProtectionState {
        let any_target = self
            .volumes
            .iter()
            .flat_map(|volume| &volume.targets)
            .next()
            .is_some();
        let every_volume_has_backup = !self.volumes.is_empty()
            && self.volumes.iter().all(|volume| {
                !volume.targets.is_empty()
                    && volume.targets.iter().any(|target| {
                        target.backup_count > 0 && target.status == "ok" && target.pending == 0
                    })
            });

        if self.healthy && any_target && every_volume_has_backup {
            ProtectionState::Protected
        } else if self.volumes.is_empty() || !any_target {
            ProtectionState::Unprotected
        } else {
            ProtectionState::AtRisk
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProtectionState {
    Protected,
    AtRisk,
    Unprotected,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum DestinationKind {
    Local,
    Raw,
    Nfs,
    Smb,
    Ssh,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DestinationSpec {
    pub id: Uuid,
    pub name: String,
    pub kind: DestinationKind,
    pub path: PathBuf,
    #[serde(default)]
    pub mount_point: Option<PathBuf>,
    #[serde(default)]
    pub expected_mount_source: Option<String>,
    #[serde(default = "default_compression")]
    pub compression: String,
    #[serde(default)]
    pub optional: bool,
}

fn default_compression() -> String {
    "zstd".to_string()
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BackupSource {
    pub path: PathBuf,
    pub snapshot_prefix: String,
    #[serde(default)]
    pub snapper_config: Option<String>,
    pub target_subdir: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RetentionPolicy {
    pub hourly: u32,
    pub daily: u32,
    pub weekly: u32,
    pub monthly: u32,
    pub yearly: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct BackupProfile {
    pub id: Uuid,
    pub name: String,
    pub sources: Vec<BackupSource>,
    pub destination_ids: Vec<Uuid>,
    pub on_calendar: String,
    pub retention: RetentionPolicy,
    pub verify_after_backup: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryScope {
    File,
    Directory,
    Subvolume,
    System,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecoveryPlan {
    pub id: Uuid,
    pub scope: RecoveryScope,
    pub restore_point: String,
    pub sources: Vec<PathBuf>,
    pub staging_path: PathBuf,
    pub requires_rescue_environment: bool,
    pub steps: Vec<String>,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ReplicaTargetKind {
    BareMetal,
    Vm,
    Lxc,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ReplicaPlan {
    pub id: Uuid,
    pub target_kind: ReplicaTargetKind,
    pub source_machine: String,
    pub target_name: String,
    pub excluded_paths: Vec<PathBuf>,
    pub reset_machine_id: bool,
    pub regenerate_ssh_host_keys: bool,
    pub requires_btrfs_staging: bool,
}

#[cfg(test)]
mod tests {
    use super::*;

    fn report(healthy: bool, target_count: u64, pending: u64) -> EngineStatusReport {
        EngineStatusReport {
            schema_version: ENGINE_STATUS_SCHEMA_VERSION,
            config: Some(PathBuf::from("/etc/btrfs-harbor/engine.toml")),
            healthy,
            parallelism: Some(Parallelism {
                volumes: 1,
                targets: 1,
            }),
            volumes: vec![VolumeStatus {
                path: PathBuf::from("/"),
                source: SourceStatus {
                    status: "ok".into(),
                    snapshot_count: 2,
                    latest_snapshot: Some("root-002".into()),
                },
                targets: vec![TargetStatus {
                    path: "raw:///mnt/backup/workstation/root".into(),
                    status: if target_count > 0 { "ok" } else { "no backups" }.into(),
                    backup_count: target_count,
                    pending,
                }],
            }],
            error: None,
            transactions: None,
        }
    }

    #[test]
    fn profile_unit_stem_is_uuid_scoped() {
        let id = Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap();
        assert_eq!(
            profile_unit_stem(id),
            "btrfs-harbor-profile-11111111-1111-4111-8111-111111111111"
        );
    }

    #[test]
    fn healthy_synced_target_is_protected() {
        assert_eq!(
            report(true, 2, 0).protection_state(),
            ProtectionState::Protected
        );
    }

    #[test]
    fn missing_backup_is_not_protected() {
        assert_eq!(
            report(false, 0, 0).protection_state(),
            ProtectionState::AtRisk
        );
    }

    #[test]
    fn pending_backup_is_at_risk() {
        assert_eq!(
            report(true, 1, 1).protection_state(),
            ProtectionState::AtRisk
        );
    }

    #[test]
    fn status_roundtrips_through_json() {
        let original = report(true, 2, 0);
        let encoded = serde_json::to_string(&original).unwrap();
        let decoded: EngineStatusReport = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded, original);
    }
}
