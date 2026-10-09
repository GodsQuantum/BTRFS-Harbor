//! Strict native checkpoint CLI argument adapter.
//! No shell, no arbitrary executable, no user-provided state path.
//! Experimental source transfers cannot accidentally replace legacy raw runs.
use serde::Deserialize;
use std::path::Path;
use uuid::Uuid;

pub const CONTROL_STATE_DIR: &str = "/var/lib/btrfs-harbor/checkpoint-v2";

#[derive(Debug, Clone, Deserialize)]
pub struct CheckpointRequest {
    pub action: String,
    pub target: String,
    pub name: Option<String>,
    pub profile_id: Option<String>,
    pub transfer_id: Option<String>,
    pub source_mode: Option<String>,
    pub snapper_config: Option<String>,
    pub snapper_number: Option<u64>,
    pub source: Option<String>,
    pub native_name: Option<String>,
    pub allow_local: bool,
    pub performance: Option<String>,
    pub set_id: Option<String>,
    pub sources: Option<Vec<String>>,
    pub staging: Option<String>,
    #[serde(default)]
    pub confirm: bool,
}

pub fn action_arguments(req: &CheckpointRequest) -> Result<Vec<String>, String> {
    let action = req.action.as_str();
    if action.starts_with("set-") {
        return set_action_arguments(req);
    }
    if !matches!(action, "start" | "resume" | "pause" | "stop" | "discard") {
        return Err("Unsupported checkpoint control action".into());
    }
    let target = Path::new(&req.target);
    if matches!(action, "start" | "resume" | "discard")
        && (!target.is_absolute()
            || req.target.contains('\0')
            || target
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
            || target.is_symlink()
            || !target.is_dir())
    {
        return Err("Existing absolute checkpoint destination required".into());
    }
    let mut args: Vec<String> = ["raw", "checkpoint-v2", action]
        .iter()
        .map(|s| (*s).to_owned())
        .collect();
    if matches!(action, "start" | "resume" | "discard") {
        args.extend(["--target".to_owned(), req.target.clone()]);
    }
    args.extend(["--state-dir".to_owned(), CONTROL_STATE_DIR.to_owned()]);

    if action == "start" {
        let id = req.profile_id.as_ref().ok_or("Missing profile identity")?;
        Uuid::parse_str(id).map_err(|_| "Invalid profile identity")?;
        let mode = req.source_mode.as_deref().ok_or("Missing source mode")?;
        if !matches!(
            mode,
            "latest-snapper"
                | "selected-snapper"
                | "create-snapper"
                | "latest-native"
                | "selected-native"
                | "create-native"
        ) {
            return Err("Unknown readonly snapshot provider/mode".into());
        }
        let name = req.name.as_deref().ok_or("Missing archive name")?;
        validate_name(name)?;
        args.extend([
            "--name".into(),
            name.to_owned(),
            "--profile-id".into(),
            id.to_owned(),
        ]);
        if mode.ends_with("-snapper") {
            let config = req
                .snapper_config
                .as_deref()
                .ok_or("Missing Snapper configuration")?;
            if config.is_empty()
                || config.len() > 80
                || !config
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
            {
                return Err("Invalid Snapper configuration name".into());
            }
            if mode == "selected-snapper" && req.snapper_number.unwrap_or(0) == 0 {
                return Err("Selected Snapper snapshot number required".into());
            }
            args.extend([
                "--source-mode".into(),
                mode.into(),
                "--snapper-config".into(),
                config.into(),
            ]);
            if mode == "selected-snapper" {
                args.extend([
                    "--snapper-number".into(),
                    req.snapper_number.unwrap().to_string(),
                ]);
            }
        } else {
            if req.snapper_config.is_some() || req.snapper_number.is_some() {
                return Err("Native snapshot mode cannot use Snapper identifiers".into());
            }
            let source = req
                .source
                .as_deref()
                .ok_or("Missing native Btrfs subvolume")?;
            if !Path::new(source).is_absolute()
                || source.contains('\0')
                || source.contains("//")
                || Path::new(source)
                    .components()
                    .any(|c| matches!(c, std::path::Component::ParentDir))
                || Path::new(source).is_symlink()
            {
                return Err("Native source must be a real absolute Btrfs subvolume".into());
            }
            args.extend([
                "--source-mode".into(),
                mode.into(),
                "--source".into(),
                source.into(),
            ]);
            if mode == "selected-native" {
                let name = req
                    .native_name
                    .as_deref()
                    .ok_or("Missing native snapshot name")?;
                if !valid_native_snapshot_name(name) {
                    return Err("Invalid native snapshot identifier".into());
                }
                args.extend(["--native-name".into(), name.into()]);
            }
        }
        let perf = req.performance.as_deref().unwrap_or("balanced");
        if !matches!(perf, "balanced" | "fast") {
            return Err("Unsupported performance mode".into());
        }
        args.extend(["--performance".into(), perf.into(), "--experimental".into()]);
    } else {
        let id = req
            .transfer_id
            .as_ref()
            .ok_or("Missing transfer identity")?;
        if Uuid::parse_str(id).is_err() || Uuid::parse_str(id).unwrap().to_string() != *id {
            return Err("Invalid transfer identity".into());
        }
        args.extend(["--transfer-id".into(), id.clone()]);
        if matches!(action, "resume" | "discard") {
            let name = req.name.as_deref().ok_or("Missing archive name")?;
            validate_name(name)?;
            args.extend(["--name".into(), name.into()]);
        }
        if action == "resume" {
            args.push("--experimental".into());
        }
        if action == "discard" {
            args.push("--confirm".into());
        }
    }
    if req.allow_local && matches!(action, "start" | "resume" | "discard") {
        args.push("--allow-local".into());
    }
    Ok(args)
}

