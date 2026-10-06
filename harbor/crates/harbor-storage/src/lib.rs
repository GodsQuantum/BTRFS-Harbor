//! Configuration, mount guards, engine config rendering and reconstructible state.

use harbor_core::{BackupProfile, BackupSource, DestinationKind, DestinationSpec, RetentionPolicy};
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::path::{Component, Path, PathBuf};
use thiserror::Error;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct HarborConfig {
    #[serde(default)]
    pub destinations: Vec<DestinationSpec>,
    #[serde(default)]
    pub profiles: Vec<BackupProfile>,
}

impl HarborConfig {
    pub fn from_toml(input: &str) -> Result<Self, ConfigError> {
        toml::from_str(input).map_err(ConfigError::Toml)
    }

    pub fn to_toml(&self) -> Result<String, ConfigError> {
        toml::to_string_pretty(self).map_err(ConfigError::Serialize)
    }

    pub fn profile(&self, id: Uuid) -> Option<&BackupProfile> {
        self.profiles.iter().find(|profile| profile.id == id)
    }

    pub fn destinations_by_id(&self) -> HashMap<Uuid, &DestinationSpec> {
        self.destinations.iter().map(|d| (d.id, d)).collect()
    }

    pub fn validate(&self) -> Result<(), ConfigValidationError> {
        let mut destination_ids = HashSet::new();
        for destination in &self.destinations {
            if !destination_ids.insert(destination.id) {
                return Err(ConfigValidationError::DuplicateDestination(destination.id));
            }
            if destination.name.trim().is_empty() {
                return Err(ConfigValidationError::EmptyDestinationName(destination.id));
            }
            if destination.path.as_os_str().is_empty() {
                return Err(ConfigValidationError::EmptyDestinationPath(destination.id));
            }
            if destination.compression.trim().is_empty() {
                return Err(ConfigValidationError::EmptyCompression(destination.id));
            }

            match destination.kind {
                DestinationKind::Nfs | DestinationKind::Smb => {
                    let mount = destination.mount_point.as_ref().ok_or(
                        ConfigValidationError::NetworkDestinationNeedsMount(destination.id),
                    )?;
                    if !mount.is_absolute() {
                        return Err(ConfigValidationError::MountPointMustBeAbsolute(
                            destination.id,
                        ));
                    }
                    if !destination.path.starts_with(mount) {
                        return Err(ConfigValidationError::DestinationOutsideMount(
                            destination.id,
                        ));
                    }
                    if destination
                        .expected_mount_source
                        .as_deref()
                        .is_none_or(|source| source.trim().is_empty())
                    {
                        return Err(ConfigValidationError::NetworkDestinationNeedsSource(
                            destination.id,
                        ));
                    }
                }
                DestinationKind::Local | DestinationKind::Raw => {
                    if !destination.path.is_absolute() {
                        return Err(ConfigValidationError::DestinationMustBeAbsolute(
                            destination.id,
                        ));
                    }
                }
                DestinationKind::Ssh => {}
            }
        }

        let destination_ids = destination_ids;
        let mut profile_ids = HashSet::new();
        for profile in &self.profiles {
            if !profile_ids.insert(profile.id) {
                return Err(ConfigValidationError::DuplicateProfile(profile.id));
            }
            if profile.name.trim().is_empty() {
                return Err(ConfigValidationError::EmptyProfileName(profile.id));
            }
            if profile.sources.is_empty() {
                return Err(ConfigValidationError::ProfileWithoutSources(profile.id));
            }
            if profile.destination_ids.is_empty() {
                return Err(ConfigValidationError::ProfileWithoutDestinations(
                    profile.id,
                ));
            }
            if profile.on_calendar.trim().is_empty() {
                return Err(ConfigValidationError::EmptySchedule(profile.id));
            }
            for destination_id in &profile.destination_ids {
                if !destination_ids.contains(destination_id) {
                    return Err(ConfigValidationError::MissingDestination {
                        profile: profile.id,
                        destination: *destination_id,
                    });
                }
            }

            let mut source_paths = HashSet::new();
            for source in &profile.sources {
                if !source.path.is_absolute() {
                    return Err(ConfigValidationError::SourceMustBeAbsolute {
                        profile: profile.id,
                        path: source.path.clone(),
                    });
                }
                if !source_paths.insert(source.path.clone()) {
                    return Err(ConfigValidationError::DuplicateSource {
                        profile: profile.id,
                        path: source.path.clone(),
                    });
                }
                if source.snapper_config.is_none() && source.snapshot_prefix.trim().is_empty() {
                    return Err(ConfigValidationError::EmptySnapshotPrefix {
                        profile: profile.id,
                        path: source.path.clone(),
                    });
                }
                if !safe_relative_subdir(&source.target_subdir) {
                    return Err(ConfigValidationError::UnsafeTargetSubdir {
                        profile: profile.id,
                        subdir: source.target_subdir.clone(),
                    });
                }
            }
        }

        Ok(())
    }
}

