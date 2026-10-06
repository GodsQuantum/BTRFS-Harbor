use anyhow::{Context, Result, bail};
use harbor_replica::{
    LXC_TARGET_PRESERVE_PATHS, LxcIdMap, LxcReplicaRequest, lxc_copy_exclusions, parse_lxc_idmap,
    usernsexec_map_args, validate_lxc_request, validate_stopped_status,
};
use std::fs::{self, OpenOptions};
use std::io::Read;
#[cfg(unix)]
use std::os::unix::fs::MetadataExt;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use uuid::Uuid;

use crate::emit_progress;

fn debug_executable_override(variable: &str, default: &str) -> PathBuf {
    #[cfg(debug_assertions)]
    if let Some(value) = std::env::var_os(variable) {
        return PathBuf::from(value);
    }

    let _ = variable;
    PathBuf::from(default)
}

fn pct_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_PCT", "/usr/sbin/pct")
}

fn btrfs_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_BTRFS", "/usr/bin/btrfs")
}

fn findmnt_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_FINDMNT", "/usr/bin/findmnt")
}

fn tar_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_TAR", "/usr/bin/tar")
}

fn lxc_usernsexec_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_LXC_USERNSEXEC", "/usr/bin/lxc-usernsexec")
}

fn cp_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_CP", "/usr/bin/cp")
}

fn sync_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_SYNC", "/usr/bin/sync")
}

fn ssh_keygen_executable() -> PathBuf {
    debug_executable_override("BTRFS_HARBOR_SSH_KEYGEN", "/usr/bin/ssh-keygen")
}

fn test_root_override_enabled() -> bool {
    #[cfg(debug_assertions)]
    {
        std::env::var_os("BTRFS_HARBOR_ALLOW_TEST_ROOT").is_some()
    }

    #[cfg(not(debug_assertions))]
    {
        false
    }
}

pub fn run_lxc_replica_jsonl() -> Result<()> {
    ensure_root()?;
    emit_progress("phase", "replica_start", "Preparing LXC replica", None)?;

    let result = deploy_lxc_replica();
    if let Err(err) = &result {
        let _ = emit_progress("failed", "replica_error", &err.to_string(), None);
    }
    result
}