fn set_action_arguments(req: &CheckpointRequest) -> Result<Vec<String>, String> {
    let action = req.action.as_str();
    if !matches!(
        action,
        "set-start" | "set-resume" | "set-status" | "set-restore" | "set-list"
    ) {
        return Err("Unsupported machine-set action".into());
    }
    let target = Path::new(&req.target);
    if !target.is_absolute()
        || req.target.contains('\0')
        || target.is_symlink()
        || !target.is_dir()
        || target
            .components()
            .any(|c| matches!(c, std::path::Component::ParentDir))
    {
        return Err("Existing real absolute machine-set destination required".into());
    }
    let mut args: Vec<String> = ["raw", "checkpoint-v2", action, "--target", &req.target]
        .iter()
        .map(|v| (*v).to_owned())
        .collect();
    if req.allow_local {
        args.push("--allow-local".into());
    }
    if !matches!(action, "set-status" | "set-list") {
        args.push("--experimental".into());
        args.extend(["--state-dir".into(), CONTROL_STATE_DIR.into()]);
    }
    if action == "set-start" {
        let profile = req.profile_id.as_deref().ok_or("Missing profile ID")?;
        Uuid::parse_str(profile).map_err(|_| "Invalid profile ID")?;
        args.extend(["--profile-id".into(), profile.into()]);
        if let Some(sources) = &req.sources {
            if sources.len() > 1024 {
                return Err("Too many selected Btrfs subvolumes".into());
            }
            for source in sources {
                let path = Path::new(source);
                if !path.is_absolute()
                    || source.contains('\0')
                    || path.is_symlink()
                    || path
                        .components()
                        .any(|c| matches!(c, std::path::Component::ParentDir))
                {
                    return Err("Invalid machine-set source".into());
                }
                args.extend(["--source".into(), source.clone()]);
            }
        }
    } else if action != "set-list" {
        let set_id = req.set_id.as_deref().ok_or("Missing machine-set ID")?;
        let parsed = Uuid::parse_str(set_id).map_err(|_| "Invalid machine-set ID")?;
        if parsed.to_string() != set_id {
            return Err("Noncanonical machine-set ID".into());
        }
        args.extend(["--set-id".into(), set_id.into()]);
    }
    if action == "set-restore" {
        let staging = req.staging.as_deref().ok_or("Missing restore staging")?;
        let path = Path::new(staging);
        if !path.is_absolute()
            || path.is_symlink()
            || !path.is_dir()
            || path
                .components()
                .any(|c| matches!(c, std::path::Component::ParentDir))
        {
            return Err("Restore staging must be an existing absolute directory".into());
        }
        args.extend(["--staging".into(), staging.into()]);
        if req.confirm {
            args.push("--confirm".into());
        }
    }
    Ok(args)
}

fn valid_native_snapshot_name(value: &str) -> bool {
    let Some(rest) = value.strip_prefix("harbor-") else {
        return false;
    };
    let Some((date, uid)) = rest.split_once('-') else {
        return false;
    };
    let Some(stamp) = date.strip_suffix('Z') else {
        return false;
    };
    let (seconds, nanos) = match stamp.split_once('.') {
        Some((seconds, nanos)) => (seconds, Some(nanos)),
        None => (stamp, None),
    };
    seconds.len() == 15
        && seconds.is_ascii()
        && seconds.as_bytes()[..8].iter().all(u8::is_ascii_digit)
        && seconds.as_bytes()[8] == b'T'
        && seconds.as_bytes()[9..15].iter().all(u8::is_ascii_digit)
        && nanos.is_none_or(|n| n.len() == 9 && n.bytes().all(|b| b.is_ascii_digit()))
        && uid.len() == 12
        && uid
            .bytes()
            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
}