fn safe_relative_subdir(value: &str) -> bool {
    let path = Path::new(value);
    if value.trim().is_empty() || path.is_absolute() {
        return false;
    }
    path.components()
        .all(|part| matches!(part, Component::Normal(_)))
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum ConfigValidationError {
    #[error("duplicate destination id {0}")]
    DuplicateDestination(Uuid),
    #[error("duplicate profile id {0}")]
    DuplicateProfile(Uuid),
    #[error("destination {0} has an empty name")]
    EmptyDestinationName(Uuid),
    #[error("destination {0} has an empty path")]
    EmptyDestinationPath(Uuid),
    #[error("destination {0} has an empty compression setting")]
    EmptyCompression(Uuid),
    #[error("network destination {0} requires an explicit mount point")]
    NetworkDestinationNeedsMount(Uuid),
    #[error("network destination {0} requires an expected mount source")]
    NetworkDestinationNeedsSource(Uuid),
    #[error("mount point for destination {0} must be absolute")]
    MountPointMustBeAbsolute(Uuid),
    #[error("destination {0} is outside its declared mount point")]
    DestinationOutsideMount(Uuid),
    #[error("destination {0} must use an absolute local path")]
    DestinationMustBeAbsolute(Uuid),
    #[error("profile {0} has an empty name")]
    EmptyProfileName(Uuid),
    #[error("profile {0} needs at least one source")]
    ProfileWithoutSources(Uuid),
    #[error("profile {0} needs at least one destination")]
    ProfileWithoutDestinations(Uuid),
    #[error("profile {0} has an empty systemd schedule")]
    EmptySchedule(Uuid),
    #[error("profile {profile} references missing destination {destination}")]
    MissingDestination { profile: Uuid, destination: Uuid },
    #[error("profile {profile} source path {path:?} must be absolute")]
    SourceMustBeAbsolute { profile: Uuid, path: PathBuf },
    #[error("profile {profile} contains duplicate source {path:?}")]
    DuplicateSource { profile: Uuid, path: PathBuf },
    #[error("profile {profile} source {path:?} needs a snapshot prefix")]
    EmptySnapshotPrefix { profile: Uuid, path: PathBuf },
    #[error("profile {profile} has unsafe target subdirectory {subdir:?}")]
    UnsafeTargetSubdir { profile: Uuid, subdir: String },
}

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("invalid Harbor TOML: {0}")]
    Toml(toml::de::Error),
    #[error("cannot serialize Harbor TOML: {0}")]
    Serialize(toml::ser::Error),
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum EngineConfigError {
    #[error("profile {profile} references missing destination {destination}")]
    MissingDestination { profile: Uuid, destination: Uuid },
    #[error("source {0} has an empty target subdirectory")]
    EmptyTargetSubdir(PathBuf),
    #[error("destination {0} has an empty path")]
    EmptyDestinationPath(String),
    #[error("cannot serialize engine configuration: {0}")]
    Serialize(String),
}

#[derive(Debug, Serialize)]
struct EngineConfig {
    global: EngineGlobal,
    volumes: Vec<EngineVolume>,
}

#[derive(Debug, Serialize)]
struct EngineGlobal {
    snapshot_dir: &'static str,
    incremental: bool,
    retention: EngineRetention,
}

#[derive(Debug, Serialize)]
struct EngineRetention {
    hourly: u32,
    daily: u32,
    weekly: u32,
    monthly: u32,
    yearly: u32,
}

impl From<&RetentionPolicy> for EngineRetention {
    fn from(value: &RetentionPolicy) -> Self {
        Self {
            hourly: value.hourly,
            daily: value.daily,
            weekly: value.weekly,
            monthly: value.monthly,
            yearly: value.yearly,
        }
    }
}

