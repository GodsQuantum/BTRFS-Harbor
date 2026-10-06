use anyhow::{Context, Result, bail};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, HashMap};
use std::env;
use std::path::PathBuf;
use tokio::process::Command;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SourceHint {
    Recommended,
    UsuallyDisposable,
    Optional,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DiscoveredSource {
    pub mount_point: String,
    pub source: String,
    pub subvolume: Option<String>,
    pub snapper_config: Option<String>,
    pub hint: SourceHint,
}

#[derive(Debug, Deserialize)]
struct FindmntDocument {
    #[serde(default)]
    filesystems: Vec<FindmntNode>,
}

#[derive(Debug, Deserialize)]
struct FindmntNode {
    #[serde(default)]
    target: Option<String>,
    #[serde(default)]
    source: Option<String>,
    #[serde(default)]
    fstype: Option<String>,
    #[serde(default)]
    options: Option<String>,
    #[serde(default)]
    children: Vec<FindmntNode>,
}

fn findmnt_executable() -> PathBuf {
    env::var_os("BTRFS_HARBOR_FINDMNT")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("findmnt"))
}

fn snapper_executable() -> PathBuf {
    env::var_os("BTRFS_HARBOR_SNAPPER")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("snapper"))
}

fn source_hint(path: &str) -> SourceHint {
    match path {
        "/" | "/home" | "/root" | "/srv" => SourceHint::Recommended,
        "/tmp" | "/var/tmp" | "/var/cache" | "/var/log" => SourceHint::UsuallyDisposable,
        _ => SourceHint::Optional,
    }
}

fn subvolume_from(options: Option<&str>, source: &str) -> Option<String> {
    if let Some(options) = options
        && let Some(raw) = options
            .split(',')
            .find_map(|option| option.strip_prefix("subvol="))
    {
        let value = raw.trim().trim_start_matches('/');
        if !value.is_empty() {
            return Some(value.to_owned());
        }
    }

    let (_, suffix) = source.rsplit_once('[')?;
    let value = suffix.strip_suffix(']')?.trim().trim_start_matches('/');
    (!value.is_empty()).then(|| value.to_owned())
}

fn collect_findmnt(node: FindmntNode, out: &mut Vec<DiscoveredSource>) {
    let FindmntNode {
        target,
        source,
        fstype,
        options,
        children,
    } = node;

    if fstype.as_deref() == Some("btrfs")
        && let (Some(target), Some(source)) = (target, source)
    {
        out.push(DiscoveredSource {
            hint: source_hint(&target),
            subvolume: subvolume_from(options.as_deref(), &source),
            mount_point: target,
            source,
            snapper_config: None,
        });
    }

    for child in children {
        collect_findmnt(child, out);
    }
}

pub fn parse_findmnt_sources(raw: &str) -> Result<Vec<DiscoveredSource>> {
    let document: FindmntDocument =
        serde_json::from_str(raw).context("invalid findmnt JSON output")?;
    let mut sources = Vec::new();
    for node in document.filesystems {
        collect_findmnt(node, &mut sources);
    }

    let mut by_target = BTreeMap::new();
    for source in sources {
        by_target.insert(source.mount_point.clone(), source);
    }
    Ok(by_target.into_values().collect())
}

fn collect_snapper_pairs(
    value: &Value,
    inherited_key: Option<&str>,
    out: &mut HashMap<String, String>,
) {
    match value {
        Value::Array(items) => {
            for item in items {
                collect_snapper_pairs(item, inherited_key, out);
            }
        }
        Value::Object(map) => {
            let config = map.get("config").and_then(Value::as_str);
            let subvolume = map.get("subvolume").and_then(Value::as_str);
            if let Some(subvolume) = subvolume
                && let Some(config) = config.or(inherited_key)
            {
                out.entry(subvolume.to_owned())
                    .or_insert_with(|| config.to_owned());
            }

            for (key, child) in map {
                if key == "config" || key == "subvolume" {
                    continue;
                }
                collect_snapper_pairs(child, Some(key), out);
            }
        }
        _ => {}
    }
}

pub fn parse_snapper_configs(raw: &str) -> Result<HashMap<String, String>> {
    let value: Value = serde_json::from_str(raw).context("invalid snapper JSON output")?;
    let mut configs = HashMap::new();
    collect_snapper_pairs(&value, None, &mut configs);
    Ok(configs)
}

