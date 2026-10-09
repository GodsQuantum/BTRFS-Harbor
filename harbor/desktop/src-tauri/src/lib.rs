mod checkpoint_actions;
mod checkpoint_status;

use harbor_core::EnginePolicy;
use harbor_engine::{EngineOrigin, EngineSelectionStatus};
use harbor_storage::MountTable;
use serde::Serialize;
use serde_json::Value;
use std::ffi::{CString, OsStr};
use std::io::Write;
use std::os::fd::{AsRawFd, FromRawFd};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};
use tauri::{
    Manager, WindowEvent,
    ipc::Channel,
    menu::{Menu, MenuItem},
    tray::TrayIconBuilder,
};
use tauri_plugin_dialog::{DialogExt, MessageDialogKind};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use uuid::Uuid;

const BUS_NAME: &str = "io.github.GodsQuantum.BtrfsHarbor1";
const OBJECT_PATH: &str = "/io/github/GodsQuantum/BtrfsHarbor1";
const INTERFACE: &str = "io.github.GodsQuantum.BtrfsHarbor1";
const BACKUP_TRAY_ID: &str = "backup-running";
const BACKUP_TRAY_SHOW_ID: &str = "backup-show";
const BACKUP_TRAY_STOP_ID: &str = "backup-stop";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum BackupClosePolicy {
    CloseNormally,
    HideToTray,
    KeepWindowOpen,
}

#[derive(Debug, Clone, Copy)]
struct BackgroundBackupText {
    title: &'static str,
    no_tray_message: &'static str,
    show_label: &'static str,
    stop_label: &'static str,
    tooltip: &'static str,
    stopped: &'static str,
}

fn backup_close_policy(active: bool, tray_available: bool) -> BackupClosePolicy {
    match (active, tray_available) {
        (false, _) => BackupClosePolicy::CloseNormally,
        (true, true) => BackupClosePolicy::HideToTray,
        (true, false) => BackupClosePolicy::KeepWindowOpen,
    }
}

fn background_backup_text(locale: Option<&str>) -> BackgroundBackupText {
    let locale = locale.unwrap_or_default().to_ascii_lowercase();
    if locale.starts_with("fr") {
        BackgroundBackupText {
            title: "Sauvegarde en cours",
            no_tray_message: "Une sauvegarde est en cours. Le système de zone de notification n’est pas disponible, donc Btrfs Harbor doit rester ouvert jusqu’à la fin ou jusqu’à l’arrêt manuel de la sauvegarde.",
            show_label: "Afficher Btrfs Harbor",
            stop_label: "Arrêter la sauvegarde",
            tooltip: "Btrfs Harbor — sauvegarde en cours",
            stopped: "Sauvegarde arrêtée par l’utilisateur.",
        }
    } else if locale.starts_with("zh") {
        BackgroundBackupText {
            title: "备份正在进行",
            no_tray_message: "备份正在进行，但系统托盘不可用。请保持 Btrfs Harbor 打开，直到备份完成或手动停止。",
            show_label: "显示 Btrfs Harbor",
            stop_label: "停止备份",
            tooltip: "Btrfs Harbor — 正在备份",
            stopped: "用户已停止备份。",
        }
    } else {
        BackgroundBackupText {
            title: "Backup in progress",
            no_tray_message: "A backup is in progress, but the system tray is unavailable. Keep Btrfs Harbor open until the backup finishes or stop it manually.",
            show_label: "Show Btrfs Harbor",
            stop_label: "Stop backup",
            tooltip: "Btrfs Harbor — backup in progress",
            stopped: "Backup stopped by the user.",
        }
    }
}

fn process_group_target(pid: u32) -> String {
    format!("-{pid}")
}

fn backup_stop_signal() -> &'static str {
    "-INT"
}

#[derive(Default)]
struct BackupRuntimeState {
    active: AtomicBool,
    tray_available: AtomicBool,
    hidden_for_backup: AtomicBool,
    stop_requested: AtomicBool,
    process_group: Mutex<Option<u32>>,
}

impl BackupRuntimeState {
    fn begin(&self) -> Result<(), String> {
        self.active
            .compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst)
            .map_err(|_| "A Btrfs Harbor backup is already running.".to_string())?;
        self.stop_requested.store(false, Ordering::SeqCst);
        self.hidden_for_backup.store(false, Ordering::SeqCst);
        *self
            .process_group
            .lock()
            .expect("backup process mutex poisoned") = None;
        Ok(())
    }

    fn set_process_group(&self, pid: u32) {
        *self
            .process_group
            .lock()
            .expect("backup process mutex poisoned") = Some(pid);
    }

    fn process_group(&self) -> Option<u32> {
        *self
            .process_group
            .lock()
            .expect("backup process mutex poisoned")
    }

    fn finish(&self) {
        *self
            .process_group
            .lock()
            .expect("backup process mutex poisoned") = None;
        self.active.store(false, Ordering::SeqCst);
        self.tray_available.store(false, Ordering::SeqCst);
        self.stop_requested.store(false, Ordering::SeqCst);
    }
}

fn current_background_backup_text() -> BackgroundBackupText {
    let locale = std::env::var("LC_ALL")
        .ok()
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var("LANG").ok());
    background_backup_text(locale.as_deref())
}

fn show_main_window(app: &tauri::AppHandle) {
    let state = app.state::<BackupRuntimeState>();
    state.hidden_for_backup.store(false, Ordering::SeqCst);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

async fn stop_active_backup_inner(app: &tauri::AppHandle) -> Result<String, String> {
    let state = app.state::<BackupRuntimeState>();
    let Some(pid) = state.process_group() else {
        return Ok("no-active-backup".to_string());
    };
    state.stop_requested.store(true, Ordering::SeqCst);
    show_main_window(app);

    let target = process_group_target(pid);
    let output = Command::new("/usr/bin/pkexec")
        .arg("/usr/bin/kill")
        .args([backup_stop_signal(), "--", target.as_str()])
        .output()
        .await
        .map_err(|err| format!("Cannot stop the active backup: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Cannot stop the active backup: {}", output.status)
        } else {
            stderr
        });
    }
    Ok("stop-requested".to_string())
}

#[tauri::command]
async fn stop_active_backup(app: tauri::AppHandle) -> Result<String, String> {
    stop_active_backup_inner(&app).await
}

fn ensure_backup_tray(app: &tauri::AppHandle) -> Result<(), String> {
    if app.tray_by_id(BACKUP_TRAY_ID).is_some() {
        app.state::<BackupRuntimeState>()
            .tray_available
            .store(true, Ordering::SeqCst);
        return Ok(());
    }

    let text = current_background_backup_text();
    let show = MenuItem::with_id(
        app,
        BACKUP_TRAY_SHOW_ID,
        text.show_label,
        true,
        None::<&str>,
    )
    .map_err(|err| format!("Cannot create Harbor tray menu: {err}"))?;
    let stop = MenuItem::with_id(
        app,
        BACKUP_TRAY_STOP_ID,
        text.stop_label,
        true,
        None::<&str>,
    )
    .map_err(|err| format!("Cannot create Harbor tray menu: {err}"))?;
    let menu = Menu::with_items(app, &[&show, &stop])
        .map_err(|err| format!("Cannot create Harbor tray menu: {err}"))?;
    let icon = app
        .default_window_icon()
        .cloned()
        .ok_or_else(|| "Btrfs Harbor has no default tray icon.".to_string())?;

    TrayIconBuilder::with_id(BACKUP_TRAY_ID)
        .icon(icon)
        .tooltip(text.tooltip)
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            BACKUP_TRAY_SHOW_ID => show_main_window(app),
            BACKUP_TRAY_STOP_ID => {
                let app = app.clone();
                tauri::async_runtime::spawn(async move {
                    let _ = stop_active_backup_inner(&app).await;
                });
            }
            _ => {}
        })
        .build(app)
        .map_err(|err| format!("Cannot create Harbor system tray icon: {err}"))?;

    app.state::<BackupRuntimeState>()
        .tray_available
        .store(true, Ordering::SeqCst);
    Ok(())
}

fn remove_backup_tray(app: &tauri::AppHandle) {
    let _ = app.remove_tray_by_id(BACKUP_TRAY_ID);
    app.state::<BackupRuntimeState>()
        .tray_available
        .store(false, Ordering::SeqCst);
}

fn finish_background_backup(app: &tauri::AppHandle, success: bool) {
    let state = app.state::<BackupRuntimeState>();
    let was_hidden = state.hidden_for_backup.swap(false, Ordering::SeqCst);
    state.finish();
    remove_backup_tray(app);
    if was_hidden {
        if success {
            app.exit(0);
        } else {
            show_main_window(app);
        }
    }
}

fn appimage_extract_reexec_required(
    appimage: Option<&OsStr>,
    extract_and_run: Option<&OsStr>,
) -> bool {
    appimage.is_some() && extract_and_run.is_none()
}

pub fn reexec_appimage_extract_and_run_if_needed() -> Result<(), String> {
    #[cfg(target_os = "linux")]
    {
        let appimage = std::env::var_os("APPIMAGE");
        let extract_and_run = std::env::var_os("APPIMAGE_EXTRACT_AND_RUN");
        if appimage_extract_reexec_required(appimage.as_deref(), extract_and_run.as_deref()) {
            use std::os::unix::process::CommandExt;
            let image = appimage.expect("APPIMAGE was checked above");
            let error = std::process::Command::new(image)
                .args(std::env::args_os().skip(1))
                .env("APPIMAGE_EXTRACT_AND_RUN", "1")
                .exec();
            return Err(format!(
                "Cannot restart Btrfs Harbor in AppImage extract-and-run mode: {error}"
            ));
        }
    }
    Ok(())
}

fn helper_failure_message(status: &std::process::ExitStatus, stderr: &[String]) -> String {
    let detail = stderr
        .iter()
        .map(|line| line.trim())
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    if detail.is_empty() {
        format!("Harbor helper failed with {status}")
    } else {
        detail
    }
}

