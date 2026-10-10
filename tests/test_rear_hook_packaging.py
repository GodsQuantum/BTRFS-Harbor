"""ReaR hook must be static and ship with every Linux package."""

from pathlib import Path
import json
import subprocess

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "packaging/rear/harbor-rear-restore"
CONFIG = ROOT / "packaging/rear/harbor-rear-local.conf.example"


def test_no_disk_action_outside_rescue():
    result = subprocess.run(
        ["bash", str(HOOK)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    assert "ReaR rescue environment" in result.stderr


def test_static_rear_external_hook_and_no_partition_commands():
    text = CONFIG.read_text()
    assert "BACKUP=EXTERNAL" in text
    assert "OUTPUT=ISO" in text
    assert (
        "EXTERNAL_RESTORE='/usr/lib/btrfs-harbor/recovery/harbor-rear-restore'" in text
    )
    assert "EXTERNAL_BACKUP='true'" in text
    # ReaR 2.9 can omit systemd's dynamically opened shared library from
    # the rescue initramfs unless the distro's systemd lib directory is copied.
    assert "rear_systemd_lib_dir" in text
    assert 'COPY_AS_IS+=( "$rear_systemd_lib_dir" )' in text
    script = HOOK.read_text()
    assert "set-rear-recover" in script
    assert "RESTAURER" in script
    assert not any(x in script for x in ("mkfs.", "sgdisk", "parted ", "wipefs"))
    for path in (HOOK, CONFIG):
        assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0


def test_rear_hook_bundled_with_all_package_flavors():
    config = json.loads((ROOT / "harbor/desktop/src-tauri/tauri.conf.json").read_text())
    for variant in ("deb", "rpm"):
        assert (
            "/usr/lib/btrfs-harbor/recovery/harbor-rear-restore"
            in config["bundle"]["linux"][variant]["files"]
        )
        assert (
            "/usr/share/btrfs-harbor/rear/local.conf"
            in config["bundle"]["linux"][variant]["files"]
        )
    universal = (ROOT / "packaging/universal/install-payload.sh").read_text()
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "harbor-rear-restore" in universal
    assert "cp packaging/rear/harbor-rear-restore" in release
    assert "cp packaging/rear/local.conf" in release
