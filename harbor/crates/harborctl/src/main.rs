mod lxc_replica;
mod platform;
mod recovery_kit;

use anyhow::{Context, Result, bail};
use harbor_core::{
    BackupProfile, BackupSource, DestinationKind, DestinationSpec, EnginePolicy, profile_unit_stem,
};
use harbor_recovery::{
    MachineRecoveryRequest, RecoveryKitManifest, StagedRestorePlan, StagedRestoreRequest,
    plan_machine_recovery, recovery_kit_relative_dir, staged_restore_path,
    staging_is_separate_from_destination, staging_is_separate_from_live_source,
};
use harbor_storage::{
    HarborConfig, MountTable, engine_target_uri, render_engine_config, validate_destination_mount,
};
use serde::{Deserialize, Serialize};
use std::env;
use std::ffi::{CString, OsString};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::MetadataExt;
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::Component;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};
use std::sync::mpsc;
use std::thread;
use uuid::Uuid;

const DEFAULT_CONFIG: &str = "/etc/btrfs-harbor/harbor.toml";
const DEFAULT_GENERATED_DIR: &str = "/var/lib/btrfs-harbor/generated";
const DEFAULT_SYSTEMD_DIR: &str = "/etc/systemd/system";
const DEFAULT_SYSTEMCTL: &str = "/usr/bin/systemctl";
const DEFAULT_BUNDLED_ENGINE: &str = "/usr/lib/btrfs-harbor/btrfs-backup-ng";
const DEFAULT_FINDMNT: &str = "/usr/bin/findmnt";
const DEFAULT_BTRFS: &str = "/usr/bin/btrfs";
const SNAPSHOT_TRIGGER: &str = "@snapshots";

fn main() -> Result<()> {
    let mut args = env::args().skip(1);
    let Some(command) = args.next() else {
        usage();
        bail!("missing command");
    };

    match command.as_str() {
        "list" => list_config(),
        "render-profile" => {
            let id = parse_profile_id(args.next())?;
            let (config, profile) = load_profile(id)?;
            print!("{}", render_engine_config(&profile, &config.destinations)?);
            Ok(())
        }
        "apply-config" => {
            let id = parse_profile_id(args.next())?;
            apply_config_from_stdin(id)
        }
        "set-engine-policy" => {
            let value = args.next().context("missing engine policy")?;
            set_engine_policy(parse_engine_policy(&value)?)
        }
        "install-profile" => {
            let id = parse_profile_id(args.next())?;
            install_profile(id)
        }
        "uninstall-profile" => {
            let id = parse_profile_id(args.next())?;
            uninstall_profile(id)
        }
        "run-profile" => {
            let id = parse_profile_id(args.next())?;
            run_profile(id)
        }
        "run-profile-v2" => {
            let id = parse_profile_id(args.next())?;
            run_profile_v2(id)
        }
        "run-profile-jsonl" => {
            let id = parse_profile_id(args.next())?;
            run_profile_jsonl(id)
        }
        "run-config-jsonl" => {
            let id = parse_profile_id(args.next())?;
            run_config_jsonl(id)
        }
        "recovery-kit" => {
            let id = parse_profile_id(args.next())?;
            recovery_kit_profile(id)
        }
        "recovery-kit-context-json" => {
            let id = parse_profile_id(args.next())?;
            recovery_kit_context_json(id)
        }
        "machine-recovery-plan-json" => machine_recovery_plan_json(),
        "platform-capabilities-json" => {
            println!(
                "{}",
                serde_json::to_string(&platform::detect_platform_capabilities())?
            );
            Ok(())
        }
        "list-restore-points-json" => {
            let id = parse_profile_id(args.next())?;
            list_restore_points_json(id)
        }
        "stage-restore-jsonl" => {
            let id = parse_profile_id(args.next())?;
            stage_restore_jsonl(id)
        }
        "stage-restore-config-jsonl" => {
            let id = parse_profile_id(args.next())?;
            stage_restore_config_jsonl(id)
        }
        "replicate-lxc-jsonl" => lxc_replica::run_lxc_replica_jsonl(),
        "status-profile" => {
            let id = parse_profile_id(args.next())?;
            status_profile(id)
        }
        _ => {
            usage();
            bail!("unknown command: {command}");
        }
    }
}

fn usage() {
    eprintln!(
        "Usage: btrfs-harborctl <list|render-profile|apply-config|set-engine-policy|install-profile|uninstall-profile|run-profile|run-profile-v2|run-profile-jsonl|run-config-jsonl|recovery-kit|recovery-kit-context-json|machine-recovery-plan-json|platform-capabilities-json|list-restore-points-json|stage-restore-jsonl|stage-restore-config-jsonl|replicate-lxc-jsonl|status-profile> [PROFILE_UUID|POLICY]"
    );
}

fn parse_engine_policy(value: &str) -> Result<EnginePolicy> {
    match value.trim().to_ascii_lowercase().as_str() {
        "auto" => Ok(EnginePolicy::Auto),
        "system" => Ok(EnginePolicy::System),
        "bundled" => Ok(EnginePolicy::Bundled),
        other => bail!("invalid engine policy: {other}; expected auto, system, or bundled"),
    }
}

fn set_engine_policy_in_config(config: &mut HarborConfig, policy: EnginePolicy) {
    config.engine_policy = policy;
}

fn set_engine_policy(policy: EnginePolicy) -> Result<()> {
    ensure_root()?;
    let mut config = read_config()?;
    set_engine_policy_in_config(&mut config, policy);
    config
        .validate()
        .map_err(|err| anyhow::anyhow!("invalid Harbor configuration: {err}"))?;
    let encoded = config.to_toml().context("cannot serialize Harbor config")?;
    atomic_write_mode(&config_path(), encoded.as_bytes(), 0o644)?;
    println!("{}", serde_json::to_string(&policy)?);
    Ok(())
}

fn parse_profile_id(value: Option<String>) -> Result<Uuid> {
    let value = value.context("missing profile UUID")?;
    Uuid::parse_str(&value).with_context(|| format!("invalid profile UUID: {value}"))
}

fn debug_path_override(variable: &str, default: &str) -> PathBuf {
    #[cfg(debug_assertions)]
    if let Some(value) = env::var_os(variable) {
        return PathBuf::from(value);
    }

    let _ = variable;
    PathBuf::from(default)
}

fn config_path() -> PathBuf {
    debug_path_override("BTRFS_HARBOR_CONFIG", DEFAULT_CONFIG)
}

fn generated_dir() -> PathBuf {
    debug_path_override("BTRFS_HARBOR_GENERATED_DIR", DEFAULT_GENERATED_DIR)
}

fn systemd_dir() -> PathBuf {
    debug_path_override("BTRFS_HARBOR_SYSTEMD_DIR", DEFAULT_SYSTEMD_DIR)
}

fn portable_engine_near_helper(helper: &Path) -> Option<PathBuf> {
    let parent = helper.parent()?;
    let wrapper = parent.join("btrfs-backup-ng");
    let payload = parent.join("btrfs-backup-ng.pyz");
    (wrapper.is_file() && payload.is_file()).then_some(wrapper)
}

fn bundled_engine_candidate() -> Option<PathBuf> {
    let portable = env::current_exe()
        .ok()
        .and_then(|helper| portable_engine_near_helper(&helper));
    portable.or_else(|| {
        let installed = PathBuf::from(DEFAULT_BUNDLED_ENGINE);
        installed.is_file().then_some(installed)
    })
}

fn engine_resolution_for_policy(policy: EnginePolicy) -> Result<harbor_engine::EngineResolution> {
    #[cfg(debug_assertions)]
    if let Some(value) = env::var_os("BTRFS_HARBOR_ENGINE") {
        return Ok(harbor_engine::EngineResolution {
            executable: PathBuf::from(value),
            origin: harbor_engine::EngineOrigin::System,
            version: None,
        });
    }

    harbor_engine::discover_engine_with_policy(policy, bundled_engine_candidate())
        .map(|status| status.active)
        .map_err(anyhow::Error::from)
}

fn engine_executable_for_policy(policy: EnginePolicy) -> Result<PathBuf> {
    Ok(engine_resolution_for_policy(policy)?.executable)
}

fn engine_progress_message(resolution: &harbor_engine::EngineResolution) -> String {
    let version = resolution
        .version
        .as_deref()
        .map(|version| format!("btrfs-backup-ng {version}"))
        .unwrap_or_else(|| "btrfs-backup-ng".to_owned());
    match resolution.origin {
        harbor_engine::EngineOrigin::System => format!(
            "System engine · {version} · {}",
            resolution.executable.display()
        ),
        harbor_engine::EngineOrigin::Bundled => {
            if resolution
                .executable
                .to_string_lossy()
                .contains("/portable/")
            {
                format!("Bundled AppImage engine · {version}")
            } else {
                format!("Bundled Harbor engine · {version}")
            }
        }
    }
}

fn systemctl_executable() -> PathBuf {
    debug_path_override("BTRFS_HARBOR_SYSTEMCTL", DEFAULT_SYSTEMCTL)
}

fn findmnt_executable() -> PathBuf {
    debug_path_override("BTRFS_HARBOR_FINDMNT", DEFAULT_FINDMNT)
}

fn btrfs_executable() -> PathBuf {
    debug_path_override("BTRFS_HARBOR_BTRFS", DEFAULT_BTRFS)
}

fn read_config() -> Result<HarborConfig> {
    let path = config_path();
    let raw = fs::read_to_string(&path)
        .with_context(|| format!("cannot read Harbor config {}", path.display()))?;
    let config = HarborConfig::from_toml(&raw).context("invalid Harbor config")?;
    config
        .validate()
        .map_err(|err| anyhow::anyhow!("invalid Harbor configuration: {err}"))?;
    Ok(config)
}