#[cfg_attr(not(test), allow(dead_code))]
fn bundled_helper_path(resource_dir: &Path) -> PathBuf {
    resource_dir.join("portable").join("btrfs-harborctl")
}

fn choose_helper_candidate(
    installed: Option<PathBuf>,
    bundled: Option<PathBuf>,
) -> Result<PathBuf, String> {
    installed
        .or(bundled)
        .ok_or_else(|| "No Harbor helper is available for this operation.".to_string())
}

fn helper_executable(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    let installed = PathBuf::from("/usr/bin/btrfs-harborctl");
    let installed = installed.is_file().then_some(installed);
    let bundled = app
        .path()
        .resource_dir()
        .ok()
        .map(|root| bundled_helper_path(&root))
        .filter(|path| path.is_file());

    choose_helper_candidate(installed, bundled)
}

fn bundled_engine_candidate(app: &tauri::AppHandle) -> Option<PathBuf> {
    app.path()
        .resource_dir()
        .ok()
        .map(|root| root.join("portable").join("btrfs-backup-ng"))
        .filter(|path| path.is_file())
        .or_else(|| {
            let installed = PathBuf::from("/usr/lib/btrfs-harbor/btrfs-backup-ng");
            installed.is_file().then_some(installed)
        })
}

fn parse_engine_policy_value(value: &str) -> Result<EnginePolicy, String> {
    match value.trim().to_ascii_lowercase().as_str() {
        "auto" => Ok(EnginePolicy::Auto),
        "system" => Ok(EnginePolicy::System),
        "bundled" => Ok(EnginePolicy::Bundled),
        other => Err(format!("Unknown backup engine policy: {other}")),
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum EngineProvenance {
    Pacman { package: String },
    Dpkg { package: String },
    Rpm { package: String },
    UvTool,
    Pipx,
    ManualUnknown,
    BundledHarbor,
}

impl EngineProvenance {
    fn kind(&self) -> &'static str {
        match self {
            Self::Pacman { .. } => "pacman",
            Self::Dpkg { .. } => "dpkg",
            Self::Rpm { .. } => "rpm",
            Self::UvTool => "uv_tool",
            Self::Pipx => "pipx",
            Self::ManualUnknown => "manual_unknown",
            Self::BundledHarbor => "bundled_harbor",
        }
    }

    fn package(&self) -> Option<&str> {
        match self {
            Self::Pacman { package } | Self::Dpkg { package } | Self::Rpm { package } => {
                Some(package)
            }
            _ => None,
        }
    }
}

#[derive(Debug, Clone, Copy, Serialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum EngineUpdateStrategy {
    HarborApplication,
    PackageManager,
    UserTool,
    GuidanceOnly,
}

#[derive(Debug, Clone, Serialize)]
struct EngineUpdateOptions {
    strategy: EngineUpdateStrategy,
    can_update: bool,
    package: Option<String>,
    provenance: String,
    reason: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct UpdateCommandSpec {
    program: String,
    args: Vec<String>,
    privileged: bool,
}

fn safe_package_name(package: &str) -> bool {
    !package.is_empty()
        && !package.starts_with('-')
        && package
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | '+' | '@' | ':'))
}

fn update_command_for_provenance(
    provenance: &EngineProvenance,
    dnf_available: bool,
    zypper_available: bool,
) -> Option<UpdateCommandSpec> {
    let package_spec = |package: &str, program: &str, mut args: Vec<String>| {
        if !safe_package_name(package) {
            return None;
        }
        args.push(package.to_owned());
        Some(UpdateCommandSpec {
            program: program.to_owned(),
            args,
            privileged: true,
        })
    };

    match provenance {
        EngineProvenance::Pacman { package } => package_spec(
            package,
            "/usr/bin/pacman",
            vec!["-S".into(), "--needed".into(), "--noconfirm".into()],
        ),
        EngineProvenance::Dpkg { package } => package_spec(
            package,
            "/usr/bin/apt-get",
            vec!["install".into(), "--only-upgrade".into(), "-y".into()],
        ),
        EngineProvenance::Rpm { package } if dnf_available => {
            package_spec(package, "/usr/bin/dnf", vec!["upgrade".into(), "-y".into()])
        }
        EngineProvenance::Rpm { package } if zypper_available => package_spec(
            package,
            "/usr/bin/zypper",
            vec!["--non-interactive".into(), "update".into()],
        ),
        EngineProvenance::UvTool => Some(UpdateCommandSpec {
            program: "uv".into(),
            args: vec!["tool".into(), "upgrade".into(), "btrfs-backup-ng".into()],
            privileged: false,
        }),
        EngineProvenance::Pipx => Some(UpdateCommandSpec {
            program: "pipx".into(),
            args: vec!["upgrade".into(), "btrfs-backup-ng".into()],
            privileged: false,
        }),
        _ => None,
    }
}

fn parse_pacman_owner(output: &str) -> Option<String> {
    let owner = output
        .split_once(" is owned by ")
        .map(|(_, owner)| owner)
        .unwrap_or(output)
        .trim();
    owner
        .split_whitespace()
        .next()
        .filter(|package| !package.is_empty())
        .map(ToOwned::to_owned)
}

fn parse_dpkg_owner(output: &str) -> Option<String> {
    output
        .lines()
        .find_map(|line| {
            line.split_once(':')
                .map(|(package, _)| package.trim().to_owned())
        })
        .filter(|package| !package.is_empty())
}

fn parse_rpm_owner(output: &str) -> Option<String> {
    output
        .lines()
        .map(str::trim)
        .find(|line| !line.is_empty() && !line.starts_with("file "))
        .map(ToOwned::to_owned)
}

fn classify_engine_provenance(
    path: &Path,
    pacman_output: Option<&str>,
    dpkg_output: Option<&str>,
    rpm_output: Option<&str>,
) -> EngineProvenance {
    if let Some(package) = pacman_output.and_then(parse_pacman_owner) {
        return EngineProvenance::Pacman { package };
    }
    if let Some(package) = dpkg_output.and_then(parse_dpkg_owner) {
        return EngineProvenance::Dpkg { package };
    }
    if let Some(package) = rpm_output.and_then(parse_rpm_owner) {
        return EngineProvenance::Rpm { package };
    }

    let normalized = path.to_string_lossy();
    if normalized.contains("/.local/share/uv/tools/") || normalized.contains("/uv/tools/") {
        EngineProvenance::UvTool
    } else if normalized.contains("/pipx/venvs/") || normalized.contains("/.local/pipx/") {
        EngineProvenance::Pipx
    } else {
        EngineProvenance::ManualUnknown
    }
}

fn update_strategy_for_origin(
    origin: EngineOrigin,
    provenance: EngineProvenance,
) -> EngineUpdateStrategy {
    match origin {
        EngineOrigin::Bundled => EngineUpdateStrategy::HarborApplication,
        EngineOrigin::System => match provenance {
            EngineProvenance::Pacman { .. }
            | EngineProvenance::Dpkg { .. }
            | EngineProvenance::Rpm { .. } => EngineUpdateStrategy::PackageManager,
            EngineProvenance::UvTool | EngineProvenance::Pipx => EngineUpdateStrategy::UserTool,
            EngineProvenance::ManualUnknown | EngineProvenance::BundledHarbor => {
                EngineUpdateStrategy::GuidanceOnly
            }
        },
    }
}

async fn bounded_command_output(program: &str, args: &[&str]) -> Option<String> {
    let mut command = Command::new(program);
    command.args(args);
    let result = tokio::time::timeout(std::time::Duration::from_secs(2), command.output()).await;
    match result {
        Ok(Ok(output)) if output.status.success() => {
            let stdout = String::from_utf8_lossy(&output.stdout).trim().to_owned();
            (!stdout.is_empty()).then_some(stdout)
        }
        _ => None,
    }
}

async fn detect_engine_provenance(path: &Path) -> EngineProvenance {
    if path == Path::new("/usr/lib/btrfs-harbor/btrfs-backup-ng")
        || path.to_string_lossy().contains("/portable/btrfs-backup-ng")
    {
        return EngineProvenance::BundledHarbor;
    }

    let path_text = path.to_string_lossy().into_owned();
    let pacman = bounded_command_output("pacman", &["-Qo", "--", &path_text]).await;
    let dpkg = bounded_command_output("dpkg-query", &["-S", &path_text]).await;
    let rpm = bounded_command_output("rpm", &["-qf", "--qf", "%{NAME}\n", &path_text]).await;
    classify_engine_provenance(path, pacman.as_deref(), dpkg.as_deref(), rpm.as_deref())
}

fn engine_update_options_from(
    selection: &EngineSelectionStatus,
    provenance: EngineProvenance,
) -> EngineUpdateOptions {
    let strategy = update_strategy_for_origin(selection.active.origin, provenance.clone());
    let package = provenance.package().map(ToOwned::to_owned);
    let (can_update, reason) = match (&selection.active.origin, &provenance, strategy) {
        (EngineOrigin::Bundled, _, EngineUpdateStrategy::HarborApplication) => (
            true,
            "The bundled engine is updated by updating Btrfs Harbor itself.".to_owned(),
        ),
        (EngineOrigin::System, EngineProvenance::Pacman { .. }, _) => (
            true,
            "This system engine is owned by pacman; Harbor can request a package upgrade and then re-check compatibility.".to_owned(),
        ),
        (EngineOrigin::System, EngineProvenance::Dpkg { .. }, _) => (
            true,
            "This system engine is owned by dpkg; Harbor can request an apt package upgrade and then re-check compatibility.".to_owned(),
        ),
        (EngineOrigin::System, EngineProvenance::Rpm { .. }, _) => (
            true,
            "This system engine is owned by RPM; Harbor can use the available RPM-family package manager and then re-check compatibility.".to_owned(),
        ),
        (EngineOrigin::System, EngineProvenance::UvTool, _) => (
            true,
            "This system engine is managed by uv tool and can be upgraded without overwriting distribution files.".to_owned(),
        ),
        (EngineOrigin::System, EngineProvenance::Pipx, _) => (
            true,
            "This system engine is managed by pipx and can be upgraded without overwriting distribution files.".to_owned(),
        ),
        _ => (
            false,
            "Harbor cannot prove who owns this engine, so it will not overwrite it. Use the bundled engine or update the manual installation yourself.".to_owned(),
        ),
    };

    EngineUpdateOptions {
        strategy,
        can_update,
        package,
        provenance: provenance.kind().to_owned(),
        reason,
    }
}

