mod discovery;
mod runtime;

use anyhow::Result;
use harbor_engine::EngineClient;
use harbor_storage::HarborConfig;
use std::path::PathBuf;
use uuid::Uuid;
use zbus::{Connection, interface};

const BUS_NAME: &str = "io.github.GodsQuantum.BtrfsHarbor1";
const OBJECT_PATH: &str = "/io/github/GodsQuantum/BtrfsHarbor1";
const CONFIG_PATH: &str = "/etc/btrfs-harbor/harbor.toml";
const GENERATED_DIR: &str = "/var/lib/btrfs-harbor/generated";

struct HarborAgent {
    engine: EngineClient,
}

impl Default for HarborAgent {
    fn default() -> Self {
        let executable = std::env::var_os("BTRFS_HARBOR_ENGINE")
            .map(PathBuf::from)
            .unwrap_or_else(|| "btrfs-backup-ng".into());
        Self {
            engine: EngineClient::new(executable),
        }
    }
}

fn parse_profile_id(profile_id: &str) -> zbus::fdo::Result<Uuid> {
    Uuid::parse_str(profile_id)
        .map_err(|err| zbus::fdo::Error::InvalidArgs(format!("invalid profile UUID: {err}")))
}

fn profile_config_path(profile_id: &str) -> zbus::fdo::Result<PathBuf> {
    let id = parse_profile_id(profile_id)?;
    Ok(PathBuf::from(GENERATED_DIR).join(format!("{id}.toml")))
}

fn read_harbor_config() -> zbus::fdo::Result<HarborConfig> {
    let raw = std::fs::read_to_string(CONFIG_PATH).map_err(|err| {
        zbus::fdo::Error::FileNotFound(format!("cannot read {CONFIG_PATH}: {err}"))
    })?;
    let config =
        HarborConfig::from_toml(&raw).map_err(|err| zbus::fdo::Error::Failed(err.to_string()))?;
    config
        .validate()
        .map_err(|err| zbus::fdo::Error::Failed(format!("invalid Harbor configuration: {err}")))?;
    Ok(config)
}

fn configuration_json(config: &HarborConfig) -> zbus::fdo::Result<String> {
    serde_json::to_string(config).map_err(|err| zbus::fdo::Error::Failed(err.to_string()))
}

fn configured_profiles() -> zbus::fdo::Result<String> {
    let config = read_harbor_config()?;

    let summaries = config
        .profiles
        .iter()
        .map(|profile| {
            serde_json::json!({
                "id": profile.id,
                "name": profile.name,
                "on_calendar": profile.on_calendar,
                "verify_after_backup": profile.verify_after_backup,
            })
        })
        .collect::<Vec<_>>();

    serde_json::to_string(&summaries).map_err(|err| zbus::fdo::Error::Failed(err.to_string()))
}

#[interface(name = "io.github.GodsQuantum.BtrfsHarbor1")]
impl HarborAgent {
    async fn version(&self) -> String {
        env!("CARGO_PKG_VERSION").to_string()
    }

    async fn system_summary(&self) -> String {
        serde_json::json!({
            "schema_version": 1,
            "agent_version": env!("CARGO_PKG_VERSION"),
            "service": "btrfs-harbor-agent",
        })
        .to_string()
    }

    async fn profiles(&self) -> zbus::fdo::Result<String> {
        configured_profiles()
    }

    async fn configuration(&self) -> zbus::fdo::Result<String> {
        let config = read_harbor_config()?;
        configuration_json(&config)
    }

    async fn profile_runtime(&self, profile_id: &str) -> zbus::fdo::Result<String> {
        let id = parse_profile_id(profile_id)?;
        let status = runtime::inspect_profile_runtime(id)
            .await
            .map_err(|err| zbus::fdo::Error::Failed(err.to_string()))?;
        serde_json::to_string(&status).map_err(|err| zbus::fdo::Error::Failed(err.to_string()))
    }

    async fn discover_sources(&self) -> zbus::fdo::Result<String> {
        let sources = discovery::discover_sources()
            .await
            .map_err(|err| zbus::fdo::Error::Failed(err.to_string()))?;
        serde_json::to_string(&sources).map_err(|err| zbus::fdo::Error::Failed(err.to_string()))
    }

    async fn backup_status(&self, profile_id: &str) -> zbus::fdo::Result<String> {
        let config_path = profile_config_path(profile_id)?;
        if !config_path.is_file() {
            return Err(zbus::fdo::Error::FileNotFound(format!(
                "profile {profile_id} is not installed"
            )));
        }

        let status = self
            .engine
            .status(Some(&config_path))
            .await
            .map_err(|err| zbus::fdo::Error::Failed(err.to_string()))?;

        serde_json::to_string(&status.report)
            .map_err(|err| zbus::fdo::Error::Failed(err.to_string()))
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    let connection = Connection::system().await?;
    connection.request_name(BUS_NAME).await?;
    connection
        .object_server()
        .at(OBJECT_PATH, HarborAgent::default())
        .await?;

    std::future::pending::<()>().await;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn configuration_json_roundtrips_typed_config() {
        let config = HarborConfig::default();
        let raw = configuration_json(&config).unwrap();
        let decoded: HarborConfig = serde_json::from_str(&raw).unwrap();

        assert_eq!(decoded, config);
    }

    #[test]
    fn profile_ids_cannot_escape_generated_directory() {
        assert!(profile_config_path("../../etc/shadow").is_err());
        let path = profile_config_path("11111111-1111-4111-8111-111111111111").expect("valid UUID");
        assert_eq!(
            path,
            PathBuf::from(
                "/var/lib/btrfs-harbor/generated/11111111-1111-4111-8111-111111111111.toml"
            )
        );
    }
}