fn load_profile_from_json(raw: &str, id: Uuid) -> Result<(HarborConfig, BackupProfile)> {
    let config = parse_config_json(raw)?;
    let profile = config
        .profile(id)
        .cloned()
        .with_context(|| format!("profile {id} not found"))?;
    Ok((config, profile))
}

#[derive(Debug, Deserialize)]
struct RestorePointsQuery {
    configuration: HarborConfig,
    destination_id: Uuid,
    source_path: PathBuf,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
struct RestorePoint {
    name: String,
    created: Option<String>,
    size: Option<u64>,
    parent_name: Option<String>,
    checksum: Option<String>,
    origin: Option<String>,
}

#[derive(Debug, Deserialize)]
struct DraftRestoreEnvelope {
    configuration: HarborConfig,
    request: StagedRestoreRequest,
}

fn parse_restore_points(raw: &str) -> Result<Vec<RestorePoint>> {
    let values: Vec<serde_json::Value> =
        serde_json::from_str(raw).context("invalid restore-point JSON from backup engine")?;
    let mut points = values
        .into_iter()
        .filter_map(|value| {
            let name = value.get("name")?.as_str()?.to_owned();
            let created = value
                .get("created")
                .and_then(|value| value.as_str())
                .map(str::to_owned);
            let size = value.get("size").and_then(|value| value.as_u64());
            let parent_name = value
                .get("parent_name")
                .and_then(|value| value.as_str())
                .map(str::to_owned);
            let checksum = value
                .get("checksum")
                .and_then(|value| value.get("value"))
                .and_then(|value| value.as_str())
                .map(str::to_owned);
            let origin = value
                .get("provenance")
                .and_then(|value| value.get("origin"))
                .and_then(|value| value.as_str())
                .map(str::to_owned);
            Some(RestorePoint {
                name,
                created,
                size,
                parent_name,
                checksum,
                origin,
            })
        })
        .collect::<Vec<_>>();
    points.sort_by(|left, right| {
        right
            .created
            .cmp(&left.created)
            .then_with(|| right.name.cmp(&left.name))
    });
    Ok(points)
}

fn query_restore_points(engine: &Path, uri: &str) -> Result<Vec<RestorePoint>> {
    let output = Command::new(engine)
        .args(["raw", "list", uri, "--json"])
        .output()
        .with_context(|| format!("failed to list backup snapshots for {uri}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        bail!(
            "backup snapshot listing failed for {uri}: {}",
            if stderr.is_empty() { stdout } else { stderr }
        );
    }
    parse_restore_points(&String::from_utf8_lossy(&output.stdout))
}

fn require_restore_coverage(
    source: &BackupSource,
    destination: &DestinationSpec,
    points: &[RestorePoint],
) -> Result<()> {
    if points.is_empty() {
        bail!(
            "backup produced no restorable point for source {} on destination {}",
            source.path.display(),
            destination.name
        );
    }
    Ok(())
}

fn resolve_restore_target<'a>(
    config: &'a HarborConfig,
    profile: &'a BackupProfile,
    source_path: &Path,
    destination_id: Uuid,
) -> Result<(&'a BackupSource, &'a DestinationSpec)> {
    let source = profile
        .sources
        .iter()
        .find(|source| source.path == source_path)
        .with_context(|| {
            format!(
                "profile {} does not contain source {}",
                profile.id,
                source_path.display()
            )
        })?;
    if !profile.destination_ids.contains(&destination_id) {
        bail!(
            "profile {} does not use destination {}",
            profile.id,
            destination_id
        );
    }
    let destination = config
        .destinations
        .iter()
        .find(|destination| destination.id == destination_id)
        .with_context(|| format!("destination {destination_id} not found"))?;
    Ok((source, destination))
}

fn list_restore_points_json(id: Uuid) -> Result<()> {
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("cannot read restore-point query from stdin")?;
    let query: RestorePointsQuery =
        serde_json::from_str(&raw).context("invalid restore-point query")?;
    query
        .configuration
        .validate()
        .map_err(|err| anyhow::anyhow!("invalid Harbor configuration: {err}"))?;
    let profile = query
        .configuration
        .profile(id)
        .cloned()
        .with_context(|| format!("profile {id} not found"))?;
    let (source, destination) = resolve_restore_target(
        &query.configuration,
        &profile,
        &query.source_path,
        query.destination_id,
    )?;

    if destination.kind != DestinationKind::Ssh {
        let mountinfo =
            fs::read_to_string("/proc/self/mountinfo").context("cannot read mount table")?;
        let mounts = MountTable::from_mountinfo(&mountinfo);
        validate_destination_mount(destination, &mounts)
            .with_context(|| format!("destination {} failed mount guard", destination.name))?;
    }

    let uri = engine_target_uri(source, destination)?;
    let engine = engine_executable_for_policy(query.configuration.engine_policy)?;
    let points = query_restore_points(&engine, &uri)?;
    serde_json::to_writer(std::io::stdout(), &points)?;
    Ok(())
}

fn load_profile(id: Uuid) -> Result<(HarborConfig, BackupProfile)> {
    let config = read_config()?;
    let profile = config
        .profile(id)
        .cloned()
        .with_context(|| format!("profile {id} not found"))?;
    Ok((config, profile))
}

fn list_config() -> Result<()> {
    let config = read_config()?;
    println!("{}", serde_json::to_string_pretty(&config)?);
    Ok(())
}

fn generated_config_path(id: Uuid) -> PathBuf {
    generated_dir().join(format!("{id}.toml"))
}

fn draft_generated_config_path(id: Uuid) -> PathBuf {
    std::env::temp_dir().join(format!(
        "btrfs-harbor-draft-{id}-{}-{}.toml",
        std::process::id(),
        Uuid::new_v4()
    ))
}

#[derive(Debug)]
struct ApplyArtifacts {
    main_config: String,
    engine_config: String,
    service: String,
    timer: Option<String>,
    path: Option<String>,
}

fn parse_config_json(raw: &str) -> Result<HarborConfig> {
    let config: HarborConfig =
        serde_json::from_str(raw).context("invalid Harbor configuration JSON")?;
    config
        .validate()
        .map_err(|err| anyhow::anyhow!("invalid Harbor configuration: {err}"))?;
    Ok(config)
}

fn render_apply_artifacts(config: &HarborConfig, id: Uuid) -> Result<ApplyArtifacts> {
    config
        .validate()
        .map_err(|err| anyhow::anyhow!("invalid Harbor configuration: {err}"))?;
    let profile = config
        .profile(id)
        .with_context(|| format!("profile {id} not found"))?;

    let snapshot_trigger = profile.on_calendar.trim() == SNAPSHOT_TRIGGER;
    Ok(ApplyArtifacts {
        main_config: config.to_toml()?,
        engine_config: render_engine_config(profile, &config.destinations)?,
        service: render_service(id),
        timer: (!snapshot_trigger).then(|| render_timer(profile)),
        path: snapshot_trigger
            .then(|| render_snapshot_path(profile))
            .transpose()?,
    })
}

fn write_apply_artifacts(
    config_path: &Path,
    generated_dir: &Path,
    systemd_dir: &Path,
    id: Uuid,
    artifacts: &ApplyArtifacts,
) -> Result<()> {
    fs::create_dir_all(generated_dir)?;
    fs::create_dir_all(systemd_dir)?;

    atomic_write_mode(config_path, artifacts.main_config.as_bytes(), 0o644)?;
    atomic_write_mode(
        &generated_dir.join(format!("{id}.toml")),
        artifacts.engine_config.as_bytes(),
        0o644,
    )?;

    let stem = profile_unit_stem(id);
    atomic_write_mode(
        &systemd_dir.join(format!("{stem}.service")),
        artifacts.service.as_bytes(),
        0o644,
    )?;

    let timer_path = systemd_dir.join(format!("{stem}.timer"));
    let path_path = systemd_dir.join(format!("{stem}.path"));
    match (&artifacts.timer, &artifacts.path) {
        (Some(timer), None) => {
            atomic_write_mode(&timer_path, timer.as_bytes(), 0o644)?;
            let _ = fs::remove_file(&path_path);
        }
        (None, Some(path_unit)) => {
            atomic_write_mode(&path_path, path_unit.as_bytes(), 0o644)?;
            let _ = fs::remove_file(&timer_path);
        }
        _ => bail!("profile must render exactly one schedule unit"),
    }

    Ok(())
}

fn apply_config_from_stdin(id: Uuid) -> Result<()> {
    ensure_root()?;

    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("cannot read Harbor configuration from stdin")?;

    let config = parse_config_json(&raw)?;
    let profile = config
        .profile(id)
        .with_context(|| format!("profile {id} not found"))?;
    validate_schedule(profile)?;

    let artifacts = render_apply_artifacts(&config, id)?;
    write_apply_artifacts(
        &config_path(),
        &generated_dir(),
        &systemd_dir(),
        id,
        &artifacts,
    )?;

    let stem = profile_unit_stem(id);
    let active_unit = if profile.on_calendar.trim() == SNAPSHOT_TRIGGER {
        format!("{stem}.path")
    } else {
        format!("{stem}.timer")
    };
    let stale_unit = if active_unit.ends_with(".path") {
        format!("{stem}.timer")
    } else {
        format!("{stem}.path")
    };
    let _ = run_systemctl(&["disable", "--now", &stale_unit]);
    run_systemctl(&["daemon-reload"])?;
    run_systemctl(&["enable", "--now", &active_unit])?;

    println!("Applied profile {} ({id})", profile.name);
    Ok(())
}

