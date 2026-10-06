use anyhow::Result;
use harbor_core::RecoveryScope;
use harbor_recovery::{RecoveryRequest, plan_recovery};
use std::path::PathBuf;

fn main() -> Result<()> {
    let request = RecoveryRequest {
        scope: RecoveryScope::System,
        restore_point: "latest-verified".to_string(),
        sources: vec![PathBuf::from("@"), PathBuf::from("@home")],
        staging_root: PathBuf::from("/mnt/btrfs-harbor-staging"),
        running_root_included: true,
    };
    let plan = plan_recovery(&request);
    println!("{}", serde_json::to_string_pretty(&plan)?);
    Ok(())
}