fn deploy_lxc_replica() -> Result<()> {
    let request = read_request()?;
    validate_lxc_request(&request).context("invalid LXC replica request")?;

    let source = fs::canonicalize(&request.staging_path).with_context(|| {
        format!(
            "cannot resolve staging path {}",
            request.staging_path.display()
        )
    })?;
    require_read_only_btrfs_subvolume(&source)?;

    emit_progress(
        "phase",
        "replica_preflight",
        &format!("Checking stopped LXC {}", request.vmid),
        None,
    )?;

    let status = pct_output(&["status", &request.vmid.to_string()])?;
    validate_stopped_status(&status).context("unsafe LXC state")?;

    let config = pct_output(&["config", &request.vmid.to_string()])?;
    let idmap = parse_lxc_idmap(&config).context("invalid LXC uid/gid mapping")?;

    let snapshot_name = if request.create_rollback_snapshot {
        let suffix = Uuid::new_v4().simple().to_string();
        let name = format!("harbor-pre-replica-{}", &suffix[..8]);
        emit_progress(
            "phase",
            "replica_snapshot",
            &format!("Creating rollback snapshot {name}"),
            None,
        )?;
        pct_status(&[
            "snapshot",
            &request.vmid.to_string(),
            &name,
            "--description",
            "Btrfs Harbor pre-replica rollback point",
        ])?;
        Some(name)
    } else {
        None
    };

    let workdir =
        PathBuf::from("/run/btrfs-harbor").join(format!("replica-{}", Uuid::new_v4().simple()));
    fs::create_dir_all(&workdir)
        .with_context(|| format!("cannot create replica workdir {}", workdir.display()))?;
    set_private_mode(&workdir)?;

    let mut mounted = false;
    let deploy_result = (|| -> Result<()> {
        emit_progress(
            "phase",
            "replica_mount",
            &format!("Mounting stopped LXC {}", request.vmid),
            None,
        )?;
        let mount_output = pct_output(&["mount", &request.vmid.to_string()])?;
        mounted = true;
        let target_root = parse_pct_mount_path(request.vmid, &mount_output)?;
        validate_target_root(request.vmid, &target_root, &source)?;

        let nested_mounts = nested_mounts_under(&target_root)?;
        let copy_exclusions = lxc_copy_exclusions(
            &nested_mounts
                .iter()
                .filter_map(|path| {
                    path.strip_prefix(&target_root)
                        .ok()
                        .map(|relative| Path::new("/").join(relative))
                })
                .collect::<Vec<_>>(),
        );

        emit_progress(
            "phase",
            "replica_preserve",
            "Preserving target network and identity files",
            None,
        )?;
        let preserve_root = workdir.join("preserve");
        preserve_target_identity(&target_root, &preserve_root)?;

        emit_progress(
            "phase",
            "replica_clear",
            "Clearing target rootfs without crossing nested mounts",
            None,
        )?;
        clear_rootfs(&target_root, &nested_mounts)?;

        emit_progress(
            "phase",
            "replica_copy",
            "Transplanting userspace into target namespace",
            None,
        )?;
        copy_userspace(&source, &target_root, &copy_exclusions, idmap.as_ref())?;

        restore_target_identity(&target_root, &preserve_root)?;
        reset_machine_identity(&target_root)?;
        if request.regenerate_ssh_host_keys {
            regenerate_ssh_host_keys(&target_root, idmap.as_ref())?;
        }

        let sync_status = Command::new(sync_executable())
            .arg("-f")
            .arg(&target_root)
            .status()
            .context("cannot flush target rootfs")?;
        if !sync_status.success() {
            bail!("sync -f failed with {sync_status}");
        }

        emit_progress(
            "phase",
            "replica_unmount",
            &format!("Unmounting LXC {}", request.vmid),
            None,
        )?;
        pct_status(&["unmount", &request.vmid.to_string()])?;
        mounted = false;

        pct_status(&[
            "set",
            &request.vmid.to_string(),
            "--hostname",
            &request.target_name,
        ])?;

        if request.start_after {
            emit_progress(
                "phase",
                "replica_start_target",
                &format!("Starting LXC {}", request.vmid),
                None,
            )?;
            pct_status(&["start", &request.vmid.to_string()])?;
        }

        emit_progress(
            "finished",
            "replica_complete",
            &format!(
                "Replica deployed to LXC {}{}",
                request.vmid,
                snapshot_name
                    .as_deref()
                    .map(|name| format!("; rollback snapshot {name} retained"))
                    .unwrap_or_default()
            ),
            None,
        )?;
        Ok(())
    })();

    if deploy_result.is_err() {
        if mounted {
            let _ = pct_status(&["unmount", &request.vmid.to_string()]);
        }
        if let Some(snapshot) = snapshot_name.as_deref() {
            let _ = emit_progress(
                "phase",
                "replica_rollback",
                &format!("Rolling LXC {} back to {snapshot}", request.vmid),
                None,
            );
            let _ = pct_status(&["rollback", &request.vmid.to_string(), snapshot]);
        }
    }

    let _ = fs::remove_dir_all(&workdir);
    deploy_result
}

fn read_request() -> Result<LxcReplicaRequest> {
    let mut raw = String::new();
    std::io::stdin()
        .read_to_string(&mut raw)
        .context("cannot read LXC replica request from stdin")?;
    serde_json::from_str(&raw).context("invalid LXC replica request JSON")
}

