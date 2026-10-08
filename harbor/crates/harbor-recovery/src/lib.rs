//! Recovery planning that never mutates the live system implicitly.

use harbor_core::{RecoveryPlan, RecoveryScope};
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use uuid::Uuid;

pub const RECOVERY_KIT_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecoveryKitManifest {
    pub schema_version: u32,
    pub created_at: String,
    pub profile_id: Uuid,
    pub profile_name: String,
    pub hostname: String,
    pub distro_id: Option<String>,
    pub distro_name: Option<String>,
    pub kernel_release: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct RecoveryKitInputs {
    pub harbor_config_toml: String,
    pub os_release: String,
    pub fstab: String,
    pub crypttab: String,
    pub findmnt_json: String,
    pub lsblk_json: String,
    pub btrfs_subvolumes: String,
    pub snapper_configs_json: String,
    pub packages_explicit: String,
    pub packages_foreign: String,
    pub boot_status: String,
    pub kernel_cmdline: String,
    pub initramfs_config: String,
    pub bootloader_config: String,
}

pub fn recovery_kit_relative_dir(profile_id: Uuid) -> PathBuf {
    PathBuf::from("_recovery-kit").join(profile_id.to_string())
}

pub fn recovery_kit_documents(
    manifest: &RecoveryKitManifest,
    inputs: &RecoveryKitInputs,
) -> Result<BTreeMap<String, String>, serde_json::Error> {
    let mut documents = BTreeMap::new();
    documents.insert(
        "manifest.json".to_string(),
        format!("{}\n", serde_json::to_string_pretty(manifest)?),
    );
    documents.insert(
        "README-RESTORE.md".to_string(),
        render_recovery_readme(manifest),
    );
    documents.insert(
        "harbor-profile.toml".to_string(),
        inputs.harbor_config_toml.clone(),
    );

    for (name, content) in [
        ("os-release", &inputs.os_release),
        ("fstab", &inputs.fstab),
        ("crypttab", &inputs.crypttab),
        ("findmnt.json", &inputs.findmnt_json),
        ("lsblk.json", &inputs.lsblk_json),
        ("btrfs-subvolumes.txt", &inputs.btrfs_subvolumes),
        ("snapper-configs.json", &inputs.snapper_configs_json),
        ("packages-explicit.txt", &inputs.packages_explicit),
        ("packages-foreign.txt", &inputs.packages_foreign),
        ("boot-status.txt", &inputs.boot_status),
        ("kernel-cmdline", &inputs.kernel_cmdline),
        ("initramfs.conf", &inputs.initramfs_config),
        ("bootloader.conf", &inputs.bootloader_config),
    ] {
        if !content.trim().is_empty() {
            documents.insert(name.to_string(), content.clone());
        }
    }

    Ok(documents)
}

fn render_recovery_readme(manifest: &RecoveryKitManifest) -> String {
    format!(
        "# Btrfs Harbor Recovery Kit\n\n\
Profile: {}\n\
Machine: {}\n\
Created: {}\n\n\
## Fresh-install recovery outline\n\n\
1. Install the same distribution family on the replacement machine.\n\
2. Recreate or unlock the target storage layout; use `lsblk.json`, `findmnt.json`, `fstab` and `crypttab` as references, not blind copy instructions.\n\
3. Install Btrfs Harbor / btrfs-backup-ng and Btrfs tools.\n\
4. Restore persistent subvolumes into a separate staging root and verify them before activation.\n\
5. Restore package intent from `packages-explicit.txt` and, when applicable, `packages-foreign.txt`.\n\
6. Reconcile Snapper, initramfs, kernel command line and bootloader configuration for the new hardware.\n\
7. Regenerate initramfs and bootloader entries, then activate the restored root only after verification.\n\n\
Never copy machine identity, EFI state, secrets or hardware-specific identifiers blindly.\n",
        manifest.profile_name, manifest.hostname, manifest.created_at
    )
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StagedRestoreRequest {
    pub destination_id: Uuid,
    pub source_path: PathBuf,
    pub staging_root: PathBuf,
    #[serde(default)]
    pub snapshot: Option<String>,
    #[serde(default)]
    pub before: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct StagedRestorePlan {
    pub profile_id: Uuid,
    pub destination_id: Uuid,
    pub source_path: PathBuf,
    pub staging_path: PathBuf,
    pub target_index: usize,
    pub snapshot: Option<String>,
    pub before: Option<String>,
    pub requires_rescue_environment: bool,
}

pub fn staged_restore_path(staging_root: &Path, profile_id: Uuid, target_subdir: &str) -> PathBuf {
    staging_root
        .join(profile_id.to_string())
        .join(target_subdir)
}

pub fn staging_is_separate_from_live_source(staging: &Path, source: &Path) -> bool {
    if source == Path::new("/") {
        return false;
    }
    staging != source && !staging.starts_with(source) && !source.starts_with(staging)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryRequest {
    pub scope: RecoveryScope,
    pub restore_point: String,
    pub sources: Vec<PathBuf>,
    pub staging_root: PathBuf,
    pub running_root_included: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryIntent {
    ReplaceMachine,
    MigrateMachine,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryCompatibility {
    FullSystem,
    DataMigrationOnly,
    Blocked,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "snake_case")]
pub enum RecoveryActionGroup {
    Restored,
    Adapted,
    Regenerated,
    Attention,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct RecoveryIdentityAction {
    pub id: String,
    pub group: RecoveryActionGroup,
    pub description: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MachineRecoveryRequest {
    pub intent: RecoveryIntent,
    pub source_hostname: String,
    pub requested_hostname: Option<String>,
    pub source_os_release: String,
    pub target_os_release: String,
    pub includes_system: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct MachineRecoveryPlan {
    pub intent: RecoveryIntent,
    pub hostname: String,
    pub compatibility: RecoveryCompatibility,
    pub requires_rescue_environment: bool,
    pub actions: Vec<RecoveryIdentityAction>,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct OsReleaseFamily {
    ids: BTreeSet<String>,
}

impl OsReleaseFamily {
    pub fn compatible_with(&self, other: &Self) -> bool {
        !self.ids.is_empty()
            && !other.ids.is_empty()
            && self.ids.iter().any(|id| other.ids.contains(id))
    }

    pub fn ids(&self) -> impl Iterator<Item = &str> {
        self.ids.iter().map(String::as_str)
    }
}

fn unquote_os_release_value(value: &str) -> &str {
    let value = value.trim();
    if value.len() >= 2
        && ((value.starts_with('"') && value.ends_with('"'))
            || (value.starts_with('\'') && value.ends_with('\'')))
    {
        &value[1..value.len() - 1]
    } else {
        value
    }
}

pub fn parse_os_release_family(raw: &str) -> OsReleaseFamily {
    let mut ids = BTreeSet::new();
    for line in raw.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        match key.trim() {
            "ID" => {
                let id = unquote_os_release_value(value).trim().to_ascii_lowercase();
                if !id.is_empty() {
                    ids.insert(id);
                }
            }
            "ID_LIKE" => {
                for id in unquote_os_release_value(value).split_whitespace() {
                    let id = id.trim().to_ascii_lowercase();
                    if !id.is_empty() {
                        ids.insert(id);
                    }
                }
            }
            _ => {}
        }
    }
    OsReleaseFamily { ids }
}

fn validate_hostname(hostname: &str) -> Result<String, String> {
    let hostname = hostname.trim();
    if hostname.is_empty() || hostname.len() > 63 {
        return Err("hostname must contain between 1 and 63 characters".into());
    }
    if hostname.starts_with('-') || hostname.ends_with('-') {
        return Err("hostname cannot start or end with '-'".into());
    }
    if !hostname
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '-')
    {
        return Err("hostname may contain only letters, digits and '-'".into());
    }
    Ok(hostname.to_string())
}

fn action(id: &str, group: RecoveryActionGroup, description: &str) -> RecoveryIdentityAction {
    RecoveryIdentityAction {
        id: id.to_string(),
        group,
        description: description.to_string(),
    }
}

pub fn plan_machine_recovery(
    request: &MachineRecoveryRequest,
) -> Result<MachineRecoveryPlan, String> {
    let source_hostname = validate_hostname(&request.source_hostname)?;
    let hostname = match request.intent {
        RecoveryIntent::ReplaceMachine => request
            .requested_hostname
            .as_deref()
            .map(validate_hostname)
            .transpose()?
            .unwrap_or_else(|| source_hostname.clone()),
        RecoveryIntent::MigrateMachine => {
            let requested = request
                .requested_hostname
                .as_deref()
                .ok_or_else(|| "migration requires a new hostname".to_string())
                .and_then(validate_hostname)?;
            if requested.eq_ignore_ascii_case(&source_hostname) {
                return Err(
                    "migration hostname must differ from the source machine hostname".into(),
                );
            }
            requested
        }
    };

    let source_family = parse_os_release_family(&request.source_os_release);
    let target_family = parse_os_release_family(&request.target_os_release);
    let compatibility = if !request.includes_system {
        RecoveryCompatibility::FullSystem
    } else if source_family.ids.is_empty() || target_family.ids.is_empty() {
        RecoveryCompatibility::Blocked
    } else if source_family.compatible_with(&target_family) {
        RecoveryCompatibility::FullSystem
    } else {
        RecoveryCompatibility::DataMigrationOnly
    };

    let mut actions = vec![action(
        "user_data",
        RecoveryActionGroup::Restored,
        "Restore user files and application configuration from the selected backup.",
    )];

    match request.intent {
        RecoveryIntent::ReplaceMachine => {
            actions.extend([
                action(
                    "hostname",
                    RecoveryActionGroup::Restored,
                    "Preserve the source hostname for this replacement machine.",
                ),
                action(
                    "machine_id",
                    RecoveryActionGroup::Restored,
                    "Preserve the machine identity because this plan replaces the same machine.",
                ),
                action(
                    "ssh_host_keys",
                    RecoveryActionGroup::Restored,
                    "Preserve SSH host identity for the same-machine replacement.",
                ),
            ]);
        }
        RecoveryIntent::MigrateMachine => {
            actions.extend([
                action(
                    "hostname",
                    RecoveryActionGroup::Adapted,
                    "Apply the new hostname so source and migrated machines can coexist.",
                ),
                action(
                    "machine_id",
                    RecoveryActionGroup::Regenerated,
                    "Generate a fresh systemd machine-id on the migrated machine.",
                ),
                action(
                    "ssh_host_keys",
                    RecoveryActionGroup::Regenerated,
                    "Generate fresh OpenSSH host keys for the migrated machine.",
                ),
                action(
                    "network_profiles",
                    RecoveryActionGroup::Attention,
                    "Review hardware-bound NetworkManager profiles for the new interfaces.",
                ),
                action(
                    "third_party_device_identities",
                    RecoveryActionGroup::Attention,
                    "Re-enrol third-party device identities such as sync/VPN clients when required.",
                ),
            ]);
        }
    }

    if request.includes_system {
        actions.push(action(
            "fstab_crypttab",
            RecoveryActionGroup::Adapted,
            "Rebuild fstab/crypttab references from target storage UUIDs instead of copying source UUIDs blindly.",
        ));

        match compatibility {
            RecoveryCompatibility::FullSystem => {
                actions.extend([
                    action(
                        "system_root",
                        RecoveryActionGroup::Restored,
                        "Restore the compatible system root into staging before activation.",
                    ),
                    action(
                        "initramfs",
                        RecoveryActionGroup::Regenerated,
                        "Regenerate initramfs with the tool available on the target system.",
                    ),
                    action(
                        "bootloader",
                        RecoveryActionGroup::Regenerated,
                        "Recreate bootloader/EFI entries for the target disk layout.",
                    ),
                ]);
            }
            RecoveryCompatibility::DataMigrationOnly => {
                actions.push(action(
                    "system_root",
                    RecoveryActionGroup::Attention,
                    "Source and target distribution families differ; do not transplant the system root. Restore data and user configuration only.",
                ));
            }
            RecoveryCompatibility::Blocked => {
                actions.push(action(
                    "system_root",
                    RecoveryActionGroup::Attention,
                    "Distribution compatibility cannot be established from os-release; system root activation is blocked until reviewed.",
                ));
            }
        }
    }

    Ok(MachineRecoveryPlan {
        intent: request.intent,
        hostname,
        compatibility,
        requires_rescue_environment: request.includes_system
            && compatibility == RecoveryCompatibility::FullSystem,
        actions,
    })
}

pub fn plan_recovery(request: &RecoveryRequest) -> RecoveryPlan {
    let requires_rescue_environment =
        request.scope == RecoveryScope::System || request.running_root_included;

    let mut steps = vec![
        "verify_backup_chain".to_string(),
        "check_destination_space".to_string(),
        "receive_into_staging".to_string(),
        "verify_staged_restore".to_string(),
    ];

    match request.scope {
        RecoveryScope::File | RecoveryScope::Directory => {
            steps.push("copy_selected_content".to_string());
        }
        RecoveryScope::Subvolume => {
            steps.push("prepare_writable_snapshot".to_string());
            steps.push("activate_subvolume_after_confirmation".to_string());
        }
        RecoveryScope::System => {
            steps.extend([
                "restore_persistent_subvolumes".to_string(),
                "reconcile_fstab_and_crypttab".to_string(),
                "restore_snapper_configuration".to_string(),
                "reconcile_packages".to_string(),
                "regenerate_initramfs".to_string(),
                "regenerate_bootloader".to_string(),
                "activate_after_confirmation".to_string(),
            ]);
        }
    }

    RecoveryPlan {
        id: Uuid::new_v4(),
        scope: request.scope,
        restore_point: request.restore_point.clone(),
        sources: request.sources.clone(),
        staging_path: request.staging_root.join(Uuid::new_v4().to_string()),
        requires_rescue_environment,
        steps,
    }
}

pub fn can_execute_on_running_system(plan: &RecoveryPlan) -> bool {
    !plan.requires_rescue_environment && plan.scope != RecoveryScope::System
}

pub fn staging_is_separate_from_destination(staging: &Path, destination: &Path) -> bool {
    staging != destination && !staging.starts_with(destination) && !destination.starts_with(staging)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn recovery_kit_has_stable_safe_relative_directory() {
        let id = Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap();
        assert_eq!(
            recovery_kit_relative_dir(id),
            PathBuf::from("_recovery-kit/11111111-1111-4111-8111-111111111111")
        );
        assert!(!recovery_kit_relative_dir(id).is_absolute());
    }

    #[test]
    fn recovery_kit_documents_are_portable_and_preserve_unicode() {
        let manifest = RecoveryKitManifest {
            schema_version: RECOVERY_KIT_SCHEMA_VERSION,
            created_at: "2026-10-05T12:00:00+02:00".into(),
            profile_id: Uuid::nil(),
            profile_name: "Récupération Workstation".into(),
            hostname: "workstation".into(),
            distro_id: Some("cachyos".into()),
            distro_name: Some("CachyOS".into()),
            kernel_release: "7.2.9-1-cachyos-gcc".into(),
        };
        let inputs = RecoveryKitInputs {
            harbor_config_toml: "name = \"Récupération Workstation\"\n".into(),
            fstab: "UUID=abc / btrfs rw 0 0\n".into(),
            packages_explicit: "btrfs-progs\nsnapper\n".into(),
            ..RecoveryKitInputs::default()
        };

        let docs = recovery_kit_documents(&manifest, &inputs).unwrap();

        assert!(docs["manifest.json"].contains("Récupération Workstation"));
        assert!(docs["README-RESTORE.md"].contains("Fresh-install recovery"));
        assert_eq!(docs["fstab"], inputs.fstab);
        assert_eq!(docs["packages-explicit.txt"], inputs.packages_explicit);
        assert!(!docs.keys().any(|name| name.contains("credential")));
    }

    #[test]
    fn recovery_kit_omits_empty_optional_documents() {
        let manifest = RecoveryKitManifest {
            schema_version: RECOVERY_KIT_SCHEMA_VERSION,
            created_at: "now".into(),
            profile_id: Uuid::nil(),
            profile_name: "test".into(),
            hostname: "host".into(),
            distro_id: None,
            distro_name: None,
            kernel_release: "kernel".into(),
        };
        let docs = recovery_kit_documents(&manifest, &RecoveryKitInputs::default()).unwrap();

        assert!(docs.contains_key("manifest.json"));
        assert!(docs.contains_key("README-RESTORE.md"));
        assert!(docs.contains_key("harbor-profile.toml"));
        assert!(!docs.contains_key("crypttab"));
        assert!(!docs.contains_key("boot-status.txt"));
    }

    #[test]
    fn staged_restore_request_roundtrips_unicode_paths() {
        let request = StagedRestoreRequest {
            destination_id: Uuid::nil(),
            source_path: PathBuf::from("/home/demo/Vidéos"),
            staging_root: PathBuf::from("/mnt/Récupération"),
            snapshot: Some("home-20261005T020000".into()),
            before: None,
        };
        let encoded = serde_json::to_string(&request).unwrap();
        let decoded: StagedRestoreRequest = serde_json::from_str(&encoded).unwrap();
        assert_eq!(decoded, request);
    }

    #[test]
    fn staged_restore_path_is_uuid_scoped_and_source_scoped() {
        let id = Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap();
        assert_eq!(
            staged_restore_path(Path::new("/mnt/stage"), id, "home"),
            PathBuf::from("/mnt/stage/11111111-1111-4111-8111-111111111111/home")
        );
    }

    #[test]
    fn staging_must_be_disjoint_from_destination_in_both_directions() {
        assert!(!staging_is_separate_from_destination(
            Path::new("/backup/staging"),
            Path::new("/backup")
        ));
        assert!(!staging_is_separate_from_destination(
            Path::new("/backup"),
            Path::new("/backup/staging")
        ));
        assert!(staging_is_separate_from_destination(
            Path::new("/mnt/staging"),
            Path::new("/backup")
        ));
    }

    #[test]
    fn staging_cannot_be_inside_live_source_and_root_requires_rescue() {
        assert!(!staging_is_separate_from_live_source(
            Path::new("/home/demo/.restore"),
            Path::new("/home")
        ));
        assert!(staging_is_separate_from_live_source(
            Path::new("/mnt/restore"),
            Path::new("/home")
        ));
        assert!(!staging_is_separate_from_live_source(
            Path::new("/mnt/restore"),
            Path::new("/")
        ));
    }

    #[test]
    fn system_recovery_requires_rescue_environment_and_staged_verify() {
        let plan = plan_recovery(&RecoveryRequest {
            scope: RecoveryScope::System,
            restore_point: "2026-10-05T02:00:00+02:00".into(),
            sources: vec![PathBuf::from("@"), PathBuf::from("@home")],
            staging_root: PathBuf::from("/mnt/recovery-staging"),
            running_root_included: true,
        });

        assert!(plan.requires_rescue_environment);
        assert!(!can_execute_on_running_system(&plan));
        assert_eq!(plan.steps[0], "verify_backup_chain");
        assert!(plan.steps.contains(&"verify_staged_restore".to_string()));
        assert!(plan.steps.contains(&"regenerate_bootloader".to_string()));
    }

    #[test]
    fn file_recovery_can_run_live() {
        let plan = plan_recovery(&RecoveryRequest {
            scope: RecoveryScope::File,
            restore_point: "snapshot-42".into(),
            sources: vec![PathBuf::from("/home/demo/Documents/note.txt")],
            staging_root: PathBuf::from("/var/tmp/btrfs-harbor"),
            running_root_included: false,
        });

        assert!(can_execute_on_running_system(&plan));
        assert!(plan.steps.contains(&"copy_selected_content".to_string()));
    }

    #[test]
    fn replacement_recovery_preserves_machine_identity_but_rebuilds_storage_boot() {
        let plan = plan_machine_recovery(&MachineRecoveryRequest {
            intent: RecoveryIntent::ReplaceMachine,
            source_hostname: "pegasus".into(),
            requested_hostname: None,
            source_os_release: "ID=cachyos\nID_LIKE=arch\n".into(),
            target_os_release: "ID=cachyos\nID_LIKE=arch\n".into(),
            includes_system: true,
        })
        .unwrap();

        assert_eq!(plan.hostname, "pegasus");
        assert_eq!(plan.compatibility, RecoveryCompatibility::FullSystem);
        assert!(plan.requires_rescue_environment);
        assert!(plan.actions.iter().any(|action| {
            action.id == "machine_id" && action.group == RecoveryActionGroup::Restored
        }));
        assert!(plan.actions.iter().any(|action| {
            action.id == "fstab_crypttab" && action.group == RecoveryActionGroup::Adapted
        }));
        assert!(plan.actions.iter().any(|action| {
            action.id == "bootloader" && action.group == RecoveryActionGroup::Regenerated
        }));
    }

    #[test]
    fn migration_requires_distinct_hostname_and_regenerates_identity() {
        let request = MachineRecoveryRequest {
            intent: RecoveryIntent::MigrateMachine,
            source_hostname: "pegasus".into(),
            requested_hostname: Some("asus-n55sf".into()),
            source_os_release: "ID=cachyos\nID_LIKE=arch\n".into(),
            target_os_release: "ID=cachyos\nID_LIKE=arch\n".into(),
            includes_system: true,
        };
        let plan = plan_machine_recovery(&request).unwrap();

        assert_eq!(plan.hostname, "asus-n55sf");
        assert_eq!(plan.compatibility, RecoveryCompatibility::FullSystem);
        assert!(plan.actions.iter().any(|action| {
            action.id == "machine_id" && action.group == RecoveryActionGroup::Regenerated
        }));
        assert!(plan.actions.iter().any(|action| {
            action.id == "ssh_host_keys" && action.group == RecoveryActionGroup::Regenerated
        }));

        let same_name = plan_machine_recovery(&MachineRecoveryRequest {
            requested_hostname: Some("pegasus".into()),
            ..request
        });
        assert!(same_name.is_err());
    }

    #[test]
    fn cross_family_system_restore_is_downgraded_to_data_migration() {
        let plan = plan_machine_recovery(&MachineRecoveryRequest {
            intent: RecoveryIntent::MigrateMachine,
            source_hostname: "pegasus".into(),
            requested_hostname: Some("fedora-box".into()),
            source_os_release: "ID=cachyos\nID_LIKE=arch\n".into(),
            target_os_release: "ID=fedora\nID_LIKE=\"rhel centos\"\n".into(),
            includes_system: true,
        })
        .unwrap();

        assert_eq!(plan.compatibility, RecoveryCompatibility::DataMigrationOnly);
        assert!(plan.actions.iter().any(|action| {
            action.id == "system_root" && action.group == RecoveryActionGroup::Attention
        }));
        assert!(plan.actions.iter().any(|action| {
            action.id == "user_data" && action.group == RecoveryActionGroup::Restored
        }));
    }

    #[test]
    fn os_release_family_uses_id_like_instead_of_cachyos_special_cases() {
        let cachy = parse_os_release_family("ID=cachyos\nID_LIKE=\"arch\"\n");
        let arch = parse_os_release_family("ID=arch\n");
        let fedora = parse_os_release_family("ID=fedora\nID_LIKE=\"rhel centos\"\n");

        assert!(cachy.compatible_with(&arch));
        assert!(!cachy.compatible_with(&fedora));
    }

    #[test]
    fn data_only_migration_remains_available_across_distro_families() {
        let plan = plan_machine_recovery(&MachineRecoveryRequest {
            intent: RecoveryIntent::MigrateMachine,
            source_hostname: "pegasus".into(),
            requested_hostname: Some("debian-box".into()),
            source_os_release: "ID=cachyos\nID_LIKE=arch\n".into(),
            target_os_release: "ID=debian\n".into(),
            includes_system: false,
        })
        .unwrap();

        assert_eq!(plan.compatibility, RecoveryCompatibility::FullSystem);
        assert!(!plan.requires_rescue_environment);
    }

    #[test]
    fn staging_must_not_equal_destination() {
        assert!(!staging_is_separate_from_destination(
            Path::new("/mnt/stage"),
            Path::new("/mnt/stage")
        ));
        assert!(staging_is_separate_from_destination(
            Path::new("/mnt/stage"),
            Path::new("/mnt/target")
        ));
    }
}