async fn engine_selection_for_app(
    app: &tauri::AppHandle,
    policy: &str,
) -> Result<EngineSelectionStatus, String> {
    let policy = parse_engine_policy_value(policy)?;
    harbor_engine::discover_engine_with_policy(policy, bundled_engine_candidate(app))
        .map_err(|err| err.to_string())
}

#[tauri::command]
async fn engine_status(app: tauri::AppHandle, policy: String) -> Result<String, String> {
    let status = engine_selection_for_app(&app, &policy).await?;
    serde_json::to_string(&status).map_err(|err| format!("Cannot encode engine status: {err}"))
}

#[tauri::command]
async fn engine_update_options(app: tauri::AppHandle, policy: String) -> Result<String, String> {
    let status = engine_selection_for_app(&app, &policy).await?;
    let provenance = if status.active.origin == EngineOrigin::Bundled {
        EngineProvenance::BundledHarbor
    } else {
        detect_engine_provenance(&status.active.executable).await
    };
    let options = engine_update_options_from(&status, provenance);
    serde_json::to_string(&options)
        .map_err(|err| format!("Cannot encode engine update options: {err}"))
}

#[tauri::command]
async fn set_engine_policy(app: tauri::AppHandle, policy: String) -> Result<String, String> {
    let parsed = parse_engine_policy_value(&policy)?;
    if !Path::new("/etc/btrfs-harbor/harbor.toml").is_file() {
        return serde_json::to_string(&parsed)
            .map_err(|err| format!("Cannot encode engine policy: {err}"));
    }

    let helper = helper_executable(&app)?;
    let output = Command::new("/usr/bin/pkexec")
        .arg(helper)
        .arg("set-engine-policy")
        .arg(policy)
        .output()
        .await
        .map_err(|err| format!("Cannot persist the backup engine policy: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Harbor helper failed with {}", output.status)
        } else {
            stderr
        });
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

#[tauri::command]
async fn update_system_engine(app: tauri::AppHandle, policy: String) -> Result<String, String> {
    let selection = engine_selection_for_app(&app, &policy).await?;
    if selection.active.origin != EngineOrigin::System {
        return Err(
            "The active engine is bundled with Harbor. Update Btrfs Harbor to update it.".into(),
        );
    }

    let provenance = detect_engine_provenance(&selection.active.executable).await;
    let spec = update_command_for_provenance(
        &provenance,
        Path::new("/usr/bin/dnf").is_file(),
        Path::new("/usr/bin/zypper").is_file(),
    )
    .ok_or_else(|| {
        "Harbor cannot safely update this engine because its local package owner is unknown."
            .to_string()
    })?;

    let output = if spec.privileged {
        let mut command = Command::new("/usr/bin/pkexec");
        command.arg(&spec.program).args(&spec.args);
        command.output().await
    } else {
        Command::new(&spec.program).args(&spec.args).output().await
    }
    .map_err(|err| format!("Cannot start the engine update: {err}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Engine update failed with {}", output.status)
        } else {
            stderr
        });
    }

    let refreshed = engine_selection_for_app(&app, &policy).await?;
    serde_json::to_string(&refreshed)
        .map_err(|err| format!("Cannot encode refreshed engine status: {err}"))
}

#[tauri::command]
async fn open_harbor_release_page() -> Result<String, String> {
    let status = Command::new("xdg-open")
        .arg("https://github.com/GodsQuantum/BTRFS-Harbor/releases/latest")
        .status()
        .await
        .map_err(|err| format!("Cannot open Harbor releases: {err}"))?;
    if status.success() {
        Ok("opened".to_owned())
    } else {
        Err(format!("Cannot open Harbor releases: {status}"))
    }
}

async fn call_agent<R>(
    method: &str,
    body: &(impl serde::ser::Serialize + zbus::zvariant::DynamicType),
) -> Result<R, String>
where
    R: for<'de> serde::Deserialize<'de> + zbus::zvariant::Type,
{
    let connection = zbus::Connection::system()
        .await
        .map_err(|err| format!("Cannot connect to the system D-Bus: {err}"))?;
    let proxy = zbus::Proxy::new(&connection, BUS_NAME, OBJECT_PATH, INTERFACE)
        .await
        .map_err(|err| format!("Cannot connect to Btrfs Harbor agent: {err}"))?;
    proxy
        .call(method, body)
        .await
        .map_err(|err| format!("Btrfs Harbor agent call {method} failed: {err}"))
}

#[tauri::command]
async fn agent_version() -> Result<String, String> {
    call_agent("Version", &()).await
}

#[tauri::command]
async fn system_summary() -> Result<String, String> {
    call_agent("SystemSummary", &()).await
}

#[derive(Debug, Serialize)]
struct InstallationState {
    portable_appimage: bool,
    helper_installed: bool,
    service_unit_installed: bool,
    service_available: bool,
}

#[tauri::command]
async fn installation_state() -> Result<String, String> {
    let state = InstallationState {
        portable_appimage: std::env::var_os("APPIMAGE").is_some(),
        helper_installed: Path::new("/usr/bin/btrfs-harborctl").is_file(),
        service_unit_installed: Path::new("/usr/lib/systemd/system/btrfs-harbor-agent.service")
            .is_file()
            || Path::new("/lib/systemd/system/btrfs-harbor-agent.service").is_file(),
        service_available: agent_version().await.is_ok(),
    };

    serde_json::to_string(&state).map_err(|err| format!("Cannot encode installation state: {err}"))
}

async fn download_file(url: &str, destination: &Path) -> Result<(), String> {
    let curl = Command::new("curl")
        .args(["-fL", "--retry", "2", "--connect-timeout", "15", "-o"])
        .arg(destination)
        .arg(url)
        .output()
        .await;

    if let Ok(output) = curl
        && output.status.success()
    {
        return Ok(());
    }

    let wget = Command::new("wget")
        .arg("-q")
        .arg("-O")
        .arg(destination)
        .arg(url)
        .output()
        .await;

    match wget {
        Ok(output) if output.status.success() => Ok(()),
        _ => Err(
            "Harbor could not download the full installer. Install curl or wget, or download the .run package from the GitHub release."
                .into(),
        ),
    }
}

#[tauri::command]
async fn install_full_package() -> Result<String, String> {
    if std::env::consts::ARCH != "x86_64" {
        return Err("The automatic installer currently supports x86_64 Linux only.".into());
    }

    if Path::new("/usr/bin/btrfs-harborctl").is_file() {
        return Ok("already-installed".into());
    }

    let version = env!("CARGO_PKG_VERSION");
    let file_name = format!("BTRFS-Harbor-{version}-linux-x86_64.run");
    let base = format!("https://github.com/GodsQuantum/BTRFS-Harbor/releases/download/v{version}");
    let temp_dir =
        std::env::temp_dir().join(format!("btrfs-harbor-install-{}", std::process::id()));
    tokio::fs::create_dir_all(&temp_dir)
        .await
        .map_err(|err| format!("Cannot create temporary installer directory: {err}"))?;

    let installer = temp_dir.join(&file_name);
    let checksums = temp_dir.join("SHA256SUMS");

    let result = async {
        download_file(&format!("{base}/{file_name}"), &installer).await?;
        download_file(&format!("{base}/SHA256SUMS"), &checksums).await?;

        let expected = tokio::fs::read_to_string(&checksums)
            .await
            .map_err(|err| format!("Cannot read Harbor release checksums: {err}"))?
            .lines()
            .find_map(|line| {
                let mut parts = line.split_whitespace();
                let hash = parts.next()?;
                let name = parts.next()?.trim_start_matches('*');
                (name == file_name).then(|| hash.to_ascii_lowercase())
            })
            .ok_or_else(|| {
                "The Harbor release checksum file does not list the installer.".to_string()
            })?;

        let digest = Command::new("sha256sum")
            .arg(&installer)
            .output()
            .await
            .map_err(|err| format!("Cannot verify the downloaded Harbor installer: {err}"))?;
        if !digest.status.success() {
            return Err("Harbor could not verify the downloaded installer.".into());
        }
        let actual = String::from_utf8_lossy(&digest.stdout)
            .split_whitespace()
            .next()
            .unwrap_or_default()
            .to_ascii_lowercase();
        if actual != expected {
            return Err("The downloaded Harbor installer failed SHA-256 verification.".into());
        }

        let mut permissions = tokio::fs::metadata(&installer)
            .await
            .map_err(|err| format!("Cannot inspect downloaded installer: {err}"))?
            .permissions();
        permissions.set_mode(0o755);
        tokio::fs::set_permissions(&installer, permissions)
            .await
            .map_err(|err| format!("Cannot make downloaded installer executable: {err}"))?;

        let output = Command::new("/usr/bin/pkexec")
            .arg(&installer)
            .output()
            .await
            .map_err(|err| format!("Cannot start the Harbor installer: {err}"))?;

        if !output.status.success() {
            let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
            return Err(if stderr.is_empty() {
                "Harbor installation was cancelled or failed.".into()
            } else {
                stderr
            });
        }

        Ok("installed".to_string())
    }
    .await;

    let _ = tokio::fs::remove_dir_all(&temp_dir).await;
    result
}

const MAX_PORTABLE_CONFIG_BYTES: u64 = 1024 * 1024;