fn require_read_only_btrfs_subvolume(path: &Path) -> Result<()> {
    let show = Command::new(btrfs_executable())
        .args(["subvolume", "show"])
        .arg(path)
        .output()
        .context("cannot inspect replica staging subvolume")?;
    if !show.status.success() {
        bail!(
            "replica staging path {} is not a Btrfs subvolume",
            path.display()
        );
    }

    let property = Command::new(btrfs_executable())
        .args(["property", "get", "-ts"])
        .arg(path)
        .arg("ro")
        .output()
        .context("cannot inspect staging subvolume read-only property")?;
    if !property.status.success()
        || !String::from_utf8_lossy(&property.stdout)
            .trim()
            .eq_ignore_ascii_case("ro=true")
    {
        bail!(
            "replica staging subvolume {} must be read-only",
            path.display()
        );
    }
    Ok(())
}

fn pct_output(args: &[&str]) -> Result<String> {
    let output = Command::new(pct_executable())
        .args(args)
        .output()
        .with_context(|| format!("cannot execute pct {}", args.join(" ")))?;
    if !output.status.success() {
        bail!(
            "pct {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn pct_status(args: &[&str]) -> Result<()> {
    let output = pct_output(args)?;
    if !output.is_empty() {
        emit_progress("output", "replica_pct", &output, Some("stdout"))?;
    }
    Ok(())
}

fn parse_pct_mount_path(vmid: u32, output: &str) -> Result<PathBuf> {
    for line in output.lines() {
        if let Some(start) = line.find(" in '") {
            let remainder = &line[start + 5..];
            if let Some(end) = remainder.find('\'') {
                let path = PathBuf::from(&remainder[..end]);
                if path.is_absolute() {
                    return Ok(path);
                }
            }
        }
    }

    let fallback = PathBuf::from(format!("/var/lib/lxc/{vmid}/rootfs"));
    if fallback.is_dir() {
        return Ok(fallback);
    }
    bail!("could not determine mounted rootfs path from pct output {output:?}")
}

fn validate_target_root(vmid: u32, target: &Path, source: &Path) -> Result<()> {
    if !target.is_absolute() || target == Path::new("/") || !target.is_dir() {
        bail!("unsafe mounted rootfs path {}", target.display());
    }
    let canonical = fs::canonicalize(target)
        .with_context(|| format!("cannot resolve target root {}", target.display()))?;
    if canonical == Path::new("/") {
        bail!("refusing to operate on host root");
    }
    if source.starts_with(&canonical) || canonical.starts_with(source) {
        bail!("replica source and target overlap");
    }

    let expected = PathBuf::from(format!("/var/lib/lxc/{vmid}/rootfs"));
    if canonical != expected && !test_root_override_enabled() {
        bail!(
            "mounted rootfs {} does not match expected Proxmox path {}",
            canonical.display(),
            expected.display()
        );
    }
    Ok(())
}

fn nested_mounts_under(root: &Path) -> Result<Vec<PathBuf>> {
    let output = Command::new(findmnt_executable())
        .args(["-R", "-r", "-n", "-o", "TARGET"])
        .arg(root)
        .output()
        .context("cannot inspect nested target mounts")?;
    if !output.status.success() {
        bail!(
            "findmnt failed for target root {}: {}",
            root.display(),
            String::from_utf8_lossy(&output.stderr).trim()
        );
    }

    let root = fs::canonicalize(root)?;
    let mut mounts = Vec::new();
    for raw in String::from_utf8_lossy(&output.stdout).lines() {
        let candidate = PathBuf::from(raw.trim());
        if candidate == root || raw.trim().is_empty() {
            continue;
        }
        if candidate.starts_with(&root) {
            mounts.push(candidate);
        }
    }
    mounts.sort();
    mounts.dedup();
    Ok(mounts)
}

fn preserve_target_identity(target_root: &Path, preserve_root: &Path) -> Result<()> {
    fs::create_dir_all(preserve_root)?;
    for raw in LXC_TARGET_PRESERVE_PATHS {
        let relative = raw.trim_start_matches('/');
        let source = target_root.join(relative);
        if !source.exists() && !source.is_symlink() {
            continue;
        }
        let destination = preserve_root.join(relative);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        copy_archive(&source, &destination)?;
    }
    Ok(())
}

fn restore_target_identity(target_root: &Path, preserve_root: &Path) -> Result<()> {
    for raw in LXC_TARGET_PRESERVE_PATHS {
        let relative = raw.trim_start_matches('/');
        let preserved = preserve_root.join(relative);
        if !preserved.exists() && !preserved.is_symlink() {
            continue;
        }
        let destination = target_root.join(relative);
        remove_path_if_present(&destination)?;
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent)?;
        }
        copy_archive(&preserved, &destination)?;
    }
    Ok(())
}

