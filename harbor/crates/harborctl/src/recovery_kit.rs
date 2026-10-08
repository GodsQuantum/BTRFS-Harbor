use anyhow::{Context, Result, bail};
use harbor_core::{BackupProfile, DestinationKind, DestinationSpec};
use harbor_recovery::{
    RECOVERY_KIT_SCHEMA_VERSION, RecoveryKitInputs, RecoveryKitManifest, recovery_kit_documents,
    recovery_kit_relative_dir,
};
use std::collections::BTreeMap;
use std::fs::{self, OpenOptions};
use std::io::Write;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::process::Command;
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RecoveryKitReport {
    pub written_directories: Vec<PathBuf>,
    pub skipped_destinations: Vec<String>,
}

pub fn collect_recovery_kit(
    profile: &BackupProfile,
    harbor_config_toml: String,
) -> Result<(RecoveryKitManifest, RecoveryKitInputs)> {
    let os_release = read_optional("/etc/os-release");
    let os_fields = parse_os_release(&os_release);

    let manifest = RecoveryKitManifest {
        schema_version: RECOVERY_KIT_SCHEMA_VERSION,
        created_at: capture_required("date", &["--iso-8601=seconds"])
            .unwrap_or_else(|_| "unknown".into()),
        profile_id: profile.id,
        profile_name: profile.name.clone(),
        hostname: read_optional("/etc/hostname").trim().to_owned(),
        distro_id: os_fields.get("ID").cloned(),
        distro_name: os_fields
            .get("PRETTY_NAME")
            .cloned()
            .or_else(|| os_fields.get("NAME").cloned()),
        kernel_release: capture_required("uname", &["-r"]).unwrap_or_else(|_| "unknown".into()),
    };

    let (packages_explicit, packages_foreign) = collect_package_intent();
    let capabilities = crate::platform::detect_platform_capabilities();
    let package_manager = format!("{:?}\n", capabilities.package_manager).to_ascii_lowercase();
    let platform_capabilities_json =
        serde_json::to_string_pretty(&capabilities).unwrap_or_else(|_| "{}".to_string()) + "\n";

    let inputs = RecoveryKitInputs {
        harbor_config_toml,
        os_release,
        fstab: read_optional("/etc/fstab"),
        crypttab: read_optional("/etc/crypttab"),
        findmnt_json: capture_best_effort(
            "findmnt",
            &["--json", "--output", "TARGET,SOURCE,FSTYPE,OPTIONS"],
        ),
        lsblk_json: capture_best_effort(
            "lsblk",
            &[
                "--json",
                "--output",
                "NAME,PATH,FSTYPE,LABEL,UUID,PARTUUID,MOUNTPOINTS,SIZE,TYPE",
            ],
        ),
        btrfs_subvolumes: capture_best_effort("btrfs", &["subvolume", "list", "-a", "/"]),
        snapper_configs_json: capture_best_effort(
            "snapper",
            &["--jsonout", "list-configs", "--columns", "config,subvolume"],
        ),
        packages_explicit,
        packages_foreign,
        package_manager,
        platform_capabilities_json,
        boot_status: capture_best_effort("bootctl", &["status", "--no-pager"]),
        kernel_cmdline: read_optional("/etc/kernel/cmdline"),
        initramfs_config: first_existing(&["/etc/mkinitcpio.conf", "/etc/dracut.conf"]),
        bootloader_config: first_existing(&[
            "/etc/default/limine",
            "/etc/default/grub",
            "/etc/kernel/uki.conf",
        ]),
    };

    Ok((manifest, inputs))
}

pub fn write_recovery_kits(
    profile: &BackupProfile,
    destinations: &[&DestinationSpec],
    harbor_config_toml: String,
) -> Result<RecoveryKitReport> {
    let (manifest, inputs) = collect_recovery_kit(profile, harbor_config_toml)?;
    let documents = recovery_kit_documents(&manifest, &inputs)?;
    write_documents_to_destinations(profile.id, destinations, &documents)
}

pub fn write_documents_to_destinations(
    profile_id: Uuid,
    destinations: &[&DestinationSpec],
    documents: &BTreeMap<String, String>,
) -> Result<RecoveryKitReport> {
    let mut report = RecoveryKitReport {
        written_directories: Vec::new(),
        skipped_destinations: Vec::new(),
    };

    for destination in destinations {
        if destination.kind == DestinationKind::Ssh {
            report
                .skipped_destinations
                .push(format!("{} (SSH)", destination.name));
            continue;
        }

        if !destination.path.is_dir() {
            bail!(
                "recovery kit destination base {} does not exist",
                destination.path.display()
            );
        }

        let kit_dir = destination.path.join(recovery_kit_relative_dir(profile_id));
        fs::create_dir_all(&kit_dir)
            .with_context(|| format!("cannot create recovery kit {}", kit_dir.display()))?;

        for (name, content) in documents {
            if name.contains('/') || name.contains('\\') || name == "." || name == ".." {
                bail!("unsafe recovery kit document name {name:?}");
            }
            atomic_write_mode(&kit_dir.join(name), content.as_bytes(), 0o644)?;
        }

        report.written_directories.push(kit_dir);
    }

    if report.written_directories.is_empty() && !report.skipped_destinations.is_empty() {
        bail!("recovery kit currently requires at least one local/raw/NFS/SMB destination");
    }

    Ok(report)
}