fn install_profile(id: Uuid) -> Result<()> {
    ensure_root()?;
    let (config, profile) = load_profile(id)?;
    validate_schedule(&profile)?;

    fs::create_dir_all(generated_dir())?;
    let generated = render_engine_config(&profile, &config.destinations)?;
    atomic_write(&generated_config_path(id), generated.as_bytes())?;

    let systemd = systemd_dir();
    fs::create_dir_all(&systemd)?;
    let stem = profile_unit_stem(id);
    atomic_write(
        &systemd.join(format!("{stem}.service")),
        render_service(id).as_bytes(),
    )?;
    let active_unit = if profile.on_calendar.trim() == SNAPSHOT_TRIGGER {
        let unit = format!("{stem}.path");
        atomic_write(
            &systemd.join(&unit),
            render_snapshot_path(&profile)?.as_bytes(),
        )?;
        let _ = fs::remove_file(systemd.join(format!("{stem}.timer")));
        unit
    } else {
        let unit = format!("{stem}.timer");
        atomic_write(&systemd.join(&unit), render_timer(&profile).as_bytes())?;
        let _ = fs::remove_file(systemd.join(format!("{stem}.path")));
        unit
    };

    run_systemctl(&["daemon-reload"])?;
    run_systemctl(&["enable", "--now", &active_unit])?;
    println!("Installed profile {} ({})", profile.name, id);
    Ok(())
}

fn uninstall_profile(id: Uuid) -> Result<()> {
    ensure_root()?;
    let stem = profile_unit_stem(id);
    let _ = run_systemctl(&["disable", "--now", &format!("{stem}.timer")]);
    let _ = run_systemctl(&["disable", "--now", &format!("{stem}.path")]);

    for path in [
        systemd_dir().join(format!("{stem}.service")),
        systemd_dir().join(format!("{stem}.timer")),
        systemd_dir().join(format!("{stem}.path")),
        generated_config_path(id),
    ] {
        match fs::remove_file(&path) {
            Ok(()) => {}
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {}
            Err(err) => {
                return Err(err).with_context(|| format!("cannot remove {}", path.display()));
            }
        }
    }
    run_systemctl(&["daemon-reload"])?;
    println!("Uninstalled profile {id}");
    Ok(())
}

fn validate_profile_destinations<'a>(
    config: &'a HarborConfig,
    profile: &BackupProfile,
) -> Result<Vec<&'a DestinationSpec>> {
    let selected = selected_destinations(config, profile)?;
    let mountinfo =
        fs::read_to_string("/proc/self/mountinfo").context("cannot read mount table")?;
    let mounts = MountTable::from_mountinfo(&mountinfo);

    for destination in &selected {
        validate_destination_mount(destination, &mounts)
            .with_context(|| format!("destination {} failed mount guard", destination.name))?;
    }

    Ok(selected)
}

fn refresh_recovery_kit(
    config: &HarborConfig,
    profile: &BackupProfile,
    destinations: &[&DestinationSpec],
) -> Result<recovery_kit::RecoveryKitReport> {
    recovery_kit::write_recovery_kits(profile, destinations, config.to_toml()?)
}

fn recovery_kit_profile(id: Uuid) -> Result<()> {
    ensure_root()?;
    let (config, profile) = load_profile(id)?;
    let selected = validate_profile_destinations(&config, &profile)?;
    let report = refresh_recovery_kit(&config, &profile, &selected)?;

    for path in &report.written_directories {
        println!("Recovery Kit written to {}", path.display());
    }
    for destination in &report.skipped_destinations {
        eprintln!("Recovery Kit skipped for unsupported destination {destination}");
    }
    Ok(())
}

#[derive(Debug, Deserialize)]
struct RecoveryKitContextQuery {
    configuration: HarborConfig,
    destination_id: Uuid,
}

#[derive(Debug, Serialize, Deserialize, PartialEq, Eq)]
struct RecoveryKitContext {
    manifest: RecoveryKitManifest,
    os_release: String,
}

fn recovery_kit_context_from(
    profile_id: Uuid,
    query: RecoveryKitContextQuery,
    mounts: &MountTable,
) -> Result<RecoveryKitContext> {
    let profile = query
        .configuration
        .profile(profile_id)
        .with_context(|| format!("profile {profile_id} not found"))?;
    if !profile.destination_ids.contains(&query.destination_id) {
        bail!(
            "destination {} is not attached to profile {profile_id}",
            query.destination_id
        );
    }
    let destination = query
        .configuration
        .destinations
        .iter()
        .find(|candidate| candidate.id == query.destination_id)
        .with_context(|| format!("destination {} not found", query.destination_id))?;
    if destination.kind == DestinationKind::Ssh {
        bail!("Recovery Kit context over SSH is not yet readable from portable mode");
    }
    validate_destination_mount(destination, mounts)
        .with_context(|| format!("destination {} failed mount guard", destination.name))?;

    let kit_dir = destination.path.join(recovery_kit_relative_dir(profile_id));
    let manifest_raw = fs::read_to_string(kit_dir.join("manifest.json"))
        .with_context(|| format!("cannot read Recovery Kit manifest in {}", kit_dir.display()))?;
    let manifest: RecoveryKitManifest =
        serde_json::from_str(&manifest_raw).context("invalid Recovery Kit manifest")?;
    let os_release = fs::read_to_string(kit_dir.join("os-release")).with_context(|| {
        format!(
            "cannot read Recovery Kit os-release in {}",
            kit_dir.display()
        )
    })?;
    if os_release.trim().is_empty() {
        bail!("Recovery Kit os-release is empty");
    }
    Ok(RecoveryKitContext {
        manifest,
        os_release,
    })
}

fn recovery_kit_context_json(profile_id: Uuid) -> Result<()> {
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("cannot read Recovery Kit context query")?;
    let query: RecoveryKitContextQuery =
        serde_json::from_str(&raw).context("invalid Recovery Kit context query")?;
    let mountinfo =
        fs::read_to_string("/proc/self/mountinfo").context("cannot read mount table")?;
    let mounts = MountTable::from_mountinfo(&mountinfo);
    let context = recovery_kit_context_from(profile_id, query, &mounts)?;
    println!("{}", serde_json::to_string(&context)?);
    Ok(())
}

fn machine_recovery_plan_json() -> Result<()> {
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("cannot read machine recovery request")?;
    let request: MachineRecoveryRequest =
        serde_json::from_str(&raw).context("invalid machine recovery request")?;
    let plan = plan_machine_recovery(&request).map_err(anyhow::Error::msg)?;
    println!("{}", serde_json::to_string(&plan)?);
    Ok(())
}

fn checkpoint_schedule_arguments(
    profile: &BackupProfile,
    source: &BackupSource,
    destination: &DestinationSpec,
) -> Result<Vec<OsString>> {
    if matches!(destination.kind, DestinationKind::Ssh) {
        bail!("SSH checkpoint v2 transport is not implemented; refusing to silently fall back");
    }
    let mut args: Vec<OsString> = ["raw", "checkpoint-v2", "schedule-run", "--profile-id"]
        .into_iter()
        .map(OsString::from)
        .collect();
    args.push(profile.id.to_string().into());
    args.push("--target".into());
    args.push(
        destination
            .path
            .join(&source.target_subdir)
            .into_os_string(),
    );
    args.extend([
        OsString::from("--experimental"),
        OsString::from("--state-dir"),
        OsString::from("/var/lib/btrfs-harbor/checkpoint-v2"),
    ]);
    if matches!(
        destination.kind,
        DestinationKind::Local | DestinationKind::Raw
    ) {
        args.push("--allow-local".into());
    }
    if let Some(config) = &source.snapper_config {
        args.push("--snapper-config".into());
        args.push(config.into());
    } else {
        args.push("--source".into());
        args.push(source.path.as_os_str().to_os_string());
    }
    Ok(args)
}

fn run_profile_v2(id: Uuid) -> Result<()> {
    ensure_root()?;
    let (config, profile) = load_profile(id)?;
    let selected = validate_profile_destinations(&config, &profile)?;
    // Never silently change the legacy remote SSH transport semantics.
    if selected
        .iter()
        .any(|dest| matches!(dest.kind, DestinationKind::Ssh))
    {
        bail!("SSH checkpoint v2 unavailable: legacy SSH profiles are not migrated");
    }
    let engine = bundled_engine_candidate()
        .context("bundled Harbor checkpoint-v2 engine is not installed")?;
    for destination in &selected {
        // The checked mkdirat/fstat mount guard is also used by legacy jobs.
        prepare_local_target_dirs(&profile, destination)?;
        for source in &profile.sources {
            let args = checkpoint_schedule_arguments(&profile, source, destination)?;
            let status = Command::new(&engine).args(args).status().with_context(|| {
                format!(
                    "could not run checkpoint-v2 for {} -> {}",
                    source.path.display(),
                    destination.path.display()
                )
            })?;
            if !status.success() {
                bail!(
                    "checkpoint v2 failed for {}: {status}",
                    source.path.display()
                );
            }
        }
    }
    if profile.verify_after_backup {
        verify_profile_targets(&profile, &selected, &engine)?;
    }
    if let Err(err) = refresh_recovery_kit(&config, &profile, &selected) {
        eprintln!("warning: Recovery Kit refresh failed: {err}");
    }
    println!("Checkpoint-v2 scheduled profile {} completed", profile.name);
    Ok(())
}