fn validate_name(name: &str) -> Result<(), String> {
    if name.is_empty()
        || name.len() > 120
        || !name
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_')
    {
        Err("Archive name must contain only ASCII letters, digits, - or _".into())
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn native_source_modes_require_safe_paths_and_names() {
        let mut req = base("start");
        req.source_mode = Some("create-native".into());
        req.snapper_config = None;
        req.source = Some("/home".into());
        assert!(
            action_arguments(&req)
                .unwrap()
                .windows(2)
                .any(|v| v == ["--source", "/home"])
        );
        req.source = Some("../home".into());
        assert!(action_arguments(&req).is_err());
        req.source = Some("/home".into());
        req.source_mode = Some("selected-native".into());
        req.native_name = Some("harbor-20261009T120000Z-abcdef012345".into());
        assert!(action_arguments(&req).is_ok());
        req.native_name = Some("harbor-20261009T120000.123456789Z-abcdef012345".into());
        assert!(action_arguments(&req).is_ok());
        req.native_name = Some("harbor-20261009T120000.12Z-abcdef012345".into());
        assert!(action_arguments(&req).is_err());
        req.native_name = Some("../../etc/passwd".into());
        assert!(action_arguments(&req).is_err());
    }

    fn base(action: &str) -> CheckpointRequest {
        CheckpointRequest {
            action: action.into(),
            target: "/tmp".into(),
            name: Some("root_20261009".into()),
            profile_id: Some("b0cbeb30-9917-44d8-9f27-1ed967f91a2d".into()),
            transfer_id: Some("3b88e8c1-5cb8-4f67-aa10-f9544b6228f0".into()),
            source_mode: Some("latest-snapper".into()),
            snapper_config: Some("root".into()),
            snapper_number: None,
            source: None,
            native_name: None,
            allow_local: true,
            performance: None,
            set_id: Some("3b88e8c1-5cb8-4f67-aa10-f9544b6228f0".into()),
            sources: None,
            staging: None,
            confirm: false,
        }
    }
    #[test]
    fn machine_set_argument_validation_and_explicit_guards() {
        let mut req = base("set-start");
        req.sources = Some(vec!["/home".into(), "/srv".into()]);
        let cmd = action_arguments(&req).unwrap();
        assert!(cmd.contains(&"--experimental".into()));
        assert_eq!(cmd.iter().filter(|arg| *arg == "--source").count(), 2);
        req.sources = Some(vec!["/home/../etc".into()]);
        assert!(action_arguments(&req).is_err());

        let mut resume = base("set-resume");
        let cmd = action_arguments(&resume).unwrap();
        assert!(cmd.contains(&"--set-id".into()));
        resume.set_id = Some("../../etc/passwd".into());
        assert!(action_arguments(&resume).is_err());

        let mut restore = base("set-restore");
        restore.staging = Some("/tmp".into());
        restore.confirm = false;
        assert!(
            !action_arguments(&restore)
                .unwrap()
                .contains(&"--confirm".into())
        );
        restore.confirm = true;
        assert!(
            action_arguments(&restore)
                .unwrap()
                .contains(&"--confirm".into())
        );

        assert!(
            !action_arguments(&base("set-list"))
                .unwrap()
                .contains(&"--experimental".into())
        );
    }
    #[test]
    fn latest_snapper_requires_experimental_flag_and_fixed_state_dir() {
        let args = action_arguments(&base("start")).unwrap();
        assert!(args.contains(&"--experimental".into()));
        assert!(args.contains(&CONTROL_STATE_DIR.into()));
        assert!(args.contains(&"--snapper-config".into()));
    }
    #[test]
    fn code_injection_through_name_or_snapper_config_is_refused() {
        let mut a = base("start");
        a.name = Some("snap;rm".into());
        assert!(action_arguments(&a).is_err());
        a = base("start");
        a.snapper_config = Some("../../etc".into());
        assert!(action_arguments(&a).is_err());
    }
    #[test]
    fn stop_requests_are_scoped_to_canonical_uuid() {
        let mut a = base("stop");
        a.transfer_id = Some("hi--kill".into());
        assert!(action_arguments(&a).is_err());
        a.transfer_id = base("stop").transfer_id;
        let args = action_arguments(&a).unwrap();
        assert!(!args.iter().any(|s| s == "--experimental"));
    }
    #[test]
    fn resume_and_discard_maintain_explicit_gates() {
        let resume = action_arguments(&base("resume")).unwrap();
        assert!(resume.contains(&"--experimental".into()));
        let discard = action_arguments(&base("discard")).unwrap();
        assert!(discard.contains(&"--confirm".into()));
    }
}
