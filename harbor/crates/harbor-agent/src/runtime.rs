use anyhow::{Context, Result, bail};
use harbor_core::profile_unit_stem;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::env;
use std::path::PathBuf;
use tokio::process::Command;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ProfileRuntime {
    pub profile_id: Uuid,
    pub timer_unit: String,
    pub service_unit: String,
    pub timer_installed: bool,
    pub timer_enabled: bool,
    pub timer_active: bool,
    pub timer_sub_state: Option<String>,
    pub next_elapse_realtime: Option<String>,
    pub last_trigger: Option<String>,
    pub service_active: bool,
    pub service_sub_state: Option<String>,
    pub service_result: Option<String>,
    pub service_exit_status: Option<i32>,
}

fn systemctl_executable() -> PathBuf {
    env::var_os("BTRFS_HARBOR_SYSTEMCTL")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("systemctl"))
}

pub fn parse_systemctl_show(raw: &str) -> HashMap<String, String> {
    raw.lines()
        .filter_map(|line| {
            let (key, value) = line.split_once('=')?;
            Some((key.trim().to_owned(), value.trim().to_owned()))
        })
        .collect()
}

fn present(value: Option<&String>) -> Option<String> {
    value
        .map(String::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .filter(|value| !matches!(*value, "n/a" | "[not set]" | "0"))
        .map(ToOwned::to_owned)
}

pub fn runtime_from_properties(
    id: Uuid,
    timer: &HashMap<String, String>,
    service: &HashMap<String, String>,
) -> ProfileRuntime {
    let stem = profile_unit_stem(id);
    let timer_state = timer
        .get("UnitFileState")
        .map(String::as_str)
        .unwrap_or_default();
    let timer_load = timer
        .get("LoadState")
        .map(String::as_str)
        .unwrap_or_default();
    let service_exit_status = service
        .get("ExecMainStatus")
        .and_then(|value| value.parse::<i32>().ok());

    ProfileRuntime {
        profile_id: id,
        timer_unit: format!("{stem}.timer"),
        service_unit: format!("{stem}.service"),
        timer_installed: timer_load == "loaded",
        timer_enabled: timer_state == "enabled" || timer_state == "enabled-runtime",
        timer_active: timer
            .get("ActiveState")
            .is_some_and(|value| value == "active"),
        timer_sub_state: present(timer.get("SubState")),
        next_elapse_realtime: present(timer.get("NextElapseUSecRealtime")),
        last_trigger: present(timer.get("LastTriggerUSec")),
        service_active: service
            .get("ActiveState")
            .is_some_and(|value| value == "active"),
        service_sub_state: present(service.get("SubState")),
        service_result: present(service.get("Result")),
        service_exit_status,
    }
}

async fn show_unit(unit: &str, properties: &[&str]) -> Result<HashMap<String, String>> {
    let mut command = Command::new(systemctl_executable());
    command.arg("show").arg(unit).arg("--no-pager");
    for property in properties {
        command.arg("--property").arg(property);
    }

    let output = command
        .output()
        .await
        .with_context(|| format!("cannot inspect systemd unit {unit}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout);
    if stdout.trim().is_empty() && !output.status.success() {
        bail!(
            "systemctl show {unit} failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    Ok(parse_systemctl_show(&stdout))
}

pub async fn inspect_profile_runtime(id: Uuid) -> Result<ProfileRuntime> {
    let stem = profile_unit_stem(id);
    let timer = show_unit(
        &format!("{stem}.timer"),
        &[
            "LoadState",
            "ActiveState",
            "SubState",
            "UnitFileState",
            "NextElapseUSecRealtime",
            "LastTriggerUSec",
        ],
    )
    .await?;
    let path = show_unit(
        &format!("{stem}.path"),
        &["LoadState", "ActiveState", "SubState", "UnitFileState"],
    )
    .await?;
    let service = show_unit(
        &format!("{stem}.service"),
        &[
            "LoadState",
            "ActiveState",
            "SubState",
            "Result",
            "ExecMainStatus",
        ],
    )
    .await?;

    let path_is_loaded = path.get("LoadState").is_some_and(|value| value == "loaded");
    let schedule = if path_is_loaded { &path } else { &timer };
    let mut runtime = runtime_from_properties(id, schedule, &service);
    if path_is_loaded {
        runtime.timer_unit = format!("{stem}.path");
        runtime.next_elapse_realtime = None;
        runtime.last_trigger = None;
    }
    Ok(runtime)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_systemctl_show_without_losing_values_containing_equals() {
        let parsed =
            parse_systemctl_show("LoadState=loaded\nActiveState=active\nDescription=foo=bar\n");
        assert_eq!(parsed["LoadState"], "loaded");
        assert_eq!(parsed["Description"], "foo=bar");
    }

    #[test]
    fn runtime_reports_enabled_waiting_timer_and_last_service_result() {
        let id = Uuid::parse_str("11111111-1111-4111-8111-111111111111").unwrap();
        let timer = parse_systemctl_show(
            "LoadState=loaded\nActiveState=active\nSubState=waiting\nUnitFileState=enabled\nNextElapseUSecRealtime=Mon 2026-10-05 02:00:00 CEST\nLastTriggerUSec=Sun 2026-10-04 02:00:01 CEST\n",
        );
        let service = parse_systemctl_show(
            "LoadState=loaded\nActiveState=inactive\nSubState=dead\nResult=success\nExecMainStatus=0\n",
        );

        let runtime = runtime_from_properties(id, &timer, &service);

        assert!(runtime.timer_installed);
        assert!(runtime.timer_enabled);
        assert!(runtime.timer_active);
        assert_eq!(runtime.timer_sub_state.as_deref(), Some("waiting"));
        assert_eq!(
            runtime.next_elapse_realtime.as_deref(),
            Some("Mon 2026-10-05 02:00:00 CEST")
        );
        assert_eq!(runtime.service_result.as_deref(), Some("success"));
        assert_eq!(runtime.service_exit_status, Some(0));
    }

    #[test]
    fn runtime_cleanly_represents_not_installed_profile() {
        let id = Uuid::nil();
        let timer = parse_systemctl_show(
            "LoadState=not-found\nActiveState=inactive\nSubState=dead\nUnitFileState=\nNextElapseUSecRealtime=[not set]\nLastTriggerUSec=n/a\n",
        );
        let service = parse_systemctl_show(
            "LoadState=not-found\nActiveState=inactive\nSubState=dead\nResult=\nExecMainStatus=0\n",
        );

        let runtime = runtime_from_properties(id, &timer, &service);

        assert!(!runtime.timer_installed);
        assert!(!runtime.timer_enabled);
        assert!(!runtime.timer_active);
        assert!(runtime.next_elapse_realtime.is_none());
        assert!(runtime.last_trigger.is_none());
    }
}
