//! Structured adapter between Harbor and the inherited btrfs-backup-ng engine.

use harbor_core::{ENGINE_STATUS_SCHEMA_VERSION, EngineStatusReport};
use std::path::{Path, PathBuf};
use std::process::ExitStatus;
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
}