fn validate_portable_configuration(input: &str) -> Result<harbor_storage::HarborConfig, String> {
    if input.len() as u64 > MAX_PORTABLE_CONFIG_BYTES {
        return Err("Portable backup configuration is too large".into());
    }
    let config: harbor_storage::HarborConfig =
        serde_json::from_str(input).map_err(|e| format!("Invalid backup configuration: {e}"))?;
    config
        .validate()
        .map_err(|e| format!("Invalid backup configuration: {e}"))?;
    for destination in &config.destinations {
        if destination.path == Path::new("/")
            || destination.path.to_string_lossy().starts_with("//")
        {
            return Err("Choose an actual backup directory, not '/' or '//root'".into());
        }
    }
    Ok(config)
}

fn portable_config_file(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_config_dir()
        .map(|p| p.join("portable-profiles.json"))
        .map_err(|e| format!("Cannot resolve Harbor portable configuration location: {e}"))
}

fn read_portable_file(path: &Path) -> Result<Option<String>, String> {
    let metadata = match std::fs::symlink_metadata(path) {
        Ok(m) => m,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(format!("Cannot inspect saved portable configuration: {e}")),
    };
    if !metadata.file_type().is_file() || metadata.len() > MAX_PORTABLE_CONFIG_BYTES {
        return Err("Portable configuration is not a regular file of acceptable size".into());
    }
    let input = std::fs::read_to_string(path)
        .map_err(|e| format!("Cannot read saved portable configuration: {e}"))?;
    validate_portable_configuration(&input)?;
    Ok(Some(input))
}

fn save_portable_file(path: &Path, input: &str) -> Result<(), String> {
    validate_portable_configuration(input)?;
    let parent = path.parent().ok_or("Invalid portable configuration path")?;
    std::fs::create_dir_all(parent)
        .map_err(|e| format!("Cannot create portable configuration directory: {e}"))?;
    if parent
        .symlink_metadata()
        .is_ok_and(|m| m.file_type().is_symlink())
    {
        return Err("Refusing symlinked portable configuration directory".into());
    }
    if path
        .symlink_metadata()
        .is_ok_and(|m| m.file_type().is_symlink())
    {
        return Err("Refusing symlinked portable configuration file".into());
    }
    let temporary = parent.join(format!(".portable-profiles-{}.tmp", Uuid::new_v4()));
    let result = (|| -> Result<(), String> {
        let mut f = std::fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)
            .map_err(|e| format!("Cannot create portable configuration draft: {e}"))?;
        f.write_all(input.as_bytes())
            .and_then(|()| f.sync_all())
            .map_err(|e| format!("Cannot write portable configuration: {e}"))?;
        std::fs::rename(&temporary, path)
            .map_err(|e| format!("Cannot publish portable configuration: {e}"))?;
        std::fs::File::open(parent)
            .and_then(|d| d.sync_all())
            .map_err(|e| format!("Cannot sync portable configuration directory: {e}"))?;
        Ok(())
    })();
    if result.is_err() {
        let _ = std::fs::remove_file(temporary);
    }
    result
}

#[tauri::command]
async fn portable_configuration(app: tauri::AppHandle) -> Result<Option<String>, String> {
    let path = portable_config_file(&app)?;
    read_portable_file(&path)
}

#[tauri::command]
async fn save_portable_configuration(
    app: tauri::AppHandle,
    configuration: String,
) -> Result<String, String> {
    let path = portable_config_file(&app)?;
    save_portable_file(&path, &configuration)?;
    Ok("saved".into())
}

#[tauri::command]
async fn profiles() -> Result<String, String> {
    call_agent("Profiles", &()).await
}

#[tauri::command]
async fn configuration() -> Result<String, String> {
    call_agent("Configuration", &()).await
}

#[derive(Debug, Serialize)]
struct SystemIdentity {
    hostname: String,
    pretty_name: String,
    architecture: String,
    root_fs: String,
}

#[derive(Debug, Serialize)]
struct MountProbe {
    mount_point: String,
    source: String,
    fs_type: String,
    kind: &'static str,
}

fn parse_os_pretty_name(raw: &str) -> String {
    raw.lines()
        .find_map(|line| line.strip_prefix("PRETTY_NAME="))
        .map(|value| value.trim_matches('"').to_string())
        .unwrap_or_else(|| "Linux".to_string())
}

#[tauri::command]
async fn system_identity() -> Result<String, String> {
    let hostname = tokio::fs::read_to_string("/etc/hostname")
        .await
        .unwrap_or_else(|_| "Linux computer".into())
        .trim()
        .to_string();
    let os_release = tokio::fs::read_to_string("/etc/os-release")
        .await
        .unwrap_or_default();
    let mountinfo = tokio::fs::read_to_string("/proc/self/mountinfo")
        .await
        .map_err(|err| format!("Cannot read mount table: {err}"))?;
    let table = MountTable::from_mountinfo(&mountinfo);
    let root_fs = table
        .find_for_path(Path::new("/"))
        .map(|entry| entry.fs_type.clone())
        .unwrap_or_else(|| "unknown".into());

    serde_json::to_string(&SystemIdentity {
        hostname,
        pretty_name: parse_os_pretty_name(&os_release),
        architecture: std::env::consts::ARCH.to_string(),
        root_fs,
    })
    .map_err(|err| format!("Cannot encode system identity: {err}"))
}

#[tauri::command]
async fn inspect_mount(path: String) -> Result<String, String> {
    let path = PathBuf::from(path);
    if !path.is_absolute() {
        return Err("Destination path must be absolute.".into());
    }

    let mountinfo = tokio::fs::read_to_string("/proc/self/mountinfo")
        .await
        .map_err(|err| format!("Cannot read mount table: {err}"))?;
    let table = MountTable::from_mountinfo(&mountinfo);
    let entry = table
        .find_for_path(&path)
        .ok_or_else(|| format!("No active mount contains {}", path.display()))?;

    let kind = match entry.fs_type.as_str() {
        "nfs" | "nfs4" => "nfs",
        "cifs" => "smb",
        _ => "raw",
    };

    serde_json::to_string(&MountProbe {
        mount_point: entry.mount_point.to_string_lossy().into_owned(),
        source: entry.source.clone(),
        fs_type: entry.fs_type.clone(),
        kind,
    })
    .map_err(|err| format!("Cannot encode mount probe: {err}"))
}

fn create_checkpoint_subdir_by_fd(root: &Path, name: &str) -> Result<(), String> {
    // All writes are relative to an already-open directory FD, so a removable
    // drive or NAS mount disappearing mid-mkdir cannot redirect into the
    // underlying host mountpoint path.
    let fd = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(root)
        .map_err(|e| format!("Cannot open selected destination: {e}"))?;
    let child = CString::new(name).map_err(|_| "Invalid checkpoint subdirectory")?;
    // SAFETY: fd is a valid opened directory, name has been validated as one
    // simple non-NUL component and CString stays alive for both calls.
    let created = unsafe { libc::mkdirat(fd.as_raw_fd(), child.as_ptr(), 0o700) };
    if created != 0 {
        let error = std::io::Error::last_os_error();
        if error.kind() != std::io::ErrorKind::AlreadyExists {
            return Err(format!("Cannot prepare backup folder: {error}"));
        }
    }
    // SAFETY: openat confines this lookup to fd's filesystem and refuses
    // symlinks; O_DIRECTORY requires a directory, not an existing file.
    let opened = unsafe {
        libc::openat(
            fd.as_raw_fd(),
            child.as_ptr(),
            libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
        )
    };
    if opened < 0 {
        return Err(format!(
            "Backup folder is not a real directory: {}",
            std::io::Error::last_os_error()
        ));
    }
    // SAFETY: opened was returned as an owned fd by openat above.
    let child_fd = unsafe { std::os::fd::OwnedFd::from_raw_fd(opened) };
    drop(child_fd);
    Ok(())
}

/// Prepare a single user-selected directory, never a mount or an arbitrary
/// path beneath it. Mount identity is checked on both sides of mkdir.
#[tauri::command]
async fn prepare_checkpoint_directory(
    root: String,
    subdir: String,
    expected_mount_point: String,
    expected_mount_source: String,
) -> Result<String, String> {
    let base = Path::new(&root);
    if root.starts_with("//")
        || root.contains('\0')
        || root == "/"
        || !base.is_absolute()
        || subdir.is_empty()
        || !subdir
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
        || subdir.len() > 100
    {
        return Err("Choose an existing absolute destination and a simple subdirectory".into());
    }
    let meta = tokio::fs::symlink_metadata(base)
        .await
        .map_err(|e| format!("Selected destination unavailable: {e}"))?;
    if !meta.is_dir() || meta.file_type().is_symlink() {
        return Err("Selected destination must be an existing real directory".into());
    }
    let actual = tokio::fs::canonicalize(base)
        .await
        .map_err(|e| format!("Cannot resolve destination: {e}"))?;
    let target = actual.join(&subdir);
    for _ in 0..2 {
        let mountinfo = tokio::fs::read_to_string("/proc/self/mountinfo")
            .await
            .map_err(|e| format!("Cannot check live mounts: {e}"))?;
        let mounts = MountTable::from_mountinfo(&mountinfo);
        let entry = mounts
            .find_for_path(&actual)
            .ok_or("Destination is not on a known mounted filesystem")?;
        if entry.mount_point.to_string_lossy() != expected_mount_point
            || entry.source != expected_mount_source
        {
            return Err("Destination mount changed or disconnected. No files were written.".into());
        }
        create_checkpoint_subdir_by_fd(&actual, &subdir)?;
    }
    Ok(target.to_string_lossy().into_owned())
}

#[tauri::command]
async fn profile_runtime(profile_id: String) -> Result<String, String> {
    call_agent("ProfileRuntime", &profile_id).await
}

#[tauri::command]
async fn discover_sources() -> Result<String, String> {
    let sources = harbor_agent::discovery::discover_sources()
        .await
        .map_err(|err| format!("Cannot inspect this computer's Btrfs layout: {err}"))?;
    serde_json::to_string(&sources).map_err(|err| format!("Cannot encode Btrfs discovery: {err}"))
}

#[tauri::command]
async fn backup_status(profile_id: String) -> Result<String, String> {
    call_agent("BackupStatus", &profile_id).await
}

fn valid_snapper_config_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 80
        && name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
}