pub fn attach_snapper_configs(sources: &mut [DiscoveredSource], configs: &HashMap<String, String>) {
    for source in sources {
        source.snapper_config = configs.get(&source.mount_point).cloned();
    }
}

pub async fn discover_sources() -> Result<Vec<DiscoveredSource>> {
    let output = Command::new(findmnt_executable())
        .args([
            "--json",
            "--types",
            "btrfs",
            "--output",
            "TARGET,SOURCE,FSTYPE,OPTIONS",
        ])
        .output()
        .await
        .context("cannot execute findmnt for Btrfs discovery")?;

    if !output.status.success() {
        bail!(
            "findmnt Btrfs discovery failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let mut sources = parse_findmnt_sources(&String::from_utf8_lossy(&output.stdout))?;

    let snapper = Command::new(snapper_executable())
        .args(["--jsonout", "list-configs", "--columns", "config,subvolume"])
        .output()
        .await;

    if let Ok(output) = snapper
        && output.status.success()
        && let Ok(configs) = parse_snapper_configs(&String::from_utf8_lossy(&output.stdout))
    {
        attach_snapper_configs(&mut sources, &configs);
    }

    sources.sort_by_key(|source| {
        let hint = match source.hint {
            SourceHint::Recommended => 0,
            SourceHint::Optional => 1,
            SourceHint::UsuallyDisposable => 2,
        };
        (hint, source.mount_point.clone())
    });

    Ok(sources)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_active_btrfs_mounts_and_subvolume_names() {
        let raw = r#"{
          "filesystems": [
            {
              "target": "/",
              "source": "/dev/mapper/root[/@]",
              "fstype": "btrfs",
              "options": "rw,relatime,subvol=/@",
              "children": [
                {
                  "target": "/home",
                  "source": "/dev/mapper/root[/@home]",
                  "fstype": "btrfs",
                  "options": "rw,relatime,subvol=/@home"
                },
                {
                  "target": "/var/cache",
                  "source": "/dev/mapper/root[/@cache]",
                  "fstype": "btrfs",
                  "options": "rw,relatime,subvol=/@cache"
                }
              ]
            },
            {
              "target": "/boot",
              "source": "/dev/nvme0n1p1",
              "fstype": "vfat",
              "options": "rw"
            }
          ]
        }"#;

        let sources = parse_findmnt_sources(raw).unwrap();

        assert_eq!(sources.len(), 3);
        let root = sources
            .iter()
            .find(|source| source.mount_point == "/")
            .unwrap();
        assert_eq!(root.subvolume.as_deref(), Some("@"));
        assert_eq!(root.hint, SourceHint::Recommended);
        let cache = sources
            .iter()
            .find(|source| source.mount_point == "/var/cache")
            .unwrap();
        assert_eq!(cache.hint, SourceHint::UsuallyDisposable);
    }

    #[test]
    fn parses_snapper_list_configs_in_array_shape() {
        let configs = parse_snapper_configs(
            r#"[{"config":"root","subvolume":"/"},{"config":"home","subvolume":"/home"}]"#,
        )
        .unwrap();

        assert_eq!(configs.get("/").map(String::as_str), Some("root"));
        assert_eq!(configs.get("/home").map(String::as_str), Some("home"));
    }

    #[test]
    fn parses_snapper_list_configs_in_keyed_shape() {
        let configs =
            parse_snapper_configs(r#"{"root":[{"subvolume":"/"}],"home":[{"subvolume":"/home"}]}"#)
                .unwrap();

        assert_eq!(configs.get("/").map(String::as_str), Some("root"));
        assert_eq!(configs.get("/home").map(String::as_str), Some("home"));
    }

    #[test]
    fn attaches_snapper_config_by_mount_point() {
        let mut sources = vec![DiscoveredSource {
            mount_point: "/".into(),
            source: "/dev/mapper/root[/@]".into(),
            subvolume: Some("@".into()),
            snapper_config: None,
            hint: SourceHint::Recommended,
        }];
        let configs = HashMap::from([("/".into(), "root".into())]);

        attach_snapper_configs(&mut sources, &configs);

        assert_eq!(sources[0].snapper_config.as_deref(), Some("root"));
    }
}