#[derive(Debug, Serialize)]
struct EngineVolume {
    path: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    snapshot_prefix: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    source: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    snapper: Option<EngineSnapper>,
    targets: Vec<EngineTarget>,
}

#[derive(Debug, Serialize)]
struct EngineSnapper {
    config_name: String,
    include_types: Vec<&'static str>,
    min_age: &'static str,
}

#[derive(Debug, Serialize)]
struct EngineTarget {
    path: String,
    compress: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    require_mount: Option<String>,
    #[serde(skip_serializing_if = "is_false")]
    optional: bool,
}

fn is_false(value: &bool) -> bool {
    !*value
}

/// Render a native btrfs-backup-ng configuration for one Harbor profile.
///
/// Harbor deliberately does not translate the engine into a second backup
/// format. The inherited engine remains the source of truth for snapshot,
/// transfer, retention and incremental-parent correctness.
pub fn render_engine_config(
    profile: &BackupProfile,
    destinations: &[DestinationSpec],
) -> Result<String, EngineConfigError> {
    let by_id = destinations
        .iter()
        .map(|destination| (destination.id, destination))
        .collect::<HashMap<_, _>>();

    let selected = profile
        .destination_ids
        .iter()
        .map(|id| {
            by_id
                .get(id)
                .copied()
                .ok_or(EngineConfigError::MissingDestination {
                    profile: profile.id,
                    destination: *id,
                })
        })
        .collect::<Result<Vec<_>, _>>()?;

    let volumes = profile
        .sources
        .iter()
        .map(|source| render_engine_volume(source, &selected))
        .collect::<Result<Vec<_>, _>>()?;

    let config = EngineConfig {
        global: EngineGlobal {
            snapshot_dir: ".snapshots",
            incremental: true,
            retention: EngineRetention::from(&profile.retention),
        },
        volumes,
    };

    toml::to_string_pretty(&config).map_err(|err| EngineConfigError::Serialize(err.to_string()))
}

fn render_engine_volume(
    source: &BackupSource,
    destinations: &[&DestinationSpec],
) -> Result<EngineVolume, EngineConfigError> {
    if source.target_subdir.trim().is_empty() {
        return Err(EngineConfigError::EmptyTargetSubdir(source.path.clone()));
    }

    let (source_kind, snapper, snapshot_prefix) = if let Some(config_name) = &source.snapper_config
    {
        (
            Some("snapper"),
            Some(EngineSnapper {
                config_name: config_name.clone(),
                include_types: vec!["single", "pre", "post"],
                min_age: "1h",
            }),
            None,
        )
    } else {
        (None, None, Some(source.snapshot_prefix.clone()))
    };

    let targets = destinations
        .iter()
        .map(|destination| render_engine_target(source, destination))
        .collect::<Result<Vec<_>, _>>()?;

    Ok(EngineVolume {
        path: source.path.to_string_lossy().into_owned(),
        snapshot_prefix,
        source: source_kind,
        snapper,
        targets,
    })
}

fn render_engine_target(
    source: &BackupSource,
    destination: &DestinationSpec,
) -> Result<EngineTarget, EngineConfigError> {
    let target_path = engine_target_uri(source, destination)?;

    Ok(EngineTarget {
        path: target_path,
        compress: destination.compression.clone(),
        require_mount: destination
            .mount_point
            .as_ref()
            .map(|path| path.to_string_lossy().into_owned()),
        optional: destination.optional,
    })
}

