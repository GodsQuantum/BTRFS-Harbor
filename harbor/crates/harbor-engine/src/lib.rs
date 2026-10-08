//! Structured adapter between Harbor and the inherited btrfs-backup-ng engine.

use harbor_core::{ENGINE_STATUS_SCHEMA_VERSION, EnginePolicy, EngineStatusReport};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::process::{Command as StdCommand, ExitStatus};
use thiserror::Error;
use tokio::process::Command;

#[derive(Debug, Error)]
pub enum EngineError {
    #[error("failed to launch backup engine: {0}")]
    Launch(#[from] std::io::Error),
    #[error("backup engine returned invalid JSON: {0}")]
    InvalidJson(#[from] serde_json::Error),
    #[error("unsupported engine status schema {found}; expected {expected}")]
    SchemaMismatch { found: u32, expected: u32 },
    #[error("backup engine produced no machine-readable status (exit {code:?}): {stderr}")]
    MissingStatus { code: Option<i32>, stderr: String },
    #[error("no compatible btrfs-backup-ng engine is available")]
    NoCompatibleEngine,
    #[error("system btrfs-backup-ng is required by policy but was not found")]
    SystemEngineMissing,
    #[error("system btrfs-backup-ng {version} is below the required version {minimum}")]
    SystemEngineIncompatible { version: String, minimum: String },
}

pub const MIN_ENGINE_VERSION: (u64, u64, u64) = (0, 9, 12);

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum EngineOrigin {
    System,
    Bundled,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EngineResolution {
    pub executable: PathBuf,
    pub origin: EngineOrigin,
    pub version: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EngineCandidateStatus {
    pub executable: PathBuf,
    pub origin: EngineOrigin,
    pub version: Option<String>,
    pub compatible: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EngineSelectionStatus {
    pub policy: EnginePolicy,
    pub active: EngineResolution,
    pub system: Option<EngineCandidateStatus>,
    pub bundled: Option<EngineCandidateStatus>,
    pub minimum_version: String,
    pub fallback_reason: Option<String>,
}

pub fn parse_engine_version(banner: &str) -> Option<(u64, u64, u64)> {
    let token = banner
        .split_whitespace()
        .find(|part| part.chars().next().is_some_and(|ch| ch.is_ascii_digit()))?;
    let core = token.split_once('-').map_or(token, |(version, _)| version);
    let mut parts = core.split('.');
    Some((
        parts.next()?.parse().ok()?,
        parts.next()?.parse().ok()?,
        parts.next()?.parse().ok()?,
    ))
}

pub fn engine_version_is_compatible(banner: &str) -> bool {
    parse_engine_version(banner).is_some_and(|version| version >= MIN_ENGINE_VERSION)
}

fn minimum_version_string() -> String {
    format!(
        "{}.{}.{}",
        MIN_ENGINE_VERSION.0, MIN_ENGINE_VERSION.1, MIN_ENGINE_VERSION.2
    )
}

fn version_from_banner(banner: &str) -> Option<String> {
    banner
        .split_whitespace()
        .find(|part| parse_engine_version(part).is_some())
        .map(ToOwned::to_owned)
}

fn system_candidate(system: Option<(PathBuf, &str)>) -> Option<EngineCandidateStatus> {
    system.map(|(executable, banner)| EngineCandidateStatus {
        executable,
        origin: EngineOrigin::System,
        version: version_from_banner(banner),
        compatible: engine_version_is_compatible(banner),
    })
}

fn bundled_candidate(bundled: Option<PathBuf>) -> Option<EngineCandidateStatus> {
    bundled.map(|executable| EngineCandidateStatus {
        executable,
        origin: EngineOrigin::Bundled,
        version: Some(minimum_version_string()),
        compatible: true,
    })
}

fn candidate_resolution(candidate: &EngineCandidateStatus) -> EngineResolution {
    EngineResolution {
        executable: candidate.executable.clone(),
        origin: candidate.origin,
        version: candidate.version.clone(),
    }
}

pub fn resolve_engine_with_policy(
    policy: EnginePolicy,
    system: Option<(PathBuf, &str)>,
    bundled: Option<PathBuf>,
) -> Result<EngineSelectionStatus, EngineError> {
    let system = system_candidate(system);
    let bundled = bundled_candidate(bundled);
    let minimum_version = minimum_version_string();

    let (active, fallback_reason) = match policy {
        EnginePolicy::Auto => {
            if let Some(candidate) = system.as_ref().filter(|candidate| candidate.compatible) {
                (candidate_resolution(candidate), None)
            } else if let Some(candidate) = bundled.as_ref() {
                let reason = system.as_ref().map_or_else(
                    || "system engine not found; using Harbor bundled engine".to_owned(),
                    |system| {
                        format!(
                            "system engine {} is incompatible; using Harbor bundled engine",
                            system.version.as_deref().unwrap_or("unknown")
                        )
                    },
                );
                (candidate_resolution(candidate), Some(reason))
            } else {
                return Err(EngineError::NoCompatibleEngine);
            }
        }
        EnginePolicy::System => {
            let candidate = system.as_ref().ok_or(EngineError::SystemEngineMissing)?;
            if !candidate.compatible {
                return Err(EngineError::SystemEngineIncompatible {
                    version: candidate.version.as_deref().unwrap_or("unknown").to_owned(),
                    minimum: minimum_version,
                });
            }
            (candidate_resolution(candidate), None)
        }
        EnginePolicy::Bundled => {
            let candidate = bundled.as_ref().ok_or(EngineError::NoCompatibleEngine)?;
            (candidate_resolution(candidate), None)
        }
    };

    Ok(EngineSelectionStatus {
        policy,
        active,
        system,
        bundled,
        minimum_version,
        fallback_reason,
    })
}

pub fn select_engine_candidate(
    system: Option<(PathBuf, &str)>,
    bundled: Option<PathBuf>,
) -> Result<EngineResolution, EngineError> {
    resolve_engine_with_policy(EnginePolicy::Auto, system, bundled).map(|status| status.active)
}

fn executable_on_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}

pub fn discover_engine_with_policy(
    policy: EnginePolicy,
    bundled: Option<PathBuf>,
) -> Result<EngineSelectionStatus, EngineError> {
    let system = executable_on_path("btrfs-backup-ng").and_then(|path| {
        let output = StdCommand::new(&path).arg("--version").output().ok()?;
        if !output.status.success() {
            return None;
        }
        let banner = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        Some((path, banner))
    });
    let bundled = bundled.filter(|path| path.is_file());
    resolve_engine_with_policy(
        policy,
        system
            .as_ref()
            .map(|(path, banner)| (path.clone(), banner.as_str())),
        bundled,
    )
}

pub fn discover_engine(bundled: Option<PathBuf>) -> Result<EngineResolution, EngineError> {
    discover_engine_with_policy(EnginePolicy::Auto, bundled).map(|status| status.active)
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StatusInvocation {
    pub report: EngineStatusReport,
    pub exit_code: Option<i32>,
}

#[derive(Debug, Clone)]
pub struct EngineClient {
    executable: PathBuf,
}

impl Default for EngineClient {
    fn default() -> Self {
        Self::new("btrfs-backup-ng")
    }
}

impl EngineClient {
    pub fn new(executable: impl Into<PathBuf>) -> Self {
        Self {
            executable: executable.into(),
        }
    }

    pub async fn status(&self, config: Option<&Path>) -> Result<StatusInvocation, EngineError> {
        let mut command = Command::new(&self.executable);
        if let Some(config) = config {
            command.arg("-c").arg(config);
        }
        command.args(["status", "--format", "json"]);

        let output = command.output().await?;
        Self::decode_status(&output.stdout, &output.stderr, output.status)
    }

    pub fn decode_status(
        stdout: &[u8],
        stderr: &[u8],
        status: ExitStatus,
    ) -> Result<StatusInvocation, EngineError> {
        if stdout.is_empty() {
            return Err(EngineError::MissingStatus {
                code: status.code(),
                stderr: String::from_utf8_lossy(stderr).trim().to_owned(),
            });
        }

        let report: EngineStatusReport = serde_json::from_slice(stdout)?;
        if report.schema_version != ENGINE_STATUS_SCHEMA_VERSION {
            return Err(EngineError::SchemaMismatch {
                found: report.schema_version,
                expected: ENGINE_STATUS_SCHEMA_VERSION,
            });
        }

        Ok(StatusInvocation {
            report,
            exit_code: status.code(),
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use harbor_core::ProtectionState;
    #[cfg(not(unix))]
    use std::process::Command as StdCommand;

    fn exit_status(code: i32) -> ExitStatus {
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            ExitStatus::from_raw(code << 8)
        }
        #[cfg(not(unix))]
        {
            StdCommand::new("cmd")
                .args(["/C", &format!("exit {code}")])
                .status()
                .unwrap()
        }
    }

    #[test]
    fn parses_healthy_status_fixture() {
        let raw = include_bytes!("../fixtures/status-healthy.json");
        let invocation = EngineClient::decode_status(raw, b"", exit_status(0)).unwrap();

        assert_eq!(invocation.exit_code, Some(0));
        assert!(invocation.report.healthy);
        assert_eq!(
            invocation.report.protection_state(),
            ProtectionState::Protected
        );
    }

    #[test]
    fn preserves_degraded_report_even_when_engine_exits_one() {
        let raw = include_bytes!("../fixtures/status-degraded.json");
        let invocation = EngineClient::decode_status(raw, b"", exit_status(1)).unwrap();

        assert_eq!(invocation.exit_code, Some(1));
        assert!(!invocation.report.healthy);
        assert_eq!(
            invocation.report.protection_state(),
            ProtectionState::AtRisk
        );
    }

    #[test]
    fn rejects_unknown_schema() {
        let raw = br#"{"schema_version":99,"healthy":false,"volumes":[]}"#;
        let err = EngineClient::decode_status(raw, b"", exit_status(1)).unwrap_err();

        assert!(matches!(
            err,
            EngineError::SchemaMismatch {
                found: 99,
                expected: ENGINE_STATUS_SCHEMA_VERSION
            }
        ));
    }

    #[test]
    fn compatible_system_engine_is_preferred_over_bundled_engine() {
        let selected = select_engine_candidate(
            Some((
                PathBuf::from("/usr/bin/btrfs-backup-ng"),
                "btrfs-backup-ng 0.9.12",
            )),
            Some(PathBuf::from("/app/resources/portable/btrfs-backup-ng")),
        )
        .unwrap();

        assert_eq!(selected.origin, EngineOrigin::System);
        assert_eq!(
            selected.executable,
            PathBuf::from("/usr/bin/btrfs-backup-ng")
        );
    }

    #[test]
    fn incompatible_system_engine_falls_back_to_bundled_engine() {
        let selected = select_engine_candidate(
            Some((
                PathBuf::from("/usr/bin/btrfs-backup-ng"),
                "btrfs-backup-ng 0.8.9",
            )),
            Some(PathBuf::from("/app/resources/portable/btrfs-backup-ng")),
        )
        .unwrap();

        assert_eq!(selected.origin, EngineOrigin::Bundled);
        assert_eq!(
            selected.executable,
            PathBuf::from("/app/resources/portable/btrfs-backup-ng")
        );
    }

    #[test]
    fn engine_policy_auto_prefers_compatible_system_and_reports_both_candidates() {
        let status = resolve_engine_with_policy(
            harbor_core::EnginePolicy::Auto,
            Some((
                PathBuf::from("/usr/bin/btrfs-backup-ng"),
                "btrfs-backup-ng 0.9.12",
            )),
            Some(PathBuf::from("/app/portable/btrfs-backup-ng")),
        )
        .unwrap();

        assert_eq!(status.policy, harbor_core::EnginePolicy::Auto);
        assert_eq!(status.active.origin, EngineOrigin::System);
        assert_eq!(status.active.version.as_deref(), Some("0.9.12"));
        assert!(
            status
                .system
                .as_ref()
                .is_some_and(|candidate| candidate.compatible)
        );
        assert!(
            status
                .bundled
                .as_ref()
                .is_some_and(|candidate| candidate.compatible)
        );
        assert_eq!(status.minimum_version, "0.9.12");
        assert_eq!(status.fallback_reason, None);
    }

    #[test]
    fn engine_policy_system_rejects_incompatible_system_instead_of_falling_back() {
        let err = resolve_engine_with_policy(
            harbor_core::EnginePolicy::System,
            Some((
                PathBuf::from("/usr/bin/btrfs-backup-ng"),
                "btrfs-backup-ng 0.9.11",
            )),
            Some(PathBuf::from("/app/portable/btrfs-backup-ng")),
        )
        .unwrap_err();

        assert!(matches!(err, EngineError::SystemEngineIncompatible { .. }));
    }

    #[test]
    fn engine_policy_bundled_ignores_newer_system_engine() {
        let status = resolve_engine_with_policy(
            harbor_core::EnginePolicy::Bundled,
            Some((
                PathBuf::from("/usr/bin/btrfs-backup-ng"),
                "btrfs-backup-ng 0.10.1",
            )),
            Some(PathBuf::from("/app/portable/btrfs-backup-ng")),
        )
        .unwrap();

        assert_eq!(status.active.origin, EngineOrigin::Bundled);
        assert_eq!(
            status.active.executable,
            PathBuf::from("/app/portable/btrfs-backup-ng")
        );
        assert_eq!(
            status
                .system
                .as_ref()
                .and_then(|candidate| candidate.version.as_deref()),
            Some("0.10.1")
        );
    }

    #[test]
    fn engine_version_parser_accepts_supported_and_newer_versions() {
        assert_eq!(
            parse_engine_version("btrfs-backup-ng 0.9.12"),
            Some((0, 9, 12))
        );
        assert!(engine_version_is_compatible("btrfs-backup-ng 0.10.1"));
        assert!(!engine_version_is_compatible("btrfs-backup-ng 0.9.11"));
    }
}
