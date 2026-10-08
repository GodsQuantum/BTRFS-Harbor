use serde::{Deserialize, Serialize};
use std::collections::BTreeSet;
use std::env;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum PackageManager {
    Pacman,
    Apt,
    Dnf,
    Zypper,
    Unknown,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum InitramfsTool {
    Mkinitcpio,
    Dracut,
    UpdateInitramfs,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PlatformCapabilities {
    pub btrfs: bool,
    pub snapper: bool,
    pub systemd: bool,
    pub polkit: bool,
    pub package_manager: PackageManager,
    pub initramfs: InitramfsTool,
    pub bootloader_tools: Vec<String>,
    pub manual_backup_recovery: bool,
    pub automatic_scheduling: bool,
}

pub fn detect_from_commands(commands: &BTreeSet<String>) -> PlatformCapabilities {
    let has = |name: &str| commands.contains(name);
    let package_manager = if has("pacman") {
        PackageManager::Pacman
    } else if has("apt-get") {
        PackageManager::Apt
    } else if has("dnf") {
        PackageManager::Dnf
    } else if has("zypper") {
        PackageManager::Zypper
    } else {
        PackageManager::Unknown
    };
    let initramfs = if has("mkinitcpio") {
        InitramfsTool::Mkinitcpio
    } else if has("dracut") {
        InitramfsTool::Dracut
    } else if has("update-initramfs") {
        InitramfsTool::UpdateInitramfs
    } else {
        InitramfsTool::Unknown
    };
    let bootloader_tools = [
        "bootctl",
        "grub-install",
        "grub2-install",
        "limine",
        "efibootmgr",
    ]
    .into_iter()
    .filter(|name| has(name))
    .map(ToOwned::to_owned)
    .collect::<Vec<_>>();

    let btrfs = has("btrfs");
    let systemd = has("systemctl");
    PlatformCapabilities {
        btrfs,
        snapper: has("snapper"),
        systemd,
        polkit: has("pkexec"),
        package_manager,
        initramfs,
        bootloader_tools,
        manual_backup_recovery: btrfs,
        automatic_scheduling: btrfs && systemd,
    }
}

fn command_exists(name: &str) -> bool {
    if name.contains('/') {
        return Path::new(name).is_file();
    }
    let Some(path) = env::var_os("PATH") else {
        return false;
    };
    env::split_paths(&path)
        .map(|directory| directory.join(name))
        .any(|candidate: PathBuf| candidate.is_file())
}

pub fn detect_platform_capabilities() -> PlatformCapabilities {
    let known = [
        "btrfs",
        "snapper",
        "systemctl",
        "pkexec",
        "pacman",
        "apt-get",
        "dnf",
        "zypper",
        "mkinitcpio",
        "dracut",
        "update-initramfs",
        "bootctl",
        "grub-install",
        "grub2-install",
        "limine",
        "efibootmgr",
    ];
    let commands = known
        .into_iter()
        .filter(|name| command_exists(name))
        .map(ToOwned::to_owned)
        .collect::<BTreeSet<_>>();
    detect_from_commands(&commands)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn commands(items: &[&str]) -> BTreeSet<String> {
        items.iter().map(|item| (*item).to_string()).collect()
    }

    #[test]
    fn arch_and_cachyos_are_capability_detected_not_name_detected() {
        let caps = detect_from_commands(&commands(&[
            "btrfs",
            "snapper",
            "systemctl",
            "pkexec",
            "pacman",
            "mkinitcpio",
            "limine",
        ]));
        assert_eq!(caps.package_manager, PackageManager::Pacman);
        assert_eq!(caps.initramfs, InitramfsTool::Mkinitcpio);
        assert!(caps.manual_backup_recovery);
        assert!(caps.automatic_scheduling);
    }

    #[test]
    fn debian_and_ubuntu_use_apt_and_update_initramfs() {
        let caps = detect_from_commands(&commands(&[
            "btrfs",
            "snapper",
            "systemctl",
            "pkexec",
            "apt-get",
            "update-initramfs",
            "grub-install",
        ]));
        assert_eq!(caps.package_manager, PackageManager::Apt);
        assert_eq!(caps.initramfs, InitramfsTool::UpdateInitramfs);
        assert!(caps.automatic_scheduling);
    }

    #[test]
    fn fedora_uses_dnf_and_dracut() {
        let caps = detect_from_commands(&commands(&[
            "btrfs",
            "snapper",
            "systemctl",
            "pkexec",
            "dnf",
            "dracut",
            "grub2-install",
        ]));
        assert_eq!(caps.package_manager, PackageManager::Dnf);
        assert_eq!(caps.initramfs, InitramfsTool::Dracut);
    }

    #[test]
    fn opensuse_uses_zypper_and_dracut() {
        let caps = detect_from_commands(&commands(&[
            "btrfs",
            "snapper",
            "systemctl",
            "pkexec",
            "zypper",
            "dracut",
            "grub2-install",
        ]));
        assert_eq!(caps.package_manager, PackageManager::Zypper);
        assert_eq!(caps.initramfs, InitramfsTool::Dracut);
    }

    #[test]
    fn non_systemd_btrfs_keeps_manual_mode_without_claiming_automation() {
        let caps = detect_from_commands(&commands(&["btrfs", "snapper", "pkexec"]));
        assert!(caps.manual_backup_recovery);
        assert!(!caps.automatic_scheduling);
        assert!(!caps.systemd);
    }
}