fn run_profile(id: Uuid) -> Result<()> {
    ensure_root()?;
    let (config, profile) = load_profile(id)?;
    let selected = validate_profile_destinations(&config, &profile)?;

    for destination in &selected {
        prepare_local_target_dirs(&profile, destination)?;
    }

    fs::create_dir_all(generated_dir())?;
    let generated = render_engine_config(&profile, &config.destinations)?;
    let generated_path = generated_config_path(id);
    atomic_write(&generated_path, generated.as_bytes())?;

    let engine = engine_executable_for_policy(config.engine_policy)?;
    let status = Command::new(&engine)
        .arg("-c")
        .arg(&generated_path)
        .arg("run")
        .status()
        .context("failed to execute btrfs-backup-ng run")?;

    if !status.success() {
        bail!("btrfs-backup-ng run failed with {status}");
    }

    if profile.verify_after_backup {
        verify_profile_targets(&profile, &selected, &engine)?;
    }

    if let Err(err) = refresh_recovery_kit(&config, &profile, &selected) {
        eprintln!("warning: Recovery Kit refresh failed: {err}");
    }

    println!("Profile {} completed successfully", profile.name);
    Ok(())
}

fn progress_line(event: &str, phase: &str, message: &str, stream: Option<&str>) -> Result<String> {
    let mut value = serde_json::json!({
        "event": event,
        "phase": phase,
        "message": message,
    });
    if let Some(stream) = stream {
        value["stream"] = serde_json::Value::String(stream.to_owned());
    }
    Ok(serde_json::to_string(&value)?)
}

fn write_jsonl_line<W: Write>(writer: &mut W, line: &str) -> Result<()> {
    if let Err(err) = writeln!(writer, "{line}") {
        if err.kind() == std::io::ErrorKind::BrokenPipe {
            return Ok(());
        }
        return Err(err.into());
    }
    if let Err(err) = writer.flush() {
        if err.kind() == std::io::ErrorKind::BrokenPipe {
            return Ok(());
        }
        return Err(err.into());
    }
    Ok(())
}

pub(crate) fn emit_progress(
    event: &str,
    phase: &str,
    message: &str,
    stream: Option<&str>,
) -> Result<()> {
    let mut stdout = std::io::stdout().lock();
    let line = progress_line(event, phase, message, stream)?;
    write_jsonl_line(&mut stdout, &line)
}

fn engine_stream_line(stream: &str, phase: &str, line: &str) -> Result<String> {
    if stream == "stdout"
        && let Ok(value) = serde_json::from_str::<serde_json::Value>(line)
        && value.get("event").and_then(serde_json::Value::as_str) == Some("transfer_progress")
        && value.get("schema").and_then(serde_json::Value::as_u64) == Some(1)
    {
        return Ok(serde_json::to_string(&value)?);
    }
    progress_line("output", phase, line, Some(stream))
}

fn emit_engine_stream_line(stream: &str, phase: &str, line: &str) -> Result<()> {
    let mut stdout = std::io::stdout().lock();
    let line = engine_stream_line(stream, phase, line)?;
    write_jsonl_line(&mut stdout, &line)
}

fn enable_engine_progress(command: &mut Command) {
    command.env("BTRFS_BACKUP_NG_PROGRESS_JSONL", "1");
}

fn run_streaming_command(mut command: Command, phase: &str) -> Result<ExitStatus> {
    command.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = command.spawn().context("cannot start backup subprocess")?;
    let stdout = child
        .stdout
        .take()
        .context("backup subprocess has no stdout")?;
    let stderr = child
        .stderr
        .take()
        .context("backup subprocess has no stderr")?;

    let (tx, rx) = mpsc::channel::<(&'static str, String)>();
    let stdout_tx = tx.clone();
    let stdout_thread = thread::spawn(move || {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let _ = stdout_tx.send(("stdout", line));
        }
    });
    let stderr_thread = thread::spawn(move || {
        for line in BufReader::new(stderr).lines().map_while(Result::ok) {
            let _ = tx.send(("stderr", line));
        }
    });

    for (stream, line) in rx {
        emit_engine_stream_line(stream, phase, &line)?;
    }

    let status = child.wait().context("cannot wait for backup subprocess")?;
    let _ = stdout_thread.join();
    let _ = stderr_thread.join();
    Ok(status)
}

fn execute_profile_jsonl(
    config: &HarborConfig,
    profile: &BackupProfile,
    generated_path: &Path,
) -> Result<()> {
    let selected = validate_profile_destinations(config, profile)?;
    emit_progress(
        "phase",
        "prepare",
        &format!("Loaded {}", profile.name),
        None,
    )?;

    for destination in &selected {
        prepare_local_target_dirs(profile, destination)?;
        emit_progress(
            "phase",
            "mount_guard",
            &format!("Destination {} is ready", destination.name),
            None,
        )?;
    }

    let generated = render_engine_config(profile, &config.destinations)?;
    atomic_write_mode(generated_path, generated.as_bytes(), 0o600)?;

    let engine_resolution = engine_resolution_for_policy(config.engine_policy)?;
    emit_progress(
        "phase",
        "engine",
        &engine_progress_message(&engine_resolution),
        None,
    )?;
    emit_progress("phase", "backup", "Running btrfs-backup-ng", None)?;
    let mut engine = Command::new(&engine_resolution.executable);
    engine.arg("-c").arg(generated_path).arg("run");
    enable_engine_progress(&mut engine);
    let status = run_streaming_command(engine, "backup")?;
    if !status.success() {
        bail!("btrfs-backup-ng run failed with {status}");
    }

    for destination in &selected {
        for source in &profile.sources {
            let uri = engine_target_uri(source, destination)?;
            emit_progress(
                "phase",
                "verify",
                &format!(
                    "Checking restore-point coverage for {}",
                    source.path.display()
                ),
                None,
            )?;
            let points = query_restore_points(&engine_resolution.executable, &uri)?;
            require_restore_coverage(source, destination, &points)?;
        }
    }

    if profile.verify_after_backup {
        for destination in &selected {
            for source in &profile.sources {
                let uri = engine_target_uri(source, destination)?;
                emit_progress(
                    "phase",
                    "verify",
                    &format!("Verifying {}", source.path.display()),
                    None,
                )?;
                let mut verify = Command::new(&engine_resolution.executable);
                verify.args(["raw", "verify", &uri, "--json"]);
                let status = run_streaming_command(verify, "verify")?;
                if !status.success() {
                    bail!("raw verification failed for {uri} with {status}");
                }
            }
        }
    }

    emit_progress("phase", "recovery_kit", "Refreshing Recovery Kit", None)?;
    match refresh_recovery_kit(config, profile, &selected) {
        Ok(report) => {
            for path in report.written_directories {
                emit_progress(
                    "output",
                    "recovery_kit",
                    &format!("Recovery Kit: {}", path.display()),
                    Some("stdout"),
                )?;
            }
            for destination in report.skipped_destinations {
                emit_progress(
                    "output",
                    "recovery_kit",
                    &format!("Recovery Kit skipped for {destination}"),
                    Some("stderr"),
                )?;
            }
        }
        Err(err) => {
            emit_progress(
                "output",
                "recovery_kit",
                &format!("Recovery Kit refresh failed: {err}"),
                Some("stderr"),
            )?;
        }
    }

    emit_progress(
        "finished",
        "complete",
        &format!("Profile {} completed successfully", profile.name),
        None,
    )?;
    Ok(())
}

fn run_profile_jsonl(id: Uuid) -> Result<()> {
    ensure_root()?;
    emit_progress("phase", "start", &format!("Starting profile {id}"), None)?;

    let result = (|| -> Result<()> {
        let (config, profile) = load_profile(id)?;
        fs::create_dir_all(generated_dir())?;
        execute_profile_jsonl(&config, &profile, &generated_config_path(id))
    })();

    if let Err(err) = &result {
        let _ = emit_progress("failed", "error", &err.to_string(), None);
    }
    result
}

fn run_config_jsonl(id: Uuid) -> Result<()> {
    ensure_root()?;
    emit_progress(
        "phase",
        "start",
        &format!("Starting portable profile {id}"),
        None,
    )?;

    let generated_path = draft_generated_config_path(id);
    let result = (|| -> Result<()> {
        let mut raw = String::new();
        std::io::stdin()
            .read_to_string(&mut raw)
            .context("cannot read portable Harbor configuration from stdin")?;
        let (config, profile) = load_profile_from_json(&raw, id)?;
        execute_profile_jsonl(&config, &profile, &generated_path)
    })();

    let _ = fs::remove_file(&generated_path);
    if let Err(err) = &result {
        let _ = emit_progress("failed", "error", &err.to_string(), None);
    }
    result
}

fn read_staged_restore_request() -> Result<StagedRestoreRequest> {
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("cannot read staged restore request from stdin")?;
    serde_json::from_str(&raw).context("invalid staged restore request JSON")
}

fn require_btrfs_staging_root(path: &Path) -> Result<PathBuf> {
    if !path.is_absolute() {
        bail!("staging root must be an absolute path");
    }
    if !path.is_dir() {
        bail!(
            "staging root {} does not exist or is not a directory",
            path.display()
        );
    }

    let canonical = fs::canonicalize(path)
        .with_context(|| format!("cannot resolve staging root {}", path.display()))?;
    let output = Command::new(findmnt_executable())
        .args(["-n", "-o", "FSTYPE", "-T"])
        .arg(&canonical)
        .output()
        .context("cannot inspect staging filesystem")?;
    if !output.status.success() {
        bail!(
            "cannot determine staging filesystem for {}",
            canonical.display()
        );
    }
    let fs_type = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if fs_type != "btrfs" {
        bail!(
            "staging root {} uses filesystem {}, expected btrfs",
            canonical.display(),
            if fs_type.is_empty() {
                "unknown"
            } else {
                &fs_type
            }
        );
    }
    Ok(canonical)
}