fn copy_archive(source: &Path, destination: &Path) -> Result<()> {
    let status = Command::new(cp_executable())
        .arg("-a")
        .arg("--")
        .arg(source)
        .arg(destination)
        .status()
        .with_context(|| format!("cannot copy {}", source.display()))?;
    if !status.success() {
        bail!(
            "cannot preserve/restore {}: cp failed with {status}",
            source.display()
        );
    }
    Ok(())
}

fn clear_rootfs(root: &Path, protected_mounts: &[PathBuf]) -> Result<()> {
    #[cfg(unix)]
    let root_dev = fs::metadata(root)?.dev();

    for entry in fs::read_dir(root)? {
        let entry = entry?;
        clear_entry(
            &entry.path(),
            protected_mounts,
            #[cfg(unix)]
            root_dev,
        )?;
    }
    Ok(())
}

fn clear_entry(
    path: &Path,
    protected_mounts: &[PathBuf],
    #[cfg(unix)] root_dev: u64,
) -> Result<()> {
    if protected_mounts.iter().any(|protected| protected == path) {
        return Ok(());
    }

    let metadata = fs::symlink_metadata(path)?;
    if metadata.file_type().is_symlink() || metadata.is_file() {
        fs::remove_file(path)?;
        return Ok(());
    }

    if metadata.is_dir() {
        #[cfg(unix)]
        if metadata.dev() != root_dev {
            return Ok(());
        }

        for entry in fs::read_dir(path)? {
            clear_entry(
                &entry?.path(),
                protected_mounts,
                #[cfg(unix)]
                root_dev,
            )?;
        }

        if !protected_mounts
            .iter()
            .any(|protected| protected.starts_with(path))
        {
            fs::remove_dir(path)?;
        }
    }
    Ok(())
}

fn copy_userspace(
    source: &Path,
    target: &Path,
    exclusions: &[PathBuf],
    idmap: Option<&LxcIdMap>,
) -> Result<()> {
    let mut producer = Command::new(tar_executable());
    producer
        .arg("--numeric-owner")
        .arg("--acls")
        .arg("--xattrs")
        .arg("--one-file-system");
    for exclusion in exclusions {
        let relative = exclusion
            .strip_prefix("/")
            .unwrap_or(exclusion)
            .to_string_lossy();
        producer.arg(format!("--exclude=./{relative}"));
    }
    producer
        .arg("-C")
        .arg(source)
        .args(["-cpf", "-", "."])
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());

    let mut producer = producer.spawn().context("cannot start source tar")?;
    let stream = producer
        .stdout
        .take()
        .context("source tar stdout unavailable")?;

    let mut consumer = if let Some(idmap) = idmap {
        let mut command = Command::new(lxc_usernsexec_executable());
        command.args(usernsexec_map_args(idmap));
        command.arg("--").arg(tar_executable());
        command
    } else {
        Command::new(tar_executable())
    };
    consumer
        .arg("--numeric-owner")
        .arg("--acls")
        .arg("--xattrs")
        .args(["-xpf", "-"])
        .arg("-C")
        .arg(target)
        .stdin(Stdio::from(stream))
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    let consumer_status = consumer
        .status()
        .context("cannot start target tar extraction")?;
    let producer_status = producer.wait().context("cannot wait for source tar")?;

    if !producer_status.success() {
        bail!("source tar failed with {producer_status}");
    }
    if !consumer_status.success() {
        bail!("target tar extraction failed with {consumer_status}");
    }
    Ok(())
}

