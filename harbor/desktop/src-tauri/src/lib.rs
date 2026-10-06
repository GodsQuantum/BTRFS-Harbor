use harbor_storage::MountTable;
use serde::Serialize;
use serde_json::Value;
use std::path::PathBuf;
use std::process::Stdio;
use tauri::ipc::Channel;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::process::Command;
use uuid::Uuid;

const BUS_NAME: &str = "io.github.GodsQuantum.BtrfsHarbor1";
const OBJECT_PATH: &str = "/io/github/GodsQuantum/BtrfsHarbor1";
const INTERFACE: &str = "io.github.GodsQuantum.BtrfsHarbor1";

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

#[tauri::command]
async fn profiles() -> Result<String, String> {
    call_agent("Profiles", &()).await
}

#[tauri::command]
async fn configuration() -> Result<String, String> {
    call_agent("Configuration", &()).await
}

#[derive(Debug, Serialize)]
struct MountProbe {
    mount_point: String,
    source: String,
    fs_type: String,
    kind: &'static str,
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
    call_agent("DiscoverSources", &()).await
}

#[tauri::command]
async fn backup_status(profile_id: String) -> Result<String, String> {
    call_agent("BackupStatus", &profile_id).await
}

async fn run_privileged_profile_command(command: &str, profile_id: &str) -> Result<String, String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();

    let output = Command::new("/usr/bin/pkexec")
        .arg("/usr/bin/btrfs-harborctl")
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

async fn run_privileged_profile_command_with_stdin(
    command: &str,
    profile_id: &str,
    input: &[u8],
) -> Result<String, String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();

    let mut child = Command::new("/usr/bin/pkexec")
        .arg("/usr/bin/btrfs-harborctl")
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
async fn apply_configuration(configuration: String, profile_id: String) -> Result<String, String> {
    run_privileged_profile_command_with_stdin("apply-config", &profile_id, configuration.as_bytes())
        .await
}

#[tauri::command]
async fn send_snapshot_now(profile_id: String) -> Result<String, String> {
    run_privileged_profile_command("run-profile", &profile_id).await
}

async fn run_privileged_profile_stream(
    command: &str,
    profile_id: &str,
    input: Option<&[u8]>,
    on_event: Channel<Value>,
) -> Result<(), String> {
    let profile_id = Uuid::parse_str(profile_id)
        .map_err(|err| format!("Invalid profile UUID: {err}"))?
        .to_string();

    let mut child = Command::new("/usr/bin/pkexec")
        .arg("/usr/bin/btrfs-harborctl")
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
async fn send_snapshot_now_stream(
    profile_id: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_privileged_profile_stream("run-profile-jsonl", &profile_id, None, on_event).await
}

#[tauri::command]
async fn stage_restore(
    profile_id: String,
    request: String,
    on_event: Channel<Value>,
) -> Result<(), String> {
    run_privileged_profile_stream(
        "stage-restore-jsonl",
        &profile_id,
        Some(request.as_bytes()),
        on_event,
    )
    .await
}

async fn run_privileged_unscoped_stream(
    command: &str,
    input: &[u8],
    on_event: Channel<Value>,
) -> Result<(), String> {
    let mut child = Command::new("/usr/bin/pkexec")
        .arg("/usr/bin/btrfs-harborctl")
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
async fn replicate_lxc(request: String, on_event: Channel<Value>) -> Result<(), String> {
    run_privileged_unscoped_stream("replicate-lxc-jsonl", request.as_bytes(), on_event).await
}

#[tauri::command]
async fn install_profile(profile_id: String) -> Result<String, String> {
    run_privileged_profile_command("install-profile", &profile_id).await
}

#[tauri::command]
async fn uninstall_profile(profile_id: String) -> Result<String, String> {
    run_privileged_profile_command("uninstall-profile", &profile_id).await
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
            profiles,
            configuration,
            profile_runtime,
            discover_sources,
            backup_status,
            inspect_mount,
            apply_configuration,
            send_snapshot_now,
            send_snapshot_now_stream,
            stage_restore,
            replicate_lxc,
            install_profile,
            uninstall_profile
        ])
        .run(tauri::generate_context!())
        .expect("error while building Btrfs Harbor desktop");
}