fn staged_restore_plan(
    config: &HarborConfig,
    profile: &BackupProfile,
    request: &StagedRestoreRequest,
) -> Result<(StagedRestorePlan, BackupSource, DestinationSpec)> {
    let source = profile
        .sources
        .iter()
        .find(|source| source.path == request.source_path)
        .cloned()
        .with_context(|| {
            format!(
                "profile {} does not contain source {}",
                profile.id,
                request.source_path.display()
            )
        })?;

    let target_index = profile
        .destination_ids
        .iter()
        .position(|id| *id == request.destination_id)
        .with_context(|| {
            format!(
                "profile {} does not use destination {}",
                profile.id, request.destination_id
            )
        })?;
    let destination = config
        .destinations
        .iter()
        .find(|destination| destination.id == request.destination_id)
        .cloned()
        .with_context(|| format!("destination {} not found", request.destination_id))?;

    let staging_root = require_btrfs_staging_root(&request.staging_root)?;
    if !staging_is_separate_from_live_source(&staging_root, &source.path) {
        if source.path == Path::new("/") {
            bail!("restoring the running system root requires a rescue environment");
        }
        bail!(
            "staging root {} overlaps live source {}",
            staging_root.display(),
            source.path.display()
        );
    }

    if destination.kind != DestinationKind::Ssh {
        if !destination.path.is_dir() {
            bail!(
                "backup destination {} does not exist",
                destination.path.display()
            );
        }
        let destination_path = fs::canonicalize(&destination.path).with_context(|| {
            format!(
                "cannot resolve backup destination {}",
                destination.path.display()
            )
        })?;
        if !staging_is_separate_from_destination(&staging_root, &destination_path) {
            bail!(
                "staging root {} overlaps backup destination {}",
                staging_root.display(),
                destination_path.display()
            );
        }
    }

    let snapshot = request
        .snapshot
        .as_ref()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty());
    let before = request
        .before
        .as_ref()
        .map(|value| value.trim().to_owned())
        .filter(|value| !value.is_empty());
    if snapshot.is_some() && before.is_some() {
        bail!("choose either an exact snapshot or a point in time, not both");
    }

    let stage_path = staged_restore_path(&staging_root, profile.id, &source.target_subdir);
    Ok((
        StagedRestorePlan {
            profile_id: profile.id,
            destination_id: destination.id,
            source_path: source.path.clone(),
            staging_path: stage_path,
            target_index,
            snapshot,
            before,
            requires_rescue_environment: source.path == Path::new("/"),
        },
        source,
        destination,
    ))
}

fn build_restore_command(
    engine_executable: &Path,
    generated_path: &Path,
    plan: &StagedRestorePlan,
    dry_run: bool,
) -> Command {
    let mut command = Command::new(engine_executable);
    command
        .arg("-c")
        .arg(generated_path)
        .arg("restore")
        .arg("--volume")
        .arg(&plan.source_path)
        .arg("--target")
        .arg(plan.target_index.to_string())
        .arg("--to")
        .arg(&plan.staging_path)
        .arg("--no-progress");
    if let Some(snapshot) = &plan.snapshot {
        command.arg("--snapshot").arg(snapshot);
    } else if let Some(before) = &plan.before {
        command.arg("--before").arg(before);
    }
    if dry_run {
        command.arg("--dry-run");
    }
    command
}

fn verify_staged_subvolumes(path: &Path) -> Result<String> {
    let output = Command::new(btrfs_executable())
        .args(["subvolume", "list", "-o"])
        .arg(path)
        .output()
        .context("cannot inspect staged Btrfs subvolumes")?;
    if !output.status.success() {
        bail!(
            "staged restore inspection failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    let listing = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if listing.is_empty() {
        bail!("staged restore contains no Btrfs subvolume");
    }
    Ok(listing)
}

fn execute_stage_restore_jsonl(
    config: &HarborConfig,
    profile: &BackupProfile,
    request: &StagedRestoreRequest,
    generated_path: &Path,
) -> Result<()> {
    let (plan, _source, destination) = staged_restore_plan(config, profile, request)?;

    let mountinfo =
        fs::read_to_string("/proc/self/mountinfo").context("cannot read mount table")?;
    let mounts = MountTable::from_mountinfo(&mountinfo);
    validate_destination_mount(&destination, &mounts)
        .with_context(|| format!("destination {} failed mount guard", destination.name))?;

    let generated = render_engine_config(profile, &config.destinations)?;
    atomic_write_mode(generated_path, generated.as_bytes(), 0o600)?;
    let engine = engine_executable_for_policy(config.engine_policy)?;
    if let Some(parent) = plan.staging_path.parent() {
        fs::create_dir_all(parent)
            .with_context(|| format!("cannot create staging parent {}", parent.display()))?;
    }

    emit_progress(
        "phase",
        "restore_plan",
        &format!("Dry-running restore of {}", plan.source_path.display()),
        None,
    )?;
    let status = run_streaming_command(
        build_restore_command(&engine, generated_path, &plan, true),
        "restore_plan",
    )?;
    if !status.success() {
        bail!("restore dry-run failed with {status}");
    }

    emit_progress(
        "phase",
        "restore",
        &format!("Restoring into {}", plan.staging_path.display()),
        None,
    )?;
    let status = run_streaming_command(
        build_restore_command(&engine, generated_path, &plan, false),
        "restore",
    )?;
    if !status.success() {
        bail!("staged restore failed with {status}");
    }

    emit_progress(
        "phase",
        "restore_verify",
        "Verifying staged Btrfs subvolumes",
        None,
    )?;
    let listing = verify_staged_subvolumes(&plan.staging_path)?;
    emit_progress("output", "restore_verify", &listing, Some("stdout"))?;

    emit_progress(
        "finished",
        "restore_complete",
        &format!("Staged restore ready at {}", plan.staging_path.display()),
        None,
    )?;
    Ok(())
}

fn stage_restore_jsonl(id: Uuid) -> Result<()> {
    ensure_root()?;
    emit_progress(
        "phase",
        "restore_start",
        &format!("Preparing restore for {id}"),
        None,
    )?;

    let result = (|| -> Result<()> {
        let request = read_staged_restore_request()?;
        let (config, profile) = load_profile(id)?;
        fs::create_dir_all(generated_dir())?;
        execute_stage_restore_jsonl(&config, &profile, &request, &generated_config_path(id))
    })();

    if let Err(err) = &result {
        let _ = emit_progress("failed", "restore_error", &err.to_string(), None);
    }
    result
}

fn stage_restore_config_jsonl(id: Uuid) -> Result<()> {
    ensure_root()?;
    emit_progress(
        "phase",
        "restore_start",
        &format!("Preparing portable restore for {id}"),
        None,
    )?;

    let generated_path = draft_generated_config_path(id);
    let result = (|| -> Result<()> {
        let mut raw = String::new();
        std::io::stdin()
            .read_to_string(&mut raw)
            .context("cannot read portable restore request from stdin")?;
        let envelope: DraftRestoreEnvelope =
            serde_json::from_str(&raw).context("invalid portable restore request")?;
        envelope
            .configuration
            .validate()
            .map_err(|err| anyhow::anyhow!("invalid Harbor configuration: {err}"))?;
        let profile = envelope
            .configuration
            .profile(id)
            .cloned()
            .with_context(|| format!("profile {id} not found"))?;
        execute_stage_restore_jsonl(
            &envelope.configuration,
            &profile,
            &envelope.request,
            &generated_path,
        )
    })();

    let _ = fs::remove_file(&generated_path);
    if let Err(err) = &result {
        let _ = emit_progress("failed", "restore_error", &err.to_string(), None);
    }
    result
}

fn status_profile(id: Uuid) -> Result<()> {
    let generated_path = generated_config_path(id);
    if !generated_path.is_file() {
        bail!("profile {id} is not installed");
    }
    let config = read_config()?;
    let engine = engine_executable_for_policy(config.engine_policy)?;

    let output = Command::new(engine)
        .arg("-c")
        .arg(&generated_path)
        .args(["status", "--format", "json"])
        .output()
        .context("failed to execute btrfs-backup-ng status")?;

    std::io::stdout().write_all(&output.stdout)?;
    if !output.status.success() && output.stdout.is_empty() {
        std::io::stderr().write_all(&output.stderr)?;
        bail!("status failed with {}", output.status);
    }
    Ok(())
}

fn selected_destinations<'a>(
    config: &'a HarborConfig,
    profile: &BackupProfile,
) -> Result<Vec<&'a DestinationSpec>> {
    let by_id = config.destinations_by_id();
    profile
        .destination_ids
        .iter()
        .map(|id| {
            by_id
                .get(id)
                .copied()
                .with_context(|| format!("profile references missing destination {id}"))
        })
        .collect()
}