#[tauri::command]
async fn native_snapshot_choices(app: tauri::AppHandle, source: String) -> Result<String, String> {
    let path = Path::new(&source);
    if !path.is_absolute()
        || source.contains('\0')
        || source.contains("//")
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
        || path.is_symlink()
    {
        return Err("Invalid native source subvolume".into());
    }
    let engine =
        bundled_engine_candidate(&app).ok_or("Bundled Btrfs snapshot reader is unavailable")?;
    let output = Command::new("/usr/bin/pkexec")
        .arg(engine)
        .args(["raw", "checkpoint-v2", "native-list", "--source", &source])
        .output()
        .await
        .map_err(|err| format!("Cannot query native snapshots: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "Cannot list native snapshots: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    if output.stdout.len() > 2_000_000 {
        return Err("Native snapshot list too large".into());
    }
    let parsed: Value = serde_json::from_slice(&output.stdout)
        .map_err(|err| format!("Invalid native snapshot list: {err}"))?;
    if !parsed.is_array() {
        return Err("Expected native snapshot array".into());
    }
    serde_json::to_string(&parsed).map_err(|err| format!("Cannot encode native snapshots: {err}"))
}

/// Recovery from an existing raw archive does not require this machine's
/// profile: it is intentionally usable after a clean OS reinstall.
fn checked_recovery_folder(value: &str) -> Result<PathBuf, String> {
    let path = Path::new(value);
    if value.is_empty()
        || value == "/"
        || value.starts_with("//")
        || value.contains('\0')
        || !path.is_absolute()
        || path
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
        || path.is_symlink()
        || !path.is_dir()
    {
        return Err("Select an existing absolute directory without symlinks".into());
    }
    path.canonicalize()
        .map_err(|err| format!("Cannot resolve recovery folder: {err}"))
}

#[tauri::command]
async fn archive_restore_catalog(app: tauri::AppHandle, source: String) -> Result<String, String> {
    let source = checked_recovery_folder(&source)?;
    let engine = bundled_engine_candidate(&app).ok_or("Bundled recovery engine unavailable")?;
    let output = Command::new(engine)
        .arg("raw")
        .arg("list")
        .arg(&source)
        .arg("--json")
        .output()
        .await
        .map_err(|e| format!("Cannot inspect backup catalog: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "Cannot read backups: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    if output.stdout.len() > 4_000_000 {
        return Err("Too many backup records in this directory".into());
    }
    let parsed: Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("Backup catalog was not valid JSON: {e}"))?;
    if !parsed.is_array() {
        return Err("Backup directory did not return a backup list".into());
    }
    serde_json::to_string(&parsed).map_err(|e| format!("Cannot encode backup list: {e}"))
}

#[tauri::command]
async fn archive_restore_staged(
    app: tauri::AppHandle,
    source: String,
    staging: String,
    snapshot_name: String,
    dry_run: bool,
) -> Result<String, String> {
    let source = checked_recovery_folder(&source)?;
    let staging = checked_recovery_folder(&staging)?;
    if source == staging || source.starts_with(&staging) || staging.starts_with(&source) {
        return Err("Choose a distinct Btrfs staging location, not the backup directory".into());
    }
    if validate_name_for_restore(&snapshot_name).is_err() {
        return Err("Unsafe backup name".into());
    }
    let mounts = MountTable::from_mountinfo(
        &tokio::fs::read_to_string("/proc/self/mountinfo")
            .await
            .map_err(|e| format!("Cannot inspect staging mount: {e}"))?,
    );
    if mounts
        .find_for_path(&staging)
        .is_none_or(|entry| entry.fs_type != "btrfs")
    {
        return Err("The staging destination must be an existing Btrfs directory".into());
    }
    let engine = bundled_engine_candidate(&app).ok_or("Bundled recovery engine unavailable")?;
    let raw = format!("raw://{}", source.to_string_lossy());
    // The engine's existing restore planner validates parent chains, stream
    // SHA-256 and collision safety. Never use --in-place/--overwrite/--skip-verify.
    let mut command = Command::new("/usr/bin/pkexec");
    command
        .arg(engine)
        .arg("restore")
        .arg(raw)
        .arg(staging)
        .arg("--snapshot")
        .arg(snapshot_name);
    if dry_run {
        command.arg("--dry-run");
    }
    let output = command
        .output()
        .await
        .map_err(|e| format!("Cannot launch safe staged recovery: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "Staged recovery refused: {} {}",
            String::from_utf8_lossy(&output.stderr).trim(),
            String::from_utf8_lossy(&output.stdout).trim(),
        ));
    }
    if output.stdout.len() > 2_000_000 {
        return Err("Recovery output exceeded the status limit".into());
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn validate_name_for_restore(name: &str) -> Result<(), String> {
    // Match the inherited raw engine's basename rules, not the narrower
    // checkpoint archive creation rules. Historical backup names such as
    // root.20261009T120000 are valid and must remain recoverable.
    if name.is_empty()
        || name == "."
        || name == ".."
        || name.as_bytes().len() > 200
        || name.contains('/')
        || name.contains('\0')
    {
        Err("Unsafe archive name".into())
    } else {
        Ok(())
    }
}

#[tauri::command]
async fn snapper_snapshot_choices(
    app: tauri::AppHandle,
    config_name: String,
) -> Result<String, String> {
    if !valid_snapper_config_name(&config_name) {
        return Err("Invalid Snapper configuration name".into());
    }
    let engine =
        bundled_engine_candidate(&app).ok_or("The bundled snapshot reader is unavailable")?;
    // Reuse the existing SnapperScanner JSON output: no second parser,
    // no Snapper settings changes and no command shell interpolation.
    let output = Command::new("/usr/bin/pkexec")
        .arg(engine)
        .args(["snapper", "list", "--config", &config_name, "--json"])
        .output()
        .await
        .map_err(|err| format!("Cannot list host Snapper snapshots: {err}"))?;
    if !output.status.success() {
        return Err(format!(
            "Cannot list Snapper snapshots: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    if output.stdout.len() > 2_000_000 {
        return Err("The snapshot list is too large to display".into());
    }
    let parsed: Value = serde_json::from_slice(&output.stdout)
        .map_err(|err| format!("Invalid Snapper snapshot list: {err}"))?;
    if !parsed.get("configs").is_some_and(Value::is_array) {
        return Err("Snapper snapshot response has no configuration list".into());
    }
    serde_json::to_string(&parsed).map_err(|err| format!("Snapshot serialization failed: {err}"))
}

#[tauri::command]
async fn checkpoint_action(
    app: tauri::AppHandle,
    state: tauri::State<'_, BackupRuntimeState>,
    request: checkpoint_actions::CheckpointRequest,
) -> Result<String, String> {
    let args = checkpoint_actions::action_arguments(&request)?;
    let executable = bundled_engine_candidate(&app)
        .ok_or("The bundled v2 engine is unavailable; install the v0.2.6 package")?;
    let long_running = matches!(
        request.action.as_str(),
        "start" | "resume" | "set-start" | "set-resume" | "set-restore"
    );
    if long_running {
        // Existing backup runtime prevents closing the only controlling UI
        // when no tray-based, durable v2 worker supervisor is available.
        state.begin()?;
    }
    let mut command = Command::new("/usr/bin/pkexec");
    command
        .arg(executable)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if long_running {
        // v2 checkpoints must have the same tray and scoped Stop behavior as
        // classic manual backups. An isolated PGID makes Stop target this
        // transaction alone, not unrelated processes or other backups.
        command.process_group(0);
    }
    let result = match command.spawn() {
        Ok(child) => {
            if long_running {
                if let Some(pid) = child.id() {
                    state.set_process_group(pid);
                }
                // If the desktop cannot create a tray, the close policy keeps
                // the controlling window open rather than killing the worker.
                let _ = ensure_backup_tray(&app);
            }
            child
                .wait_with_output()
                .await
                .map_err(|err| format!("Cannot wait for privileged checkpoint engine: {err}"))
                .and_then(|output| {
                    if output.status.success() {
                        Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
                    } else {
                        let error = String::from_utf8_lossy(&output.stderr).trim().to_owned();
                        Err(if error.is_empty() {
                            format!("Checkpoint operation failed: {}", output.status)
                        } else {
                            error
                        })
                    }
                })
        }
        Err(err) => Err(format!("Cannot invoke privileged checkpoint engine: {err}")),
    };
    if long_running {
        finish_background_backup(&app, result.is_ok());
    }
    result
}

#[tauri::command]
async fn checkpoint_transfers(app: tauri::AppHandle, target: String) -> Result<String, String> {
    // Reject missing/symlink/nonabsolute targets without creating them.
    // Check presence of v2 manifests by filenames only; never follow symlinks.
    let has_manifests = tokio::task::spawn_blocking({
        let target = target.clone();
        move || -> Result<bool, String> {
            let root = Path::new(&target);
            let _ = checkpoint_status::list_checkpoint_manifests(root)?;
            let mut found = false;
            for entry in std::fs::read_dir(root)
                .map_err(|e| e.to_string())?
                .take(2048)
            {
                let entry = entry.map_err(|e| e.to_string())?;
                let filename = entry.file_name();
                let filename = filename.to_string_lossy();
                if filename.starts_with(".harbor-resume-") && filename.ends_with(".json") {
                    found = true;
                    break;
                }
            }
            Ok(found)
        }
    })
    .await
    .map_err(|err| format!("Checkpoint directory inspection failed: {err}"))??;
    if !has_manifests {
        return Ok("[]".to_owned());
    }
    let engine = bundled_engine_candidate(&app).ok_or("Bundled v2 engine is unavailable")?;
    let arguments = ["raw", "checkpoint-v2", "list", "--target", target.as_str()];
    let normal = Command::new(&engine).args(arguments).output().await;
    if let Ok(output) = normal
        && output.status.success()
        && String::from_utf8_lossy(&output.stdout).trim() != "[]"
    {
        return Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned());
    }
    // Private 0600 transaction journals are not made world-readable:
    // enumerate through polkit only when such records actually exist.
    let privileged = Command::new("/usr/bin/pkexec")
        .arg(engine)
        .args(arguments)
        .output()
        .await
        .map_err(|err| format!("Cannot inspect private checkpoints: {err}"))?;
    if !privileged.status.success() {
        return Err(String::from_utf8_lossy(&privileged.stderr)
            .trim()
            .to_owned());
    }
    Ok(String::from_utf8_lossy(&privileged.stdout)
        .trim()
        .to_owned())
}

#[tauri::command]
async fn machine_set_catalog(app: tauri::AppHandle, target: String) -> Result<String, String> {
    // A read-only operation: avoid a polkit prompt on every status refresh.
    let root = Path::new(&target);
    if !root.is_absolute()
        || target.contains('\0')
        || root.is_symlink()
        || !root.is_dir()
        || root
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err("Existing real absolute backup destination required".into());
    }
    let mut found = false;
    for entry in std::fs::read_dir(root)
        .map_err(|e| e.to_string())?
        .take(4096)
    {
        let name = entry.map_err(|e| e.to_string())?.file_name();
        let name = name.to_string_lossy();
        if name.starts_with(".harbor-machine-set-") && name.ends_with(".json") {
            found = true;
            break;
        }
    }
    if !found {
        return Ok("[]".into());
    }
    let engine = bundled_engine_candidate(&app).ok_or("Bundled v2 engine unavailable")?;
    let args = [
        "raw",
        "checkpoint-v2",
        "set-list",
        "--target",
        target.as_str(),
    ];
    if let Ok(response) = Command::new(&engine).args(args).output().await
        && response.status.success()
    {
        return Ok(String::from_utf8_lossy(&response.stdout).trim().into());
    }
    // Some root-owned catalogs are mode 0600; elevate only if needed.
    let response = Command::new("/usr/bin/pkexec")
        .arg(engine)
        .args(args)
        .output()
        .await
        .map_err(|e| format!("Cannot inspect protected machine sets: {e}"))?;
    if !response.status.success() {
        return Err(String::from_utf8_lossy(&response.stderr).trim().into());
    }
    Ok(String::from_utf8_lossy(&response.stdout).trim().into())
}

async fn run_privileged_profile_command(
    app: &tauri::AppHandle,
    command: &str,
    profile_id: &str,
) -> Result<String, String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();
    let helper = helper_executable(app)?;

    let output = Command::new("/usr/bin/pkexec")
        .arg(helper)
        .arg(command)
        .arg(profile_id)
        .output()
        .await
        .map_err(|err| format!("Cannot start privileged Harbor helper: {err}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Harbor helper failed with {}", output.status)
        } else {
            stderr
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

async fn run_unscoped_command_with_stdin(
    app: &tauri::AppHandle,
    command: &str,
    input: &[u8],
) -> Result<String, String> {
    let helper = helper_executable(app)?;
    let mut child = Command::new(helper)
        .arg(command)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Cannot start Harbor helper: {err}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(input)
            .await
            .map_err(|err| format!("Cannot send data to Harbor helper: {err}"))?;
        stdin
            .shutdown()
            .await
            .map_err(|err| format!("Cannot finish Harbor helper input: {err}"))?;
    } else {
        return Err("Harbor helper stdin was unavailable".into());
    }

    let output = child
        .wait_with_output()
        .await
        .map_err(|err| format!("Cannot wait for Harbor helper: {err}"))?;
    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Harbor helper failed with {}", output.status)
        } else {
            stderr
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

async fn run_profile_command_with_stdin(
    app: &tauri::AppHandle,
    command: &str,
    profile_id: &str,
    input: &[u8],
) -> Result<String, String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();
    let helper = helper_executable(app)?;

    let mut child = Command::new(helper)
        .arg(command)
        .arg(profile_id)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Cannot start Harbor helper: {err}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(input)
            .await
            .map_err(|err| format!("Cannot send data to Harbor helper: {err}"))?;
        stdin
            .shutdown()
            .await
            .map_err(|err| format!("Cannot finish Harbor helper input: {err}"))?;
    } else {
        return Err("Harbor helper stdin was unavailable".into());
    }

    let output = child
        .wait_with_output()
        .await
        .map_err(|err| format!("Cannot wait for Harbor helper: {err}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Harbor helper failed with {}", output.status)
        } else {
            stderr
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

async fn run_privileged_profile_command_with_stdin(
    app: &tauri::AppHandle,
    command: &str,
    profile_id: &str,
    input: &[u8],
) -> Result<String, String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();
    let helper = helper_executable(app)?;

    let mut child = Command::new("/usr/bin/pkexec")
        .arg(helper)
        .arg(command)
        .arg(profile_id)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Cannot start privileged Harbor helper: {err}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(input)
            .await
            .map_err(|err| format!("Cannot send configuration to Harbor helper: {err}"))?;
        stdin
            .shutdown()
            .await
            .map_err(|err| format!("Cannot finish Harbor helper input: {err}"))?;
    } else {
        return Err("Harbor helper stdin was unavailable".into());
    }

    let output = child
        .wait_with_output()
        .await
        .map_err(|err| format!("Cannot wait for privileged Harbor helper: {err}"))?;

    if !output.status.success() {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_owned();
        return Err(if stderr.is_empty() {
            format!("Harbor helper failed with {}", output.status)
        } else {
            stderr
        });
    }

    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

#[tauri::command]
async fn apply_configuration(
    app: tauri::AppHandle,
    configuration: String,
    profile_id: String,
) -> Result<String, String> {
    run_privileged_profile_command_with_stdin(
        &app,
        "apply-config",
        &profile_id,
        configuration.as_bytes(),
    )
    .await
}

async fn run_privileged_profile_stream_inner(
    app: &tauri::AppHandle,
    command: &str,
    profile_id: &str,
    input: Option<&[u8]>,
    on_event: Channel<Value>,
    backup_state: Option<&BackupRuntimeState>,
) -> Result<(), String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();
    let helper = helper_executable(app)?;

    let mut helper_command = Command::new("/usr/bin/pkexec");
    helper_command
        .arg(helper)
        .arg(command)
        .arg(profile_id)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if backup_state.is_some() {
        helper_command.process_group(0);
    }
    let mut child = helper_command
        .spawn()
        .map_err(|err| format!("Cannot start privileged Harbor helper: {err}"))?;

    if let Some(state) = backup_state {
        let pid = child
            .id()
            .ok_or_else(|| "Cannot determine the Harbor backup process group.".to_string())?;
        state.set_process_group(pid);
        let _ = ensure_backup_tray(app);
    }

    if let Some(mut stdin) = child.stdin.take() {
        if let Some(input) = input {
            stdin
                .write_all(input)
                .await
                .map_err(|err| format!("Cannot send data to Harbor helper: {err}"))?;
        }
        stdin
            .shutdown()
            .await
            .map_err(|err| format!("Cannot finish Harbor helper input: {err}"))?;
    } else {
        return Err("Harbor helper stdin was unavailable".into());
    }

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Harbor helper stdout was unavailable".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Harbor helper stderr was unavailable".to_string())?;

    let mut stdout_lines = BufReader::new(stdout).lines();
    let mut stderr_lines = BufReader::new(stderr).lines();
    let mut stderr_tail = Vec::<String>::new();
    let mut stdout_done = false;
    let mut stderr_done = false;

    while !stdout_done || !stderr_done {
        tokio::select! {
            line = stdout_lines.next_line(), if !stdout_done => {
                match line {
                    Ok(Some(line)) => {
                        let event = serde_json::from_str::<Value>(&line).unwrap_or_else(|_| {
                            serde_json::json!({
                                "event": "output",
                                "phase": "helper",
                                "message": line,
                                "stream": "stdout"
                            })
                        });
                        let _ = on_event.send(event);
                    }
                    Ok(None) => stdout_done = true,
                    Err(err) => {
                        stdout_done = true;
                        let _ = on_event.send(serde_json::json!({
                            "event": "output",
                            "phase": "helper",
                            "message": format!("Cannot read helper stdout: {err}"),
                            "stream": "stderr"
                        }));
                    }
                }
            }
            line = stderr_lines.next_line(), if !stderr_done => {
                match line {
                    Ok(Some(line)) => {
                        stderr_tail.push(line.clone());
                        if stderr_tail.len() > 8 {
                            stderr_tail.remove(0);
                        }
                        let _ = on_event.send(serde_json::json!({
                            "event": "output",
                            "phase": "helper",
                            "message": line,
                            "stream": "stderr"
                        }));
                    }
                    Ok(None) => stderr_done = true,
                    Err(err) => {
                        stderr_done = true;
                        let _ = on_event.send(serde_json::json!({
                            "event": "output",
                            "phase": "helper",
                            "message": format!("Cannot read helper stderr: {err}"),
                            "stream": "stderr"
                        }));
                    }
                }
            }
        }
    }

    let status = child
        .wait()
        .await
        .map_err(|err| format!("Cannot wait for privileged Harbor helper: {err}"))?;

    if !status.success() {
        if backup_state.is_some_and(|state| state.stop_requested.load(Ordering::SeqCst)) {
            return Err(current_background_backup_text().stopped.to_string());
        }
        return Err(helper_failure_message(&status, &stderr_tail));
    }

    Ok(())
}

async fn run_background_backup_stream(
    app: &tauri::AppHandle,
    state: &BackupRuntimeState,
    command: &str,
    profile_id: &str,
    input: Option<&[u8]>,
    on_event: Channel<Value>,
) -> Result<(), String> {
    state.begin()?;
    let result =
        run_privileged_profile_stream_inner(app, command, profile_id, input, on_event, Some(state))
            .await;
    finish_background_backup(app, result.is_ok());
    result
}

#[tauri::command]
async fn send_snapshot_now_stream(
    app: tauri::AppHandle,
    state: tauri::State<'_, BackupRuntimeState>,
    profile_id: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_background_backup_stream(
        &app,
        state.inner(),
        "run-profile-jsonl",
        &profile_id,
        None,
        on_event,
    )
    .await
}

#[tauri::command]
async fn send_draft_now_stream(
    app: tauri::AppHandle,
    state: tauri::State<'_, BackupRuntimeState>,
    configuration: String,
    profile_id: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_background_backup_stream(
        &app,
        state.inner(),
        "run-config-jsonl",
        &profile_id,
        Some(configuration.as_bytes()),
        on_event,
    )
    .await
}

#[tauri::command]
async fn recovery_kit_context(
    app: tauri::AppHandle,
    configuration: String,
    profile_id: String,
    destination_id: String,
) -> Result<String, String> {
    let configuration: Value =
        serde_json::from_str(&configuration).map_err(|err| err.to_string())?;
    let query = serde_json::json!({
        "configuration": configuration,
        "destination_id": destination_id,
    });
    let encoded = serde_json::to_vec(&query).map_err(|err| err.to_string())?;
    run_profile_command_with_stdin(&app, "recovery-kit-context-json", &profile_id, &encoded).await
}

#[tauri::command]
async fn machine_recovery_plan(app: tauri::AppHandle, request: String) -> Result<String, String> {
    let request: Value = serde_json::from_str(&request).map_err(|err| err.to_string())?;
    let encoded = serde_json::to_vec(&request).map_err(|err| err.to_string())?;
    run_unscoped_command_with_stdin(&app, "machine-recovery-plan-json", &encoded).await
}

#[tauri::command]
fn local_os_release() -> Result<String, String> {
    std::fs::read_to_string("/etc/os-release")
        .map_err(|err| format!("Cannot read /etc/os-release: {err}"))
}

#[tauri::command]
async fn list_restore_points(
    app: tauri::AppHandle,
    configuration: String,
    profile_id: String,
    source_path: String,
    destination_id: String,
) -> Result<String, String> {
    let configuration: Value = serde_json::from_str(&configuration)
        .map_err(|err| format!("Invalid Harbor configuration: {err}"))?;
    let destination_id = Uuid::parse_str(&destination_id)
        .map_err(|err| format!("Invalid destination UUID: {err}"))?;
    let query = serde_json::json!({
        "configuration": configuration,
        "destination_id": destination_id,
        "source_path": source_path,
    });
    run_profile_command_with_stdin(
        &app,
        "list-restore-points-json",
        &profile_id,
        serde_json::to_vec(&query)
            .map_err(|err| format!("Cannot encode restore-point query: {err}"))?
            .as_slice(),
    )
    .await
}

#[tauri::command]
async fn stage_restore(
    app: tauri::AppHandle,
    configuration: String,
    profile_id: String,
    request: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    let configuration: Value = serde_json::from_str(&configuration)
        .map_err(|err| format!("Invalid Harbor configuration: {err}"))?;
    let request: Value =
        serde_json::from_str(&request).map_err(|err| format!("Invalid restore request: {err}"))?;
    let envelope = serde_json::json!({
        "configuration": configuration,
        "request": request,
    });
    let encoded = serde_json::to_vec(&envelope)
        .map_err(|err| format!("Cannot encode portable restore request: {err}"))?;
    run_privileged_profile_stream_inner(
        &app,
        "stage-restore-config-jsonl",
        &profile_id,
        Some(&encoded),
        on_event,
        None,
    )
    .await
}

async fn run_privileged_unscoped_stream(
    app: &tauri::AppHandle,
    command: &str,
    input: &[u8],
    on_event: Channel<Value>,
) -> Result<(), String> {
    let helper = helper_executable(app)?;
    let mut child = Command::new("/usr/bin/pkexec")
        .arg(helper)
        .arg(command)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|err| format!("Cannot start privileged Harbor helper: {err}"))?;

    if let Some(mut stdin) = child.stdin.take() {
        stdin
            .write_all(input)
            .await
            .map_err(|err| format!("Cannot send data to Harbor helper: {err}"))?;
        stdin
            .shutdown()
            .await
            .map_err(|err| format!("Cannot finish Harbor helper input: {err}"))?;
    } else {
        return Err("Harbor helper stdin was unavailable".into());
    }

    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "Harbor helper stdout was unavailable".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "Harbor helper stderr was unavailable".to_string())?;

    let mut stdout_lines = BufReader::new(stdout).lines();
    let mut stderr_lines = BufReader::new(stderr).lines();
    let mut stdout_done = false;
    let mut stderr_done = false;

    while !stdout_done || !stderr_done {
        tokio::select! {
            line = stdout_lines.next_line(), if !stdout_done => {
                match line {
                    Ok(Some(line)) => {
                        let event = serde_json::from_str::<Value>(&line).unwrap_or_else(|_| {
                            serde_json::json!({
                                "event": "output",
                                "phase": "helper",
                                "message": line,
                                "stream": "stdout"
                            })
                        });
                        let _ = on_event.send(event);
                    }
                    Ok(None) => stdout_done = true,
                    Err(err) => {
                        stdout_done = true;
                        let _ = on_event.send(serde_json::json!({
                            "event": "output",
                            "phase": "helper",
                            "message": format!("Cannot read helper stdout: {err}"),
                            "stream": "stderr"
                        }));
                    }
                }
            }
            line = stderr_lines.next_line(), if !stderr_done => {
                match line {
                    Ok(Some(line)) => {
                        let _ = on_event.send(serde_json::json!({
                            "event": "output",
                            "phase": "helper",
                            "message": line,
                            "stream": "stderr"
                        }));
                    }
                    Ok(None) => stderr_done = true,
                    Err(err) => {
                        stderr_done = true;
                        let _ = on_event.send(serde_json::json!({
                            "event": "output",
                            "phase": "helper",
                            "message": format!("Cannot read helper stderr: {err}"),
                            "stream": "stderr"
                        }));
                    }
                }
            }
        }
    }

    let status = child
        .wait()
        .await
        .map_err(|err| format!("Cannot wait for privileged Harbor helper: {err}"))?;

    if !status.success() {
        return Err(format!("Harbor helper failed with {status}"));
    }
    Ok(())
}

