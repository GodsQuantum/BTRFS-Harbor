//! Read-only checkpoint timeline, never a privileged transfer control.
//! User-owned mounted directory only. Invalid, symlink or oversized manifests
//! are ignored and never interpreted as verified backup/restore points.

use serde::Serialize;
use serde_json::Value;
use std::fs;
use std::path::Path;
use uuid::Uuid;

const MAX_ENTRIES: usize = 2048;
const MAX_MANIFEST_BYTES: u64 = 16 * 1024 * 1024;

#[derive(Debug, Serialize, PartialEq, Eq)]
pub struct CheckpointStatus {
    pub transfer_id: String,
    pub state: String,
    pub source: String,
    pub checkpoint_count: usize,
    pub committed_raw_bytes: u64,
    pub committed_compressed_bytes: u64,
    pub last_checkpoint_at: Option<String>,
    pub resumable: bool,
    // The dashboard must never call an unchecked index "verified".
    pub verified: bool,
}

fn parse_checkpoint_file(path: &Path, expected_id: &str) -> Option<CheckpointStatus> {
    let metadata = fs::symlink_metadata(path).ok()?;
    if !metadata.file_type().is_file() || metadata.len() > MAX_MANIFEST_BYTES {
        return None;
    }
    let data: Value = serde_json::from_slice(&fs::read(path).ok()?).ok()?;
    if data.get("schema_version")?.as_u64()? != 2
        || data.get("transfer_id")?.as_str()? != expected_id
    {
        return None;
    }
    let state = data.get("state")?.as_str()?;
    if !matches!(
        state,
        "preparing"
            | "uploading"
            | "pause_requested"
            | "paused"
            | "replaying"
            | "failed_resumable"
            | "finalizing"
            | "completed"
            | "discarded"
    ) {
        return None;
    }
    let checkpoints = data.get("checkpoints")?.as_array()?;
    let source = data.get("identity")?.get("source_path")?.as_str()?;
    let committed_raw_bytes = data.get("committed_raw_bytes")?.as_u64()?;
    let committed_compressed_bytes = data.get("committed_compressed_bytes")?.as_u64()?;
    let last_checkpoint_at = checkpoints
        .last()
        .and_then(|cp| cp.get("committed_at"))
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    Some(CheckpointStatus {
        transfer_id: expected_id.to_owned(),
        state: state.to_owned(),
        source: source.to_owned(),
        checkpoint_count: checkpoints.len(),
        committed_raw_bytes,
        committed_compressed_bytes,
        last_checkpoint_at,
        resumable: matches!(
            state,
            "preparing"
                | "uploading"
                | "pause_requested"
                | "paused"
                | "replaying"
                | "failed_resumable"
                | "finalizing"
        ),
        verified: false,
    })
}

pub fn list_checkpoint_manifests(target: &Path) -> Result<Vec<CheckpointStatus>, String> {
    if !target.is_absolute() {
        return Err("Checkpoint target must be an absolute directory".into());
    }
    let metadata = fs::symlink_metadata(target).map_err(|e| e.to_string())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err("Checkpoint target is not a real directory".into());
    }
    let entries = fs::read_dir(target).map_err(|e| e.to_string())?;
    let mut results = Vec::new();
    for entry in entries.take(MAX_ENTRIES) {
        let entry = entry.map_err(|e| e.to_string())?;
        let file_name = entry.file_name();
        let Some(name) = file_name.to_str() else {
            continue;
        };
        let Some(id) = name
            .strip_prefix(".harbor-resume-")
            .and_then(|s| s.strip_suffix(".json"))
        else {
            continue;
        };
        let Ok(uuid) = Uuid::parse_str(id) else {
            continue;
        };
        if uuid.to_string() != id {
            continue;
        }
        if let Some(record) = parse_checkpoint_file(&entry.path(), id) {
            results.push(record);
        }
        if results.len() >= 100 {
            break;
        }
    }
    results.sort_by(|a, b| b.last_checkpoint_at.cmp(&a.last_checkpoint_at));
    Ok(results)
}

#[cfg(test)]
mod tests {
    use super::*;

    const ID: &str = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0";

    fn write_fixture(directory: &Path, filename_id: &str, document_id: &str) {
        let file = directory.join(format!(".harbor-resume-{filename_id}.json"));
        let body = serde_json::json!({
            "schema_version": 2,
            "transfer_id": document_id,
            "state": "paused",
            "identity": {"source_path": "/.snapshots/12/snapshot"},
            "committed_raw_bytes": 128,
            "committed_compressed_bytes": 64,
            "checkpoints": [
                {"committed_at": "2026-10-09T00:00:00Z"}
            ]
        });
        fs::write(file, body.to_string()).expect("write temporary fixture");
    }

    #[test]
    fn read_only_timeline_reports_unverified_checkpoint_metadata() {
        let temp = tempfile::tempdir().unwrap();
        write_fixture(temp.path(), ID, ID);
        let found = list_checkpoint_manifests(temp.path()).unwrap();
        assert_eq!(found.len(), 1);
        assert_eq!(found[0].committed_raw_bytes, 128);
        assert_eq!(found[0].checkpoint_count, 1);
        assert!(found[0].resumable);
        assert!(!found[0].verified);
    }

    #[test]
    fn mismatched_transfer_id_is_excluded() {
        let temp = tempfile::tempdir().unwrap();
        write_fixture(temp.path(), ID, "no-trust");
        assert!(list_checkpoint_manifests(temp.path()).unwrap().is_empty());
    }

    #[cfg(unix)]
    #[test]
    fn symlink_manifest_is_not_followed() {
        use std::os::unix::fs::symlink;
        let temp = tempfile::tempdir().unwrap();
        let outside = temp.path().join("foreign");
        fs::write(&outside, b"protected").unwrap();
        symlink(
            &outside,
            temp.path().join(format!(".harbor-resume-{ID}.json")),
        )
        .unwrap();
        assert!(list_checkpoint_manifests(temp.path()).unwrap().is_empty());
    }

    #[test]
    fn non_directory_target_is_rejected_without_creating_anything() {
        let temp = tempfile::tempdir().unwrap();
        let other = temp.path().join("does-not-exist");
        assert!(list_checkpoint_manifests(&other).is_err());
        assert!(!other.exists());
    }
}