/// Return the exact raw/raw+ssh target URI used by the inherited engine.
pub fn engine_target_uri(
    source: &BackupSource,
    destination: &DestinationSpec,
) -> Result<String, EngineConfigError> {
    let base = destination.path.to_string_lossy();
    if base.trim().is_empty() {
        return Err(EngineConfigError::EmptyDestinationPath(
            destination.name.clone(),
        ));
    }
    if source.target_subdir.trim().is_empty() {
        return Err(EngineConfigError::EmptyTargetSubdir(source.path.clone()));
    }

    Ok(match destination.kind {
        DestinationKind::Ssh => {
            let trimmed = base.trim_end_matches('/');
            if trimmed.starts_with("raw+ssh://") {
                format!("{trimmed}/{}", source.target_subdir)
            } else if let Some(rest) = trimmed.strip_prefix("ssh://") {
                format!("raw+ssh://{rest}/{}", source.target_subdir)
            } else {
                format!("raw+ssh://{trimmed}/{}", source.target_subdir)
            }
        }
        DestinationKind::Local
        | DestinationKind::Raw
        | DestinationKind::Nfs
        | DestinationKind::Smb => {
            let path = destination.path.join(&source.target_subdir);
            format!("raw://{}", path.to_string_lossy())
        }
    })
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct MountEntry {
    pub mount_point: PathBuf,
    pub source: String,
    pub fs_type: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct MountTable {
    pub entries: Vec<MountEntry>,
}

impl MountTable {
    pub fn from_mountinfo(input: &str) -> Self {
        let entries = input
            .lines()
            .filter_map(parse_mountinfo_line)
            .collect::<Vec<_>>();
        Self { entries }
    }

    pub fn find_exact(&self, path: &Path) -> Option<&MountEntry> {
        self.entries.iter().find(|entry| entry.mount_point == path)
    }

    pub fn find_for_path(&self, path: &Path) -> Option<&MountEntry> {
        self.entries
            .iter()
            .filter(|entry| path == entry.mount_point || path.starts_with(&entry.mount_point))
            .max_by_key(|entry| entry.mount_point.components().count())
    }
}

#[derive(Debug, Error, PartialEq, Eq)]
pub enum MountGuardError {
    #[error("network destination {0} is not an active mountpoint")]
    NotMounted(PathBuf),
    #[error("destination {path} has filesystem {found}, expected {expected}")]
    WrongFilesystem {
        path: PathBuf,
        found: String,
        expected: &'static str,
    },
    #[error("destination {path} is mounted from {found}, expected {expected}")]
    WrongSource {
        path: PathBuf,
        found: String,
        expected: String,
    },
}

/// Refuse the dangerous case where an absent NAS mount leaves a normal local
/// directory at the configured path and the backup engine would fill it.
pub fn validate_destination_mount(
    destination: &DestinationSpec,
    mounts: &MountTable,
) -> Result<(), MountGuardError> {
    let requires_mount = matches!(
        destination.kind,
        DestinationKind::Nfs | DestinationKind::Smb
    ) || destination.mount_point.is_some()
        || destination.expected_mount_source.is_some();

    if !requires_mount {
        return Ok(());
    }

    let mount_point = destination
        .mount_point
        .as_deref()
        .unwrap_or(destination.path.as_path());

    let entry = mounts
        .find_exact(mount_point)
        .ok_or_else(|| MountGuardError::NotMounted(mount_point.to_path_buf()))?;

    match destination.kind {
        DestinationKind::Nfs if !matches!(entry.fs_type.as_str(), "nfs" | "nfs4") => {
            return Err(MountGuardError::WrongFilesystem {
                path: mount_point.to_path_buf(),
                found: entry.fs_type.clone(),
                expected: "nfs/nfs4",
            });
        }
        DestinationKind::Smb if entry.fs_type != "cifs" => {
            return Err(MountGuardError::WrongFilesystem {
                path: mount_point.to_path_buf(),
                found: entry.fs_type.clone(),
                expected: "cifs",
            });
        }
        _ => {}
    }

    if let Some(expected) = &destination.expected_mount_source
        && &entry.source != expected
    {
        return Err(MountGuardError::WrongSource {
            path: mount_point.to_path_buf(),
            found: entry.source.clone(),
            expected: expected.clone(),
        });
    }

    Ok(())
}

fn parse_mountinfo_line(line: &str) -> Option<MountEntry> {
    let (left, right) = line.split_once(" - ")?;
    let left_fields = left.split_whitespace().collect::<Vec<_>>();
    let right_fields = right.split_whitespace().collect::<Vec<_>>();
    if left_fields.len() < 5 || right_fields.len() < 2 {
        return None;
    }

    Some(MountEntry {
        mount_point: PathBuf::from(unescape_mount_field(left_fields[4])),
        fs_type: right_fields[0].to_owned(),
        source: unescape_mount_field(right_fields[1]),
    })
}

fn unescape_mount_field(value: &str) -> String {
    value
        .replace(r"\040", " ")
        .replace(r"\011", "\t")
        .replace(r"\012", "\n")
        .replace(r"\134", r"\")
}

#[cfg(test)]
mod tests {
    use super::*;
    use harbor_core::{BackupSource, DestinationKind, RetentionPolicy};
    use uuid::Uuid;

    fn destination(kind: DestinationKind, path: &str, source: Option<&str>) -> DestinationSpec {
        DestinationSpec {
            id: Uuid::nil(),
            name: "Backup NAS".into(),
            kind,
            path: PathBuf::from(path),
            mount_point: match kind {
                DestinationKind::Nfs | DestinationKind::Smb => Some(PathBuf::from(path)),
                _ => None,
            },
            expected_mount_source: source.map(str::to_owned),
            compression: "zstd".into(),
            optional: false,
        }
    }

    #[test]
    fn rejects_offline_nfs_target_even_if_directory_exists_conceptually() {
        let mounts = MountTable::default();
        let dest = destination(
            DestinationKind::Nfs,
            "/mnt/backup-nas",
            Some("192.0.2.10:/backup-systems"),
        );

        assert_eq!(
            validate_destination_mount(&dest, &mounts),
            Err(MountGuardError::NotMounted(PathBuf::from(
                "/mnt/backup-nas"
            )))
        );
    }

    #[test]
    fn validates_nfs_mount_identity() {
        let mounts = MountTable::from_mountinfo(
            "39 28 0:55 / /mnt/backup-nas rw - nfs4 192.0.2.10:/backup-systems rw",
        );
        let dest = destination(
            DestinationKind::Nfs,
            "/mnt/backup-nas",
            Some("192.0.2.10:/backup-systems"),
        );

        assert_eq!(validate_destination_mount(&dest, &mounts), Ok(()));
    }

    #[test]
    fn refuses_wrong_server_or_share() {
        let mounts = MountTable::from_mountinfo(
            "39 28 0:55 / /mnt/backup-nas rw - nfs4 192.0.2.99:/other rw",
        );
        let dest = destination(
            DestinationKind::Nfs,
            "/mnt/backup-nas",
            Some("192.0.2.10:/backup-systems"),
        );

        assert!(matches!(
            validate_destination_mount(&dest, &mounts),
            Err(MountGuardError::WrongSource { .. })
        ));
    }

    #[test]
    fn finds_most_specific_mount_for_selected_path() {
        let mounts = MountTable::from_mountinfo(
            "24 1 0:1 / / rw - btrfs /dev/mapper/root rw
             39 24 0:55 / /mnt/nas rw - nfs4 192.0.2.20:/ rw
             40 39 0:56 / /mnt/nas/shared rw - nfs4 192.0.2.20:/shared rw",
        );

        let entry = mounts
            .find_for_path(Path::new("/mnt/nas/shared/Téléchargements/Sauvegardes"))
            .expect("network mount");

        assert_eq!(entry.mount_point, PathBuf::from("/mnt/nas/shared"));
        assert_eq!(entry.source, "192.0.2.20:/shared");
        assert_eq!(entry.fs_type, "nfs4");
    }

    #[test]
    fn parses_utf8_and_escaped_spaces_without_ascii_aliases() {
        let mounts = MountTable::from_mountinfo(
            r"39 28 0:55 / /mnt/nas/Téléchargements\040NAS rw - cifs //nas/share rw",
        );
        let dest = destination(
            DestinationKind::Smb,
            "/mnt/nas/Téléchargements NAS",
            Some("//nas/share"),
        );

        assert_eq!(validate_destination_mount(&dest, &mounts), Ok(()));
    }

    #[test]
    fn config_roundtrip_preserves_unicode_paths() {
        let config = HarborConfig {
            destinations: vec![destination(
                DestinationKind::Raw,
                "/mnt/Sauvegardes/Vidéos",
                None,
            )],
            profiles: vec![BackupProfile {
                id: Uuid::nil(),
                name: "Workstation recovery".into(),
                sources: vec![],
                destination_ids: vec![Uuid::nil()],
                on_calendar: "*-*-* 02:00:00".into(),
                retention: RetentionPolicy {
                    hourly: 0,
                    daily: 7,
                    weekly: 4,
                    monthly: 3,
                    yearly: 0,
                },
                verify_after_backup: true,
            }],
        };

        let encoded = config.to_toml().unwrap();
        let decoded = HarborConfig::from_toml(&encoded).unwrap();
        assert_eq!(decoded, config);
        assert!(encoded.contains("Vidéos"));
    }

    fn valid_harbor_config() -> HarborConfig {
        let destination_id = Uuid::new_v4();
        HarborConfig {
            destinations: vec![DestinationSpec {
                id: destination_id,
                name: "Backup NAS".into(),
                kind: DestinationKind::Nfs,
                path: PathBuf::from("/mnt/backup-nas/harbor/workstation"),
                mount_point: Some(PathBuf::from("/mnt/backup-nas")),
                expected_mount_source: Some("192.0.2.10:/backup-systems".into()),
                compression: "zstd".into(),
                optional: false,
            }],
            profiles: vec![BackupProfile {
                id: Uuid::new_v4(),
                name: "Workstation recovery".into(),
                sources: vec![BackupSource {
                    path: PathBuf::from("/"),
                    snapshot_prefix: "root-".into(),
                    snapper_config: Some("root".into()),
                    target_subdir: "rootfs".into(),
                }],
                destination_ids: vec![destination_id],
                on_calendar: "*-*-* 02:00:00".into(),
                retention: RetentionPolicy {
                    hourly: 0,
                    daily: 7,
                    weekly: 4,
                    monthly: 3,
                    yearly: 0,
                },
                verify_after_backup: true,
            }],
        }
    }

    #[test]
    fn valid_configuration_passes_validation() {
        assert!(valid_harbor_config().validate().is_ok());
    }

    #[test]
    fn validation_rejects_missing_destination_reference() {
        let mut config = valid_harbor_config();
        config.profiles[0].destination_ids = vec![Uuid::new_v4()];

        let err = config.validate().unwrap_err().to_string();
        assert!(err.contains("missing destination"), "{err}");
    }

    #[test]
    fn validation_rejects_duplicate_destination_ids() {
        let mut config = valid_harbor_config();
        config.destinations.push(config.destinations[0].clone());

        let err = config.validate().unwrap_err().to_string();
        assert!(err.contains("duplicate destination"), "{err}");
    }

    #[test]
    fn validation_rejects_profiles_without_sources() {
        let mut config = valid_harbor_config();
        config.profiles[0].sources.clear();

        let err = config.validate().unwrap_err().to_string();
        assert!(err.contains("at least one source"), "{err}");
    }

    #[test]
    fn renders_native_engine_config_with_snapper_and_mount_guard() {
        let destination_id = Uuid::new_v4();
        let destination = DestinationSpec {
            id: destination_id,
            name: "Backup NAS".into(),
            kind: DestinationKind::Nfs,
            path: PathBuf::from("/mnt/backup-nas/harbor/workstation"),
            mount_point: Some(PathBuf::from("/mnt/backup-nas")),
            expected_mount_source: Some("192.0.2.10:/backup-systems".into()),
            compression: "zstd".into(),
            optional: false,
        };
        let profile = BackupProfile {
            id: Uuid::new_v4(),
            name: "Workstation recovery".into(),
            sources: vec![
                BackupSource {
                    path: PathBuf::from("/"),
                    snapshot_prefix: "root-".into(),
                    snapper_config: Some("root".into()),
                    target_subdir: "rootfs".into(),
                },
                BackupSource {
                    path: PathBuf::from("/home"),
                    snapshot_prefix: "home-".into(),
                    snapper_config: None,
                    target_subdir: "home".into(),
                },
            ],
            destination_ids: vec![destination_id],
            on_calendar: "*-*-* 02:00:00".into(),
            retention: RetentionPolicy {
                hourly: 0,
                daily: 7,
                weekly: 4,
                monthly: 3,
                yearly: 0,
            },
            verify_after_backup: true,
        };

        let rendered = render_engine_config(&profile, &[destination]).unwrap();

        assert!(rendered.contains("incremental = true"));
        assert!(rendered.contains("source = \"snapper\""));
        assert!(rendered.contains("config_name = \"root\""));
        assert!(rendered.contains("path = \"raw:///mnt/backup-nas/harbor/workstation/rootfs\""));
        assert!(rendered.contains("require_mount = \"/mnt/backup-nas\""));
        assert!(rendered.contains("compress = \"zstd\""));
        assert!(rendered.contains("daily = 7"));
    }
}
