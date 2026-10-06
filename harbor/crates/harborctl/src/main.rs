mod lxc_replica;
mod recovery_kit;

use anyhow::{Context, Result, bail};
use harbor_core::{
    BackupProfile, BackupSource, DestinationKind, DestinationSpec, profile_unit_stem,
};
use harbor_recovery::{
    StagedRestorePlan, StagedRestoreRequest, staged_restore_path,
    staging_is_separate_from_destination, staging_is_separate_from_live_source,
};
use harbor_storage::{
    HarborConfig, MountTable, engine_target_uri, render_engine_config, validate_destination_mount,
};
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
#[cfg(unix)]
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
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
        "run-profile-jsonl" => {
            let id = parse_profile_id(args.next())?;
            run_profile_jsonl(id)
        }
        "recovery-kit" => {
            let id = parse_profile_id(args.next())?;
            recovery_kit_profile(id)
        }
        "stage-restore-jsonl" => {
            let id = parse_profile_id(args.next())?;
            stage_restore_jsonl(id)
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
        "Usage: btrfs-harborctl <list|render-profile|apply-config|install-profile|uninstall-profile|run-profile|run-profile-jsonl|recovery-kit|stage-restore-jsonl|replicate-lxc-jsonl|status-profile> [PROFILE_UUID]"
    );
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

fn engine_executable() -> PathBuf {
    #[cfg(debug_assertions)]
    if let Some(value) = env::var_os("BTRFS_HARBOR_ENGINE") {
        return PathBuf::from(value);
    }

    harbor_engine::discover_engine(Some(PathBuf::from(DEFAULT_BUNDLED_ENGINE)))
        .map(|resolution| resolution.executable)
        .unwrap_or_else(|_| PathBuf::from(DEFAULT_BUNDLED_ENGINE))
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

    let status = Command::new(engine_executable())
        .arg("-c")
        .arg(&generated_path)
        .arg("run")
        .status()
        .context("failed to execute btrfs-backup-ng run")?;

    if !status.success() {
        bail!("btrfs-backup-ng run failed with {status}");
    }

    if profile.verify_after_backup {
        verify_profile_targets(&profile, &selected)?;
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

pub(crate) fn emit_progress(
    event: &str,
    phase: &str,
    message: &str,
    stream: Option<&str>,
) -> Result<()> {
    let mut stdout = std::io::stdout().lock();
    writeln!(stdout, "{}", progress_line(event, phase, message, stream)?)?;
    stdout.flush()?;
    Ok(())
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
        emit_progress("output", phase, &line, Some(stream))?;
    }

    let status = child.wait().context("cannot wait for backup subprocess")?;
    let _ = stdout_thread.join();
    let _ = stderr_thread.join();
    Ok(status)
}

fn run_profile_jsonl(id: Uuid) -> Result<()> {
    ensure_root()?;
    emit_progress("phase", "start", &format!("Starting profile {id}"), None)?;

    let result = (|| -> Result<()> {
        let (config, profile) = load_profile(id)?;
        let selected = validate_profile_destinations(&config, &profile)?;
        emit_progress(
            "phase",
            "prepare",
            &format!("Loaded {}", profile.name),
            None,
        )?;

        for destination in &selected {
            prepare_local_target_dirs(&profile, destination)?;
            emit_progress(
                "phase",
                "mount_guard",
                &format!("Destination {} is ready", destination.name),
                None,
            )?;
        }

        fs::create_dir_all(generated_dir())?;
        let generated = render_engine_config(&profile, &config.destinations)?;
        let generated_path = generated_config_path(id);
        atomic_write(&generated_path, generated.as_bytes())?;

        emit_progress("phase", "backup", "Running btrfs-backup-ng", None)?;
        let mut engine = Command::new(engine_executable());
        engine.arg("-c").arg(&generated_path).arg("run");
        let status = run_streaming_command(engine, "backup")?;
        if !status.success() {
            bail!("btrfs-backup-ng run failed with {status}");
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
                    let mut verify = Command::new(engine_executable());
                    verify.args(["raw", "verify", &uri, "--json"]);
                    let status = run_streaming_command(verify, "verify")?;
                    if !status.success() {
                        bail!("raw verification failed for {uri} with {status}");
                    }
                }
            }
        }

        emit_progress("phase", "recovery_kit", "Refreshing Recovery Kit", None)?;
        match refresh_recovery_kit(&config, &profile, &selected) {
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
    })();

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

    let stage_path = staged_restore_path(&staging_root, profile.id, &source.target_subdir);
    Ok((
        StagedRestorePlan {
            profile_id: profile.id,
            destination_id: destination.id,
            source_path: source.path.clone(),
            staging_path: stage_path,
            target_index,
            before: request
                .before
                .as_ref()
                .map(|value| value.trim().to_owned())
                .filter(|value| !value.is_empty()),
            requires_rescue_environment: source.path == Path::new("/"),
        },
        source,
        destination,
    ))
}