fn reset_machine_identity(target_root: &Path) -> Result<()> {
    let machine_id = target_root.join("etc/machine-id");
    if machine_id.exists() {
        OpenOptions::new()
            .write(true)
            .truncate(true)
            .open(&machine_id)
            .context("cannot reset /etc/machine-id")?;
    }

    let dbus_machine_id = target_root.join("var/lib/dbus/machine-id");
    remove_path_if_present(&dbus_machine_id)?;

    let ssh_dir = target_root.join("etc/ssh");
    if ssh_dir.is_dir() {
        for entry in fs::read_dir(&ssh_dir)? {
            let entry = entry?;
            let name = entry.file_name();
            if name.to_string_lossy().starts_with("ssh_host_") {
                remove_path_if_present(&entry.path())?;
            }
        }
    }
    Ok(())
}

fn regenerate_ssh_host_keys(target_root: &Path, idmap: Option<&LxcIdMap>) -> Result<()> {
    let target_ssh_dir = target_root.join("etc/ssh");
    if !target_ssh_dir.is_dir() {
        return Ok(());
    }

    let mut command = if let Some(idmap) = idmap {
        let mut command = Command::new(lxc_usernsexec_executable());
        command.args(usernsexec_map_args(idmap));
        command.arg("--").arg(ssh_keygen_executable());
        command
    } else {
        Command::new(ssh_keygen_executable())
    };

    let output = command
        .arg("-A")
        .arg("-f")
        .arg(target_root)
        .output()
        .context("cannot regenerate SSH host keys")?;

    for line in String::from_utf8_lossy(&output.stdout).lines() {
        if !line.trim().is_empty() {
            emit_progress("output", "replica_ssh_keys", line, Some("stdout"))?;
        }
    }
    for line in String::from_utf8_lossy(&output.stderr).lines() {
        if !line.trim().is_empty() {
            emit_progress("output", "replica_ssh_keys", line, Some("stderr"))?;
        }
    }

    if !output.status.success() {
        bail!(
            "ssh-keygen -A -f {} failed with {}",
            target_root.display(),
            output.status
        );
    }
    Ok(())
}

fn remove_path_if_present(path: &Path) -> Result<()> {
    let metadata = match fs::symlink_metadata(path) {
        Ok(metadata) => metadata,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(err) => return Err(err.into()),
    };
    if metadata.is_dir() && !metadata.file_type().is_symlink() {
        fs::remove_dir_all(path)?;
    } else {
        fs::remove_file(path)?;
    }
    Ok(())
}

fn set_private_mode(path: &Path) -> Result<()> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

#[cfg(unix)]
fn ensure_root() -> Result<()> {
    let output = Command::new("id")
        .arg("-u")
        .output()
        .context("cannot determine current uid")?;
    if String::from_utf8_lossy(&output.stdout).trim() != "0" {
        bail!("this operation must run as root");
    }
    Ok(())
}

#[cfg(not(unix))]
fn ensure_root() -> Result<()> {
    bail!("LXC replica deployment is only supported on Linux");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pct_mount_path_is_parsed_from_native_output() {
        let path =
            parse_pct_mount_path(420, "mounted CT 420 in '/var/lib/lxc/420/rootfs'\n").unwrap();
        assert_eq!(path, PathBuf::from("/var/lib/lxc/420/rootfs"));
    }

    #[test]
    fn clear_rootfs_preserves_explicit_nested_mount_paths() {
        let root = std::env::temp_dir().join(format!("harbor-clear-{}", Uuid::new_v4()));
        let keep = root.join("srv/external");
        let stale = root.join("etc/stale");
        fs::create_dir_all(&keep).unwrap();
        fs::create_dir_all(stale.parent().unwrap()).unwrap();
        fs::write(keep.join("keep.txt"), "keep").unwrap();
        fs::write(&stale, "delete").unwrap();

        clear_rootfs(&root, std::slice::from_ref(&keep)).unwrap();

        assert!(keep.join("keep.txt").is_file());
        assert!(!stale.exists());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn target_root_validation_refuses_host_root() {
        let source = PathBuf::from("/tmp/source");
        assert!(validate_target_root(420, Path::new("/"), &source).is_err());
    }
}