fn capture_required(program: &str, args: &[&str]) -> Result<String> {
    let output = Command::new(program)
        .args(args)
        .output()
        .with_context(|| format!("cannot execute {program}"))?;
    if !output.status.success() {
        bail!(
            "{program} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(normalize_text(&String::from_utf8_lossy(&output.stdout)))
}

fn capture_best_effort(program: &str, args: &[&str]) -> String {
    capture_required(program, args).unwrap_or_default()
}

fn collect_package_intent() -> (String, String) {
    if let Ok(explicit) = capture_required("pacman", &["-Qqe"]) {
        return (explicit, capture_best_effort("pacman", &["-Qqm"]));
    }

    if let Ok(manual) = capture_required("apt-mark", &["showmanual"]) {
        return (manual, String::new());
    }

    if let Ok(packages) = capture_required("rpm", &["-qa"]) {
        return (packages, String::new());
    }

    (
        capture_best_effort("dpkg-query", &["-W", "-f=${binary:Package}\n"]),
        String::new(),
    )
}

fn read_optional(path: impl AsRef<Path>) -> String {
    fs::read_to_string(path).unwrap_or_default()
}

fn first_existing(paths: &[&str]) -> String {
    paths
        .iter()
        .find_map(|path| {
            let value = read_optional(path);
            (!value.is_empty()).then_some(value)
        })
        .unwrap_or_default()
}

fn parse_os_release(raw: &str) -> BTreeMap<String, String> {
    raw.lines()
        .filter_map(|line| {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                return None;
            }
            let (key, value) = line.split_once('=')?;
            let value = value
                .trim()
                .trim_matches('"')
                .replace("\\\"", "\"")
                .replace("\\\\", "\\");
            Some((key.trim().to_owned(), value))
        })
        .collect()
}

fn normalize_text(value: &str) -> String {
    let value = value.trim_end();
    if value.is_empty() {
        String::new()
    } else {
        format!("{value}\n")
    }
}

fn atomic_write_mode(path: &Path, content: &[u8], mode: u32) -> Result<()> {
    let parent = path
        .parent()
        .with_context(|| format!("{} has no parent directory", path.display()))?;
    fs::create_dir_all(parent)?;

    let tmp = parent.join(format!(
        ".{}.tmp-{}-{}",
        path.file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("recovery-kit"),
        std::process::id(),
        Uuid::new_v4()
    ));

    {
        let mut options = OpenOptions::new();
        options.write(true).create_new(true);
        #[cfg(unix)]
        options.mode(mode);

        let mut file = options
            .open(&tmp)
            .with_context(|| format!("cannot create temporary file {}", tmp.display()))?;
        file.write_all(content)?;
        file.sync_all()?;
    }

    #[cfg(unix)]
    fs::set_permissions(&tmp, fs::Permissions::from_mode(mode))?;

    fs::rename(&tmp, path)
        .with_context(|| format!("cannot atomically replace {}", path.display()))?;

    if let Ok(dir) = fs::File::open(parent) {
        let _ = dir.sync_all();
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_os_release_quotes_without_losing_unicode() {
        let parsed =
            parse_os_release("ID=cachyos\nPRETTY_NAME=\"CachyOS — édition test\"\n# ignored\n");
        assert_eq!(parsed.get("ID").map(String::as_str), Some("cachyos"));
        assert_eq!(
            parsed.get("PRETTY_NAME").map(String::as_str),
            Some("CachyOS — édition test")
        );
    }

    #[test]
    fn writes_recovery_documents_only_below_destination_base() {
        let root = std::env::temp_dir().join(format!("btrfs-harbor-kit-{}", Uuid::new_v4()));
        let destination = root.join("backup");
        fs::create_dir_all(&destination).unwrap();

        let spec = DestinationSpec {
            id: Uuid::new_v4(),
            name: "Disposable".into(),
            kind: DestinationKind::Raw,
            path: destination.clone(),
            mount_point: None,
            expected_mount_source: None,
            compression: "zstd".into(),
            optional: false,
        };
        let profile_id = Uuid::new_v4();
        let documents = BTreeMap::from([
            ("manifest.json".to_string(), "{}\n".to_string()),
            ("README-RESTORE.md".to_string(), "# restore\n".to_string()),
        ]);

        let report = write_documents_to_destinations(profile_id, &[&spec], &documents).unwrap();
        let kit = destination.join(recovery_kit_relative_dir(profile_id));

        assert_eq!(report.written_directories, vec![kit.clone()]);
        assert_eq!(
            fs::read_to_string(kit.join("manifest.json")).unwrap(),
            "{}\n"
        );
        assert!(!root.join("credentials").exists());

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn ssh_only_destination_is_not_falsely_reported_as_supported() {
        let spec = DestinationSpec {
            id: Uuid::new_v4(),
            name: "Remote".into(),
            kind: DestinationKind::Ssh,
            path: PathBuf::from("host:/backup"),
            mount_point: None,
            expected_mount_source: None,
            compression: "zstd".into(),
            optional: false,
        };
        let err = write_documents_to_destinations(
            Uuid::new_v4(),
            &[&spec],
            &BTreeMap::from([("manifest.json".into(), "{}".into())]),
        )
        .unwrap_err()
        .to_string();

        assert!(err.contains("local/raw/NFS/SMB"), "{err}");
    }

    #[test]
    fn unsafe_document_names_are_rejected() {
        let root = std::env::temp_dir().join(format!("btrfs-harbor-kit-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let spec = DestinationSpec {
            id: Uuid::new_v4(),
            name: "Disposable".into(),
            kind: DestinationKind::Raw,
            path: root.clone(),
            mount_point: None,
            expected_mount_source: None,
            compression: "zstd".into(),
            optional: false,
        };

        let err = write_documents_to_destinations(
            Uuid::new_v4(),
            &[&spec],
            &BTreeMap::from([("../escape".into(), "no".into())]),
        )
        .unwrap_err()
        .to_string();

        assert!(err.contains("unsafe recovery kit document name"), "{err}");
        fs::remove_dir_all(root).unwrap();
    }
}