fn build_restore_command(
    generated_path: &Path,
    plan: &StagedRestorePlan,
    dry_run: bool,
) -> Command {
    let mut command = Command::new(engine_executable());
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
    if let Some(before) = &plan.before {
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
        let (plan, _source, destination) = staged_restore_plan(&config, &profile, &request)?;

        let mountinfo =
            fs::read_to_string("/proc/self/mountinfo").context("cannot read mount table")?;
        let mounts = MountTable::from_mountinfo(&mountinfo);
        validate_destination_mount(&destination, &mounts)
            .with_context(|| format!("destination {} failed mount guard", destination.name))?;

        fs::create_dir_all(generated_dir())?;
        let generated = render_engine_config(&profile, &config.destinations)?;
        let generated_path = generated_config_path(id);
        atomic_write(&generated_path, generated.as_bytes())?;
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
            build_restore_command(&generated_path, &plan, true),
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
            build_restore_command(&generated_path, &plan, false),
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
    })();

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

    let output = Command::new(engine_executable())
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

fn prepare_local_target_dirs(profile: &BackupProfile, destination: &DestinationSpec) -> Result<()> {
    if matches!(destination.kind, DestinationKind::Ssh) {
        return Ok(());
    }

    if !destination.path.exists() {
        bail!(
            "destination base {} does not exist; Harbor never creates a missing mount root",
            destination.path.display()
        );
    }

    for source in &profile.sources {
        let path = destination.path.join(&source.target_subdir);
        fs::create_dir_all(&path)
            .with_context(|| format!("cannot create backup target {}", path.display()))?;
    }
    Ok(())
}

fn verify_profile_targets(
    profile: &BackupProfile,
    destinations: &[&DestinationSpec],
) -> Result<()> {
    for destination in destinations {
        for source in &profile.sources {
            let uri = engine_target_uri(source, destination)?;
            let status = Command::new(engine_executable())
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

    fn configuration() -> HarborConfig {
        let destination_id = Uuid::parse_str("22222222-2222-4222-8222-222222222222").unwrap();
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

    #[test]
    fn restore_command_defaults_to_latest_and_dry_run() {
        let plan = StagedRestorePlan {
            profile_id: Uuid::nil(),
            destination_id: Uuid::nil(),
            source_path: PathBuf::from("/home"),
            staging_path: PathBuf::from("/mnt/stage/profile/home"),
            target_index: 2,
            before: None,
            requires_rescue_environment: false,
        };
        let command = build_restore_command(Path::new("/tmp/generated.toml"), &plan, true);
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
    fn restore_command_carries_point_in_time_as_one_argument() {
        let plan = StagedRestorePlan {
            profile_id: Uuid::nil(),
            destination_id: Uuid::nil(),
            source_path: PathBuf::from("/home"),
            staging_path: PathBuf::from("/mnt/stage/home"),
            target_index: 0,
            before: Some("2026-10-05 02:00:00".into()),
            requires_rescue_environment: false,
        };
        let command = build_restore_command(Path::new("/tmp/generated.toml"), &plan, false);
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