#[tauri::command]
async fn replicate_lxc(
    app: tauri::AppHandle,
    request: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_privileged_unscoped_stream(&app, "replicate-lxc-jsonl", request.as_bytes(), on_event).await
}

#[tauri::command]
async fn install_profile(app: tauri::AppHandle, profile_id: String) -> Result<String, String> {
    run_privileged_profile_command(&app, "install-profile", &profile_id).await
}

#[tauri::command]
async fn uninstall_profile(app: tauri::AppHandle, profile_id: String) -> Result<String, String> {
    run_privileged_profile_command(&app, "uninstall-profile", &profile_id).await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(BackupRuntimeState::default())
        .plugin(tauri_plugin_dialog::init())
        .on_window_event(|window, event| {
            let WindowEvent::CloseRequested { api, .. } = event else {
                return;
            };
            let app = window.app_handle();
            let state = app.state::<BackupRuntimeState>();
            let policy = backup_close_policy(
                state.active.load(Ordering::SeqCst),
                state.tray_available.load(Ordering::SeqCst),
            );
            let text = current_background_backup_text();
            match policy {
                BackupClosePolicy::CloseNormally => {}
                BackupClosePolicy::HideToTray => {
                    api.prevent_close();
                    // Closing the window during a v2/legacy backup is a quiet
                    // hide-to-tray, not a modal confirmation on every click.
                    state.hidden_for_backup.store(true, Ordering::SeqCst);
                    let _ = window.hide();
                }
                BackupClosePolicy::KeepWindowOpen => {
                    api.prevent_close();
                    app.dialog()
                        .message(text.no_tray_message)
                        .kind(MessageDialogKind::Warning)
                        .title(text.title)
                        .blocking_show();
                }
            }
        })
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            agent_version,
            system_summary,
            installation_state,
            system_identity,
            engine_status,
            engine_update_options,
            set_engine_policy,
            update_system_engine,
            open_harbor_release_page,
            install_full_package,
            profiles,
            configuration,
            portable_configuration,
            save_portable_configuration,
            profile_runtime,
            discover_sources,
            backup_status,
            checkpoint_transfers,
            machine_set_catalog,
            checkpoint_action,
            snapper_snapshot_choices,
            native_snapshot_choices,
            archive_restore_catalog,
            archive_restore_staged,
            inspect_mount,
            prepare_checkpoint_directory,
            apply_configuration,
            send_snapshot_now_stream,
            send_draft_now_stream,
            stop_active_backup,
            recovery_kit_context,
            machine_recovery_plan,
            local_os_release,
            list_restore_points,
            stage_restore,
            replicate_lxc,
            install_profile,
            uninstall_profile
        ])
        .run(tauri::generate_context!())
        .expect("error while building Btrfs Harbor desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn chosen_destination_creates_only_expected_source_directory() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path();
        let table =
            MountTable::from_mountinfo(&std::fs::read_to_string("/proc/self/mountinfo").unwrap());
        let mount = table.find_for_path(root).unwrap();
        let mounted = mount.mount_point.to_string_lossy().to_string();
        let source = mount.source.clone();
        let folder = root.join("rootfs");
        assert!(!folder.exists());
        let outcome = prepare_checkpoint_directory(
            root.to_string_lossy().to_string(),
            "rootfs".into(),
            mounted.clone(),
            source.clone(),
        )
        .await
        .unwrap();
        assert_eq!(outcome, folder.to_string_lossy());
        assert!(folder.is_dir());
        assert!(
            prepare_checkpoint_directory(
                root.to_string_lossy().to_string(),
                "../escape".into(),
                mounted.clone(),
                source.clone()
            )
            .await
            .is_err()
        );
        assert!(
            prepare_checkpoint_directory(
                root.to_string_lossy().to_string(),
                "home".into(),
                mounted,
                "unexpected-source".into()
            )
            .await
            .is_err()
        );
        assert!(!root.join("home").exists());
    }

    #[test]
    fn portable_restore_without_original_profile_rejects_traversal_and_unsafe_archives() {
        assert!(checked_recovery_folder("/").is_err());
        assert!(checked_recovery_folder("../backups").is_err());
        assert!(checked_recovery_folder("/backups/../etc").is_err());
        assert!(validate_name_for_restore("root_20261009").is_ok());
        assert!(validate_name_for_restore("root.20261009T153000").is_ok());
        assert!(validate_name_for_restore("user data 20261009").is_ok());
        assert!(validate_name_for_restore("home-école.20261009").is_ok());
        assert!(validate_name_for_restore(".").is_err());
        assert!(validate_name_for_restore("..").is_err());
        assert!(validate_name_for_restore(&"a".repeat(201)).is_err());
        assert!(validate_name_for_restore("../../etc").is_err());
        // There is no shell interpolation: punctuation is passed as one argv.
        assert!(validate_name_for_restore("root;rm").is_ok());
        assert!(validate_name_for_restore("a\\0b").is_err());
    }

    #[test]
    fn snapper_snapshot_read_only_api_rejects_invalid_config_names() {
        assert!(valid_snapper_config_name("root"));
        assert!(valid_snapper_config_name("home-1"));
        assert!(!valid_snapper_config_name(""));
        assert!(!valid_snapper_config_name("../root"));
        assert!(!valid_snapper_config_name("root;rm"));
        assert!(!valid_snapper_config_name("root space"));
        assert!(!valid_snapper_config_name(&"a".repeat(81)));
    }

    #[test]
    fn portable_profile_rejects_root_directory_and_round_trips_safely() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("portable-profiles.json");
        let mut config = serde_json::json!({
            "engine_policy": "auto",
            "destinations": [{
                "id": "22222222-2222-4222-8222-222222222222",
                "name": "Backup",
                "kind": "nfs",
                "path": "/mnt/backup/workstation",
                "mount_point": "/mnt/backup",
                "expected_mount_source": "server:/export",
                "compression": "zstd",
                "optional": false
            }],
            "profiles": [{
                "id": "11111111-1111-4111-8111-111111111111",
                "name": "Workstation",
                "sources": [{
                    "path": "/", "snapshot_prefix": "root-",
                    "snapper_config": "root", "target_subdir": "rootfs"
                }],
                "destination_ids": ["22222222-2222-4222-8222-222222222222"],
                "on_calendar": "*-*-* 02:00:00",
                "retention": {"hourly": 0, "daily": 7, "weekly": 4, "monthly": 3, "yearly": 0},
                "verify_after_backup": true
            }]
        });
        let json = config.to_string();
        save_portable_file(&path, &json).unwrap();
        assert_eq!(read_portable_file(&path).unwrap(), Some(json.clone()));
        assert_eq!(
            std::fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        config["destinations"][0]["path"] = serde_json::json!("/");
        assert!(save_portable_file(&path, &config.to_string()).is_err());
        assert_eq!(read_portable_file(&path).unwrap(), Some(json));
    }

    #[test]
    fn portable_profile_missing_file_is_not_mistaken_for_corrupt_configuration() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("no-such-profile.json");
        assert!(read_portable_file(&path).unwrap().is_none());
        std::os::unix::fs::symlink("/etc/passwd", &path).unwrap();
        assert!(read_portable_file(&path).is_err());
    }

    #[test]
    fn engine_provenance_prefers_local_package_ownership_over_path_hints() {
        assert_eq!(
            classify_engine_provenance(
                Path::new("/usr/bin/btrfs-backup-ng"),
                Some("btrfs-backup-ng 0.9.12-1"),
                None,
                None,
            ),
            EngineProvenance::Pacman {
                package: "btrfs-backup-ng".into()
            }
        );
        assert_eq!(
            classify_engine_provenance(
                Path::new("/usr/bin/btrfs-backup-ng"),
                None,
                Some("btrfs-backup-ng: /usr/bin/btrfs-backup-ng"),
                None,
            ),
            EngineProvenance::Dpkg {
                package: "btrfs-backup-ng".into()
            }
        );
        assert_eq!(
            classify_engine_provenance(
                Path::new("/usr/bin/btrfs-backup-ng"),
                None,
                None,
                Some("btrfs-backup-ng"),
            ),
            EngineProvenance::Rpm {
                package: "btrfs-backup-ng".into()
            }
        );
    }

    #[test]
    fn engine_provenance_detects_user_tool_and_unknown_manual_install() {
        assert_eq!(
            classify_engine_provenance(
                Path::new("/home/user/.local/share/uv/tools/btrfs-backup-ng/bin/btrfs-backup-ng"),
                None,
                None,
                None,
            ),
            EngineProvenance::UvTool
        );
        assert_eq!(
            classify_engine_provenance(
                Path::new("/opt/custom/bin/btrfs-backup-ng"),
                None,
                None,
                None,
            ),
            EngineProvenance::ManualUnknown
        );
    }

    #[test]
    fn update_strategy_never_overwrites_unknown_manual_engine() {
        assert_eq!(
            update_strategy_for_origin(
                harbor_engine::EngineOrigin::System,
                EngineProvenance::ManualUnknown
            ),
            EngineUpdateStrategy::GuidanceOnly
        );
        assert_eq!(
            update_strategy_for_origin(
                harbor_engine::EngineOrigin::Bundled,
                EngineProvenance::BundledHarbor
            ),
            EngineUpdateStrategy::HarborApplication
        );
    }

    #[test]
    fn package_update_commands_are_fixed_by_local_provenance() {
        assert_eq!(
            update_command_for_provenance(
                &EngineProvenance::Pacman {
                    package: "btrfs-backup-ng".into()
                },
                true,
                false,
            ),
            Some(UpdateCommandSpec {
                program: "/usr/bin/pacman".into(),
                args: vec![
                    "-S".into(),
                    "--needed".into(),
                    "--noconfirm".into(),
                    "btrfs-backup-ng".into()
                ],
                privileged: true
            })
        );
        assert_eq!(
            update_command_for_provenance(
                &EngineProvenance::Rpm {
                    package: "btrfs-backup-ng".into()
                },
                false,
                true,
            ),
            Some(UpdateCommandSpec {
                program: "/usr/bin/zypper".into(),
                args: vec![
                    "--non-interactive".into(),
                    "update".into(),
                    "btrfs-backup-ng".into()
                ],
                privileged: true
            })
        );
        assert_eq!(
            update_command_for_provenance(&EngineProvenance::ManualUnknown, true, true),
            None
        );
    }

    #[test]
    fn user_tool_update_commands_never_require_root() {
        let uv = update_command_for_provenance(&EngineProvenance::UvTool, false, false).unwrap();
        assert_eq!(uv.program, "uv");
        assert_eq!(uv.args, vec!["tool", "upgrade", "btrfs-backup-ng"]);
        assert!(!uv.privileged);

        let pipx = update_command_for_provenance(&EngineProvenance::Pipx, false, false).unwrap();
        assert_eq!(pipx.program, "pipx");
        assert_eq!(pipx.args, vec!["upgrade", "btrfs-backup-ng"]);
        assert!(!pipx.privileged);
    }

    #[test]
    fn appimage_reexec_is_required_before_privileged_helpers_touch_fuse_payload() {
        assert!(appimage_extract_reexec_required(
            Some(std::ffi::OsStr::new("/home/user/Btrfs-Harbor.AppImage")),
            None
        ));
        assert!(!appimage_extract_reexec_required(None, None));
        assert!(!appimage_extract_reexec_required(
            Some(std::ffi::OsStr::new("/home/user/Btrfs-Harbor.AppImage")),
            Some(std::ffi::OsStr::new("1"))
        ));
    }

    #[test]
    fn helper_failure_prefers_captured_stderr_over_generic_exit_status() {
        let status = std::process::Command::new("/bin/sh")
            .args(["-c", "exit 127"])
            .status()
            .unwrap();
        assert_eq!(
            helper_failure_message(&status, &["Permission denied".to_string()]),
            "Permission denied"
        );
        assert!(helper_failure_message(&status, &[]).contains("exit status: 127"));
    }

    #[test]
    fn bundled_helper_lives_under_portable_resource_directory() {
        let root = PathBuf::from("/app/resources");
        assert_eq!(
            bundled_helper_path(&root),
            PathBuf::from("/app/resources/portable/btrfs-harborctl")
        );
    }

    #[test]
    fn installed_helper_is_preferred_over_bundled_helper() {
        let selected = choose_helper_candidate(
            Some(PathBuf::from("/usr/bin/btrfs-harborctl")),
            Some(PathBuf::from("/app/resources/portable/btrfs-harborctl")),
        )
        .unwrap();

        assert_eq!(selected, PathBuf::from("/usr/bin/btrfs-harborctl"));
    }

    #[test]
    fn portable_appimage_uses_bundled_helper_when_not_installed() {
        let selected = choose_helper_candidate(
            None,
            Some(PathBuf::from("/app/resources/portable/btrfs-harborctl")),
        )
        .unwrap();

        assert_eq!(
            selected,
            PathBuf::from("/app/resources/portable/btrfs-harborctl")
        );
    }

    #[test]
    fn closing_during_backup_hides_only_when_tray_is_available() {
        assert_eq!(
            backup_close_policy(true, true),
            BackupClosePolicy::HideToTray
        );
        assert_eq!(
            backup_close_policy(true, false),
            BackupClosePolicy::KeepWindowOpen
        );
        assert_eq!(
            backup_close_policy(false, true),
            BackupClosePolicy::CloseNormally
        );
    }

    #[test]
    fn stop_targets_the_complete_backup_process_group() {
        assert_eq!(backup_stop_signal(), "-INT");
        assert_eq!(process_group_target(4242), "-4242");
    }

    #[test]
    fn french_background_tray_has_clear_status_and_stop_action() {
        let text = background_backup_text(Some("fr_FR.UTF-8"));
        assert!(text.tooltip.contains("sauvegarde en cours"));
        assert!(text.no_tray_message.contains("zone de notification"));
        assert_eq!(text.stop_label, "Arrêter la sauvegarde");
    }
}
