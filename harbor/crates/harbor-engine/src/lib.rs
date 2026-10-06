//! Structured adapter between Harbor and the inherited btrfs-backup-ng engine.

use harbor_core::{ENGINE_STATUS_SCHEMA_VERSION, EngineStatusReport};
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
}

pub const MIN_ENGINE_VERSION: (u64, u64, u64) = (0, 9, 12);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum EngineOrigin {
    System,
    Bundled,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct EngineResolution {
    pub executable: PathBuf,
    pub origin: EngineOrigin,
    pub version: Option<String>,
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

pub fn select_engine_candidate(
    system: Option<(PathBuf, &str)>,
    bundled: Option<PathBuf>,
) -> Result<EngineResolution, EngineError> {
    if let Some((executable, banner)) = system
        && engine_version_is_compatible(banner)
    {
        return Ok(EngineResolution {
            executable,
            origin: EngineOrigin::System,
            version: banner
                .split_whitespace()
                .find(|part| parse_engine_version(part).is_some())
                .map(ToOwned::to_owned),
        });
    }

    bundled
        .map(|executable| EngineResolution {
            executable,
            origin: EngineOrigin::Bundled,
            version: Some(format!(
                "{}.{}.{}",
                MIN_ENGINE_VERSION.0, MIN_ENGINE_VERSION.1, MIN_ENGINE_VERSION.2
            )),
        })
        .ok_or(EngineError::NoCompatibleEngine)
}

fn executable_on_path(name: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    std::env::split_paths(&path)
        .map(|dir| dir.join(name))
        .find(|candidate| candidate.is_file())
}

pub fn discover_engine(bundled: Option<PathBuf>) -> Result<EngineResolution, EngineError> {
    let system = executable_on_path("btrfs-backup-ng").and_then(|path| {
        let output = StdCommand::new(&path).arg("--version").output().ok()?;
        if !output.status.success() {
            return None;
        }
        let banner = String::from_utf8_lossy(&output.stdout).trim().to_owned();
        Some((path, banner))
    });
    let bundled = bundled.filter(|path| path.is_file());
    select_engine_candidate(
        system
            .as_ref()
            .map(|(path, banner)| (path.clone(), banner.as_str())),
        bundled,
    )
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
    fn engine_version_parser_accepts_supported_and_newer_versions() {
        assert_eq!(
            parse_engine_version("btrfs-backup-ng 0.9.12"),
            Some((0, 9, 12))
        );
        assert!(engine_version_is_compatible("btrfs-backup-ng 0.10.1"));
        assert!(!engine_version_is_compatible("btrfs-backup-ng 0.9.11"));
    }
}
