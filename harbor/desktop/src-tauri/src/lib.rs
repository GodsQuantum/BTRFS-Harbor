use harbor_storage::MountTable;
use serde::Serialize;
use serde_json::Value;
use std::ffi::OsStr;
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;
use tauri::{Manager, ipc::Channel};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use uuid::Uuid;

const BUS_NAME: &str = "io.github.GodsQuantum.BtrfsHarbor1";
const OBJECT_PATH: &str = "/io/github/GodsQuantum/BtrfsHarbor1";
const INTERFACE: &str = "io.github.GodsQuantum.BtrfsHarbor1";

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

#[tauri::command]
async fn send_snapshot_now(app: tauri::AppHandle, profile_id: String) -> Result<String, String> {
    run_privileged_profile_command(&app, "run-profile", &profile_id).await
}

async fn run_privileged_profile_stream(
    app: &tauri::AppHandle,
    command: &str,
    profile_id: &str,
    input: Option<&[u8]>,
    on_event: Channel<Value>,
) -> Result<(), String> {
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
        return Err(helper_failure_message(&status, &stderr_tail));
    }

    Ok(())
}

#[tauri::command]
async fn send_snapshot_now_stream(
    app: tauri::AppHandle,
    profile_id: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_privileged_profile_stream(&app, "run-profile-jsonl", &profile_id, None, on_event).await
}

#[tauri::command]
async fn send_draft_now_stream(
    app: tauri::AppHandle,
    configuration: String,
    profile_id: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_privileged_profile_stream(
        &app,
        "run-config-jsonl",
        &profile_id,
        Some(configuration.as_bytes()),
        on_event,
    )
    .await
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
    run_privileged_profile_stream(
        &app,
        "stage-restore-config-jsonl",
        &profile_id,
        Some(&encoded),
        on_event,
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
        .plugin(tauri_plugin_dialog::init())
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
            install_full_package,
            profiles,
            configuration,
            profile_runtime,
            discover_sources,
            backup_status,
            inspect_mount,
            apply_configuration,
            send_snapshot_now,
            send_snapshot_now_stream,
            send_draft_now_stream,
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
}