fn checked_directory_fd(destination: &DestinationSpec) -> Result<OwnedFd> {
    // Neither network roots nor parent paths are invented by a backup job.
    if destination.path.is_symlink() || !destination.path.is_dir() {
        bail!(
            "destination {} must be an existing real directory",
            destination.path.display()
        );
    }
    let cpath = CString::new(destination.path.as_os_str().as_bytes())
        .context("backup destination contains a NUL byte")?;
    // SAFETY: cpath is NUL terminated, and open returns a new owned fd.
    let fd = unsafe {
        libc::open(
            cpath.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if fd < 0 {
        return Err(std::io::Error::last_os_error())
            .with_context(|| format!("cannot open destination {}", destination.path.display()));
    }
    // SAFETY: successful open returned ownership of a new, nonnegative fd.
    Ok(unsafe { OwnedFd::from_raw_fd(fd) })
}

fn ensure_live_destination(destination: &DestinationSpec, root_fd: &OwnedFd) -> Result<()> {
    let current_mounts = fs::read_to_string("/proc/self/mountinfo")
        .context("cannot recheck live destination mount table")?;
    let mounts = MountTable::from_mountinfo(&current_mounts);
    validate_destination_mount(destination, &mounts)
        .with_context(|| format!("destination {} mount changed", destination.name))?;

    if destination.path.is_symlink() {
        bail!("destination directory became a symbolic link");
    }
    let path = fs::metadata(&destination.path)
        .context("destination disappeared while preparing target subdirectories")?;
    // fstat against the directory descriptor pinned before any mkdirat.
    // SAFETY: root_fd is valid, and stat_buf points to writable initialized storage.
    let mut stat_buf: libc::stat = unsafe { std::mem::zeroed() };
    let rc = unsafe { libc::fstat(root_fd.as_raw_fd(), &mut stat_buf) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error())
            .context("cannot stat pinned destination descriptor");
    }
    if path.dev() != stat_buf.st_dev || path.ino() != stat_buf.st_ino {
        bail!("destination mount was detached or replaced during backup preparation");
    }
    Ok(())
}

fn create_target_below_pinned(
    destination: &DestinationSpec,
    root_fd: &OwnedFd,
    target_subdir: &str,
) -> Result<()> {
    let mut current = root_fd.try_clone()?;
    let mut components = 0usize;
    for part in Path::new(target_subdir).components() {
        let Component::Normal(name) = part else {
            bail!("unsafe backup destination subdirectory component");
        };
        components += 1;
        if components > 128 {
            bail!("backup target has too many subdirectory components");
        }
        let cpart =
            CString::new(name.as_bytes()).context("backup subdirectory contains a NUL byte")?;
        ensure_live_destination(destination, root_fd)?;

        // SAFETY: all path components are relative to a directory descriptor,
        // and only one component is created; it cannot rebuild a missing mount.
        let made = unsafe { libc::mkdirat(current.as_raw_fd(), cpart.as_ptr(), 0o700) };
        if made != 0 {
            let err = std::io::Error::last_os_error();
            if err.kind() != std::io::ErrorKind::AlreadyExists {
                return Err(err).context("cannot create target below pinned destination");
            }
        }
        // O_NOFOLLOW also rejects symlinks left by other tenants.
        // SAFETY: cpart is NUL terminated and openat yields a new owned fd.
        let child = unsafe {
            libc::openat(
                current.as_raw_fd(),
                cpart.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        if child < 0 {
            return Err(std::io::Error::last_os_error())
                .context("backup target component is unsafe or no longer accessible");
        }
        // SAFETY: the new fd has single ownership and is closed on drop.
        current = unsafe { OwnedFd::from_raw_fd(child) };
        ensure_live_destination(destination, root_fd)?;
    }
    if components == 0 {
        bail!("backup target subdirectory cannot be empty");
    }
    Ok(())
}

fn prepare_local_target_dirs(profile: &BackupProfile, destination: &DestinationSpec) -> Result<()> {
    if matches!(destination.kind, DestinationKind::Ssh) {
        return Ok(());
    }
    let pinned = checked_directory_fd(destination)?;
    for source in &profile.sources {
        create_target_below_pinned(destination, &pinned, &source.target_subdir).with_context(
            || {
                format!(
                    "cannot safely prepare backup target {}",
                    destination.path.join(&source.target_subdir).display()
                )
            },
        )?;
    }
    Ok(())
}

fn verify_profile_targets(
    profile: &BackupProfile,
    destinations: &[&DestinationSpec],
    engine: &Path,
) -> Result<()> {
    for destination in destinations {
        for source in &profile.sources {
            let uri = engine_target_uri(source, destination)?;
            let status = Command::new(engine)
                .args(["raw", "verify", &uri, "--json"])
                .status()
                .with_context(|| format!("failed to verify {uri}"))?;
            if !status.success() {
                bail!("raw verification failed for {uri} with {status}");
            }
        }
    }
    Ok(())
}

fn render_service(id: Uuid) -> String {
    format!(
        "[Unit]\nDescription=Btrfs Harbor backup profile {id}\nAfter=network-online.target\nWants=network-online.target\n\n[Service]\nType=oneshot\nExecStart=/usr/bin/btrfs-harborctl run-profile {id}\nUser=root\nGroup=root\nUMask=0077\nNice=10\nIOSchedulingClass=best-effort\nIOSchedulingPriority=6\n"
    )
}

fn render_timer(profile: &BackupProfile) -> String {
    format!(
        "[Unit]
Description=Btrfs Harbor schedule for profile {}

[Timer]
OnCalendar={}
Persistent=true
RandomizedDelaySec=5m
AccuracySec=1m
Unit={}.service

[Install]
WantedBy=timers.target
",
        profile.id,
        profile.on_calendar,
        profile_unit_stem(profile.id)
    )
}

fn render_snapshot_path(profile: &BackupProfile) -> Result<String> {
    let mut paths = profile
        .sources
        .iter()
        .filter(|source| source.snapper_config.is_some())
        .map(|source| source.path.join(".snapshots"))
        .collect::<Vec<_>>();
    paths.sort();
    paths.dedup();

    if paths.is_empty() {
        bail!("snapshot-triggered scheduling needs at least one Snapper-managed source");
    }

    let mut unit = format!(
        "[Unit]\nDescription=Btrfs Harbor snapshot trigger for profile {}\n\n[Path]\n",
        profile.id
    );
    for path in paths {
        let value = path.to_string_lossy();
        if value.contains('\n') || value.contains('\r') {
            bail!("snapshot watch path contains a newline");
        }
        unit.push_str(&format!("PathChanged={value}\n"));
    }
    unit.push_str(&format!(
        "Unit={}.service\n\n[Install]\nWantedBy=paths.target\n",
        profile_unit_stem(profile.id)
    ));
    Ok(unit)
}

fn validate_schedule(profile: &BackupProfile) -> Result<()> {
    if profile.on_calendar.trim() == SNAPSHOT_TRIGGER {
        if profile
            .sources
            .iter()
            .any(|source| source.snapper_config.is_some())
        {
            return Ok(());
        }
        bail!("snapshot-triggered scheduling needs at least one Snapper-managed source");
    }
    validate_calendar(&profile.on_calendar)
}

fn validate_calendar(spec: &str) -> Result<()> {
    if spec.trim().is_empty() || spec.contains('\n') || spec.contains('\r') {
        bail!("invalid empty or multiline OnCalendar specification");
    }
    let output = Command::new("systemd-analyze")
        .arg("calendar")
        .arg(spec)
        .output()
        .context("systemd-analyze calendar is unavailable")?;
    if !output.status.success() {
        bail!(
            "invalid OnCalendar value {spec:?}: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(())
}

fn run_systemctl(args: &[&str]) -> Result<ExitStatus> {
    let status = Command::new(systemctl_executable())
        .args(args)
        .status()
        .with_context(|| format!("failed to execute systemctl {}", args.join(" ")))?;
    if !status.success() {
        bail!("systemctl {} failed with {status}", args.join(" "));
    }
    Ok(status)
}

fn atomic_write(path: &Path, content: &[u8]) -> Result<()> {
    atomic_write_mode(path, content, 0o644)
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
            .unwrap_or("harbor"),
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

#[cfg(unix)]
fn ensure_root() -> Result<()> {
    let output = Command::new("id")
        .arg("-u")
        .output()
        .context("cannot determine current uid")?;
    let uid = String::from_utf8_lossy(&output.stdout).trim().to_owned();
    if uid != "0" {
        bail!("this operation must run as root");
    }
    Ok(())
}

#[cfg(not(unix))]
fn ensure_root() -> Result<()> {
    bail!("Btrfs Harbor is supported on Linux")
}

#[cfg(test)]
mod tests {
    use super::*;
    use harbor_core::{BackupSource, RetentionPolicy};

    fn profile() -> BackupProfile {
        BackupProfile {
            id: Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap(),
            name: "Workstation recovery".into(),
            sources: vec![BackupSource {
                path: PathBuf::from("/"),
                snapshot_prefix: "root-".into(),
                snapper_config: Some("root".into()),
                target_subdir: "rootfs".into(),
            }],
            destination_ids: vec![],
            on_calendar: "*-*-* 02:00:00".into(),
            retention: RetentionPolicy {
                hourly: 0,
                daily: 7,
                weekly: 4,
                monthly: 3,
                yearly: 0,
            },
            verify_after_backup: true,
        }
    }

    #[test]
    fn pinned_backup_destination_preparation_is_leaf_only_and_refuses_symlinks() {
        use std::os::unix::fs::symlink;
        let base = std::env::temp_dir().join(format!("harbor-mkdirat-{}", Uuid::new_v4()));
        fs::create_dir(&base).unwrap();
        let backup = base.join("backup");
        fs::create_dir(&backup).unwrap();
        let destination = DestinationSpec {
            id: Uuid::new_v4(),
            name: "isolated test".into(),
            kind: DestinationKind::Local,
            path: backup.clone(),
            mount_point: None,
            expected_mount_source: None,
            compression: "zstd".into(),
            optional: false,
        };
        let mut p = profile();
        p.sources[0].target_subdir = "rootfs/2026".into();
        prepare_local_target_dirs(&p, &destination).unwrap();
        assert!(backup.join("rootfs/2026").is_dir());

        p.sources[0].target_subdir = "../outside".into();
        assert!(prepare_local_target_dirs(&p, &destination).is_err());
        assert!(!base.join("outside").exists());

        let external = base.join("external");
        fs::create_dir(&external).unwrap();
        symlink(&external, backup.join("linked")).unwrap();
        p.sources[0].target_subdir = "linked/secret".into();
        assert!(prepare_local_target_dirs(&p, &destination).is_err());
        assert!(!external.join("secret").exists());

        // A pinned fd is NOT permission to write into a replacement mount.
        let pinned = checked_directory_fd(&destination).unwrap();
        fs::rename(&backup, base.join("detached")).unwrap();
        fs::create_dir(&backup).unwrap();
        assert!(ensure_live_destination(&destination, &pinned).is_err());

        fs::remove_dir_all(&base).unwrap();
    }

    #[test]
    fn v2_scheduled_profile_reuses_existing_systemd_and_checkpoint_engine() {
        let p = profile();
        let source = &p.sources[0];
        let destination = DestinationSpec {
            id: Uuid::new_v4(),
            name: "dedicated destination".into(),
            kind: DestinationKind::Nfs,
            path: PathBuf::from("/mnt/backup"),
            mount_point: Some(PathBuf::from("/mnt/backup")),
            expected_mount_source: None,
            compression: "zstd".into(),
            optional: false,
        };
        let args = checkpoint_schedule_arguments(&p, source, &destination).unwrap();
        let text = args.iter().map(|v| v.to_string_lossy()).collect::<Vec<_>>();
        assert!(
            text.windows(2)
                .any(|w| w == ["--target", "/mnt/backup/rootfs"])
        );
        assert!(text.windows(2).any(|w| w == ["--snapper-config", "root"]));
        assert!(!text.iter().any(|item| item == "--allow-local"));
        assert!(text.iter().any(|item| item == "schedule-run"));
        assert!(text.iter().any(|item| item == "--experimental"));

        let local = DestinationSpec {
            kind: DestinationKind::Local,
            ..destination.clone()
        };
        let args = checkpoint_schedule_arguments(&p, source, &local).unwrap();
        assert!(args.iter().any(|arg| arg == "--allow-local"));
        let native = BackupSource {
            snapper_config: None,
            path: PathBuf::from("/home"),
            ..source.clone()
        };
        let args = checkpoint_schedule_arguments(&p, &native, &local).unwrap();
        assert!(
            args.windows(2)
                .any(|pair| pair[0] == "--source" && pair[1] == "/home")
        );

        let ssh = DestinationSpec {
            kind: DestinationKind::Ssh,
            ..destination
        };
        assert!(checkpoint_schedule_arguments(&p, source, &ssh).is_err());
    }

    fn configuration() -> HarborConfig {
        let destination_id = Uuid::parse_str("22222222-2222-4222-8222-222222222222").unwrap();
        HarborConfig {
            engine_policy: harbor_core::EnginePolicy::Auto,
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
                destination_ids: vec![destination_id],
                ..profile()
            }],
        }
    }

    #[test]
    fn apply_json_parses_and_validates_typed_configuration() {
        let raw = serde_json::to_string(&configuration()).unwrap();
        let parsed = parse_config_json(&raw).unwrap();

        assert_eq!(parsed.profiles[0].name, "Workstation recovery");
        assert_eq!(parsed.destinations[0].name, "Backup NAS");
    }

    #[test]
    fn draft_config_selects_profile_without_reading_system_configuration() {
        let config = configuration();
        let id = config.profiles[0].id;
        let raw = serde_json::to_string(&config).unwrap();

        let (parsed, selected) = load_profile_from_json(&raw, id).unwrap();

        assert_eq!(selected.id, id);
        assert_eq!(selected.name, "Workstation recovery");
        assert_eq!(parsed.destinations.len(), 1);
    }

    #[test]
    fn draft_backup_uses_ephemeral_config_outside_persistent_harbor_state() {
        let id = Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap();
        let path = draft_generated_config_path(id);

        assert!(path.starts_with(std::env::temp_dir()));
        assert!(!path.starts_with("/etc/btrfs-harbor"));
        assert!(!path.starts_with("/var/lib/btrfs-harbor"));
        assert!(
            path.file_name()
                .unwrap()
                .to_string_lossy()
                .contains(&id.to_string())
        );
    }

    #[test]
    fn portable_helper_uses_sibling_engine_only_when_payload_is_complete() {
        let root = std::env::temp_dir().join(format!("btrfs-harbor-portable-{}", Uuid::new_v4()));
        fs::create_dir_all(&root).unwrap();
        let helper = root.join("btrfs-harborctl");
        let wrapper = root.join("btrfs-backup-ng");
        let payload = root.join("btrfs-backup-ng.pyz");
        fs::write(&helper, b"helper").unwrap();
        fs::write(&wrapper, b"engine").unwrap();

        assert_eq!(portable_engine_near_helper(&helper), None);

        fs::write(&payload, b"payload").unwrap();
        assert_eq!(portable_engine_near_helper(&helper), Some(wrapper));

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn apply_json_rejects_unsafe_target_subdirectory() {
        let mut config = configuration();
        config.profiles[0].sources[0].target_subdir = "../escape".into();
        let raw = serde_json::to_string(&config).unwrap();

        let err = parse_config_json(&raw).unwrap_err().to_string();
        assert!(err.contains("unsafe target subdirectory"), "{err}");
    }

    #[test]
    fn apply_artifacts_are_complete_before_any_write() {
        let config = configuration();
        let id = config.profiles[0].id;
        let artifacts = render_apply_artifacts(&config, id).unwrap();

        assert!(artifacts.main_config.contains("[[profiles]]"));
        assert!(artifacts.engine_config.contains("incremental = true"));
        assert!(artifacts.engine_config.contains("source = \"snapper\""));
        assert!(artifacts.service.contains(&format!("run-profile {id}")));
        let timer = artifacts
            .timer
            .as_deref()
            .expect("calendar profile renders timer");
        assert!(timer.contains("OnCalendar=*-*-* 02:00:00"));
        assert!(timer.contains("Persistent=true"));
        assert!(artifacts.path.is_none());
    }

    #[cfg(unix)]
    #[test]
    fn apply_artifacts_write_to_explicit_roots_with_safe_modes() {
        use std::os::unix::fs::PermissionsExt;

        let root = std::env::temp_dir().join(format!("btrfs-harbor-test-{}", Uuid::new_v4()));
        let config_path = root.join("etc/harbor.toml");
        let generated = root.join("var/generated");
        let systemd = root.join("systemd");
        let config = configuration();
        let id = config.profiles[0].id;
        let artifacts = render_apply_artifacts(&config, id).unwrap();

        write_apply_artifacts(&config_path, &generated, &systemd, id, &artifacts).unwrap();

        let generated_path = generated.join(format!("{id}.toml"));
        let stem = profile_unit_stem(id);
        let service_path = systemd.join(format!("{stem}.service"));
        let timer_path = systemd.join(format!("{stem}.timer"));

        assert_eq!(
            fs::read_to_string(&config_path).unwrap(),
            artifacts.main_config
        );
        assert_eq!(
            fs::read_to_string(&generated_path).unwrap(),
            artifacts.engine_config
        );
        assert_eq!(
            fs::metadata(&config_path).unwrap().permissions().mode() & 0o777,
            0o644
        );
        assert_eq!(
            fs::metadata(&generated_path).unwrap().permissions().mode() & 0o777,
            0o644
        );
        assert!(service_path.is_file());
        assert!(timer_path.is_file());

        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn snapshot_schedule_renders_native_systemd_path_unit() {
        let mut config = configuration();
        config.profiles[0].on_calendar = SNAPSHOT_TRIGGER.into();
        let id = config.profiles[0].id;

        let artifacts = render_apply_artifacts(&config, id).unwrap();
        let path = artifacts
            .path
            .as_deref()
            .expect("snapshot profile renders path");

        assert!(artifacts.timer.is_none());
        assert!(path.contains("PathChanged=/.snapshots"));
        assert!(path.contains(&format!("Unit={}.service", profile_unit_stem(id))));
        validate_schedule(&config.profiles[0]).unwrap();
    }

    #[test]
    fn snapshot_schedule_requires_a_snapper_managed_source() {
        let mut p = profile();
        p.on_calendar = SNAPSHOT_TRIGGER.into();
        p.sources[0].snapper_config = None;

        let err = validate_schedule(&p).unwrap_err().to_string();
        assert!(err.contains("Snapper-managed"), "{err}");
    }

    #[test]
    fn timer_description_cannot_be_injected_through_profile_name() {
        let mut p = profile();
        p.name = "safe name\nOnBootSec=1".into();

        let timer = render_timer(&p);

        assert!(!timer.contains("safe name\n"));
        assert!(timer.contains(&p.id.to_string()));
    }

    #[test]
    fn engine_policy_parser_accepts_public_values_and_rejects_unknown() {
        assert_eq!(parse_engine_policy("auto").unwrap(), EnginePolicy::Auto);
        assert_eq!(parse_engine_policy("system").unwrap(), EnginePolicy::System);
        assert_eq!(
            parse_engine_policy("bundled").unwrap(),
            EnginePolicy::Bundled
        );
        assert!(parse_engine_policy("magic").is_err());
    }

    #[test]
    fn changing_engine_policy_preserves_jobs_and_destinations() {
        let mut config = configuration();
        let original_profiles = config.profiles.clone();
        let original_destinations = config.destinations.clone();

        set_engine_policy_in_config(&mut config, EnginePolicy::Bundled);

        assert_eq!(config.engine_policy, EnginePolicy::Bundled);
        assert_eq!(config.profiles, original_profiles);
        assert_eq!(config.destinations, original_destinations);
    }

    #[test]
    fn engine_progress_message_reports_actual_origin_version_and_path() {
        let bundled = harbor_engine::EngineResolution {
            executable: PathBuf::from("/tmp/AppDir/portable/btrfs-backup-ng"),
            origin: harbor_engine::EngineOrigin::Bundled,
            version: Some("0.9.12".into()),
        };
        let system = harbor_engine::EngineResolution {
            executable: PathBuf::from("/usr/bin/btrfs-backup-ng"),
            origin: harbor_engine::EngineOrigin::System,
            version: Some("0.10.1".into()),
        };

        assert_eq!(
            engine_progress_message(&bundled),
            "Bundled AppImage engine · btrfs-backup-ng 0.9.12"
        );
        assert_eq!(
            engine_progress_message(&system),
            "System engine · btrfs-backup-ng 0.10.1 · /usr/bin/btrfs-backup-ng"
        );
    }

    #[test]
    fn engine_transfer_telemetry_is_forwarded_without_log_wrapping() {
        let line = r#"{"schema":1,"event":"transfer_progress","volume":"/home","bytes_target":2048,"bytes_per_second":1024.0}"#;
        let raw = engine_stream_line("stdout", "backup", line).unwrap();
        let value: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(value["event"], "transfer_progress");
        assert_eq!(value["schema"], 1);
        assert_eq!(value["volume"], "/home");
        assert_eq!(value["bytes_target"], 2048);
        assert!(value.get("message").is_none());
    }

    #[test]
    fn normal_engine_output_remains_a_harbor_output_event() {
        let raw =
            engine_stream_line("stdout", "backup", "Transfer completed successfully").unwrap();
        let value: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(value["event"], "output");
        assert_eq!(value["phase"], "backup");
        assert_eq!(value["message"], "Transfer completed successfully");
        assert_eq!(value["stream"], "stdout");
    }

    #[test]
    fn harbor_backup_command_explicitly_enables_machine_progress() {
        let mut command = Command::new("/usr/bin/btrfs-backup-ng");
        enable_engine_progress(&mut command);
        let enabled = command
            .get_envs()
            .find(|(name, _)| name.to_string_lossy() == "BTRFS_BACKUP_NG_PROGRESS_JSONL")
            .and_then(|(_, value)| value)
            .map(|value| value == "1")
            .unwrap_or(false);
        assert!(enabled);
    }

    #[test]
    fn progress_line_is_single_json_object_with_stream_metadata() {
        let raw = progress_line("output", "backup", "42 MiB", Some("stderr")).unwrap();
        assert!(!raw.contains('\n'));
        let value: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(value["event"], "output");
        assert_eq!(value["phase"], "backup");
        assert_eq!(value["message"], "42 MiB");
        assert_eq!(value["stream"], "stderr");
    }

    #[test]
    fn progress_line_escapes_newlines_in_messages() {
        let raw = progress_line(
            "failed",
            "error",
            "line one
line two",
            None,
        )
        .unwrap();
        assert!(!raw.contains(
            "line one
line two"
        ));
        let value: serde_json::Value = serde_json::from_str(&raw).unwrap();
        assert_eq!(
            value["message"],
            "line one
line two"
        );
    }

    struct FailingWriter {
        kind: std::io::ErrorKind,
    }

    impl std::io::Write for FailingWriter {
        fn write(&mut self, _buf: &[u8]) -> std::io::Result<usize> {
            Err(std::io::Error::from(self.kind))
        }

        fn flush(&mut self) -> std::io::Result<()> {
            Err(std::io::Error::from(self.kind))
        }
    }

    #[test]
    fn ui_broken_pipe_never_aborts_backup_event_forwarding() {
        let mut writer = FailingWriter {
            kind: std::io::ErrorKind::BrokenPipe,
        };
        assert!(write_jsonl_line(&mut writer, "{}").is_ok());
    }

    #[test]
    fn non_pipe_output_errors_still_fail_loudly() {
        let mut writer = FailingWriter {
            kind: std::io::ErrorKind::PermissionDenied,
        };
        let error = write_jsonl_line(&mut writer, "{}").unwrap_err();
        assert_eq!(
            error
                .downcast_ref::<std::io::Error>()
                .map(std::io::Error::kind),
            Some(std::io::ErrorKind::PermissionDenied)
        );
    }

    #[test]
    fn restore_command_defaults_to_latest_and_dry_run() {
        let plan = StagedRestorePlan {
            profile_id: Uuid::nil(),
            destination_id: Uuid::nil(),
            source_path: PathBuf::from("/home"),
            staging_path: PathBuf::from("/mnt/stage/profile/home"),
            target_index: 2,
            snapshot: None,
            before: None,
            requires_rescue_environment: false,
        };
        let command = build_restore_command(
            Path::new("/usr/bin/btrfs-backup-ng"),
            Path::new("/tmp/generated.toml"),
            &plan,
            true,
        );
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert_eq!(
            args,
            vec![
                "-c",
                "/tmp/generated.toml",
                "restore",
                "--volume",
                "/home",
                "--target",
                "2",
                "--to",
                "/mnt/stage/profile/home",
                "--no-progress",
                "--dry-run",
            ]
        );
    }

    #[test]
    fn restore_command_targets_exact_snapshot_by_name() {
        let plan = StagedRestorePlan {
            profile_id: Uuid::nil(),
            destination_id: Uuid::nil(),
            source_path: PathBuf::from("/home"),
            staging_path: PathBuf::from("/mnt/stage/home"),
            target_index: 0,
            snapshot: Some("home-20261006T020000".into()),
            before: None,
            requires_rescue_environment: false,
        };
        let command = build_restore_command(
            Path::new("/usr/bin/btrfs-backup-ng"),
            Path::new("/tmp/generated.toml"),
            &plan,
            false,
        );
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert!(args.windows(2).any(|pair| {
            pair == ["--snapshot".to_string(), "home-20261006T020000".to_string()]
        }));
        assert!(!args.iter().any(|arg| arg == "--before"));
    }

    #[test]
    fn raw_restore_points_keep_name_date_size_and_parent() {
        let raw = r#"[
          {
            "name": "home-20261006T020000",
            "created": "2026-10-06T02:00:00+00:00",
            "size": 1048576,
            "parent_name": "home-20261005T020000",
            "checksum": {"algorithm": "sha256", "value": "abc123"},
            "provenance": {"origin": "native"}
          }
        ]"#;
        let points = parse_restore_points(raw).unwrap();
        assert_eq!(points.len(), 1);
        assert_eq!(points[0].name, "home-20261006T020000");
        assert_eq!(
            points[0].created.as_deref(),
            Some("2026-10-06T02:00:00+00:00")
        );
        assert_eq!(points[0].size, Some(1_048_576));
        assert_eq!(
            points[0].parent_name.as_deref(),
            Some("home-20261005T020000")
        );
        assert_eq!(points[0].checksum.as_deref(), Some("abc123"));
    }

    #[test]
    fn restore_coverage_rejects_a_selected_source_with_no_restore_point() {
        let config = configuration();
        let profile = &config.profiles[0];
        let source = &profile.sources[0];
        let destination = &config.destinations[0];

        let err = require_restore_coverage(source, destination, &[])
            .unwrap_err()
            .to_string();

        assert!(err.contains("no restorable point"), "{err}");
        assert!(err.contains("/"), "{err}");
        assert!(err.contains("Backup NAS"), "{err}");
    }

    #[test]
    fn restore_coverage_accepts_a_finalized_restore_point() {
        let config = configuration();
        let profile = &config.profiles[0];
        let source = &profile.sources[0];
        let destination = &config.destinations[0];
        let points = vec![RestorePoint {
            name: "root-20261008T120000".into(),
            created: Some("2026-10-08T12:00:00+00:00".into()),
            size: Some(1024),
            parent_name: None,
            checksum: Some("abc".into()),
            origin: Some("native".into()),
        }];

        require_restore_coverage(source, destination, &points).unwrap();
    }

    #[test]
    fn restore_command_carries_point_in_time_as_one_argument() {
        let plan = StagedRestorePlan {
            profile_id: Uuid::nil(),
            destination_id: Uuid::nil(),
            source_path: PathBuf::from("/home"),
            staging_path: PathBuf::from("/mnt/stage/home"),
            target_index: 0,
            snapshot: None,
            before: Some("2026-10-05 02:00:00".into()),
            requires_rescue_environment: false,
        };
        let command = build_restore_command(
            Path::new("/usr/bin/btrfs-backup-ng"),
            Path::new("/tmp/generated.toml"),
            &plan,
            false,
        );
        let args = command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        assert!(
            args.windows(2).any(|pair| {
                pair == ["--before".to_string(), "2026-10-05 02:00:00".to_string()]
            })
        );
        assert!(!args.iter().any(|arg| arg == "--dry-run"));
    }

    #[test]
    fn timer_is_persistent_and_uses_profile_calendar() {
        let timer = render_timer(&profile());
        assert!(timer.contains("OnCalendar=*-*-* 02:00:00"));
        assert!(timer.contains("Persistent=true"));
        assert!(timer.contains("RandomizedDelaySec=5m"));
    }

    #[test]
    fn service_runs_exact_profile_as_root() {
        let p = profile();
        let service = render_service(p.id);
        assert!(service.contains(&format!("run-profile {}", p.id)));
        assert!(service.contains("User=root"));
        assert!(service.contains("UMask=0077"));
    }

    #[test]
    fn profile_unit_name_is_uuid_scoped() {
        assert_eq!(
            profile_unit_stem(profile().id),
            "btrfs-harbor-profile-11111111-1111-4111-8111-111111111111"
        );
    }
}
