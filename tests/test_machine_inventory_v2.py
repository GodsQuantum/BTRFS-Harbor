"""Coverage inventory must never claim that Btrfs-only == bootable machine DR."""

import argparse
import json

from btrfs_backup_ng.core.machine_inventory_v2 import (
    _mount_rows,
    build_machine_inventory,
)
from btrfs_backup_ng.detection.models import (
    DetectedSubvolume,
    DetectionResult,
    SubvolumeClass,
)
from btrfs_backup_ng.detection import scanner


def subvol(device, number, path, mount, kind=SubvolumeClass.UNKNOWN):
    return DetectedSubvolume(
        id=number,
        device=device,
        path=path,
        mount_point=mount,
        classification=kind,
    )


def test_root_home_and_separate_efi_never_claim_bootable():
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/sda2", 256, "/@", "/", SubvolumeClass.SYSTEM_ROOT),
            subvol("/dev/sda2", 257, "/@home", "/home", SubvolumeClass.USER_DATA),
            subvol(
                "/dev/sda2",
                258,
                "/@/.snapshots/2/snapshot",
                None,
                SubvolumeClass.SNAPSHOT,
            ),
        ]
    )
    mounts = _mount_rows(
        "/dev/sda2 / btrfs rw,subvolid=256 0 0\n"
        "/dev/sda2 /home btrfs rw,subvolid=257 0 0\n"
        "/dev/sda1 /boot/efi vfat rw 0 0\n"
    )
    result = build_machine_inventory(report, mounts)
    assert result["inventory_complete"] is True
    assert result["included_btrfs_count"] == 2
    assert [s["mount_point"] for s in result["sources"]] == ["/", "/home"]
    assert result["complete_machine_backup"] is False
    assert result["bootable_recovery_ready"] is False
    assert result["non_btrfs_persistent_mounts"] == [
        {"source": "/dev/sda1", "mount_point": "/boot/efi", "filesystem": "vfat"}
    ]
    assert result["excluded_internal"][0]["reason"] == "snapshot"


def test_external_btrfs_and_unmounted_sources_are_explicit():
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/sdb1", 256, "/media", "/media/disk"),
            subvol("/dev/sda2", 270, "/@data", None),
        ]
    )
    result = build_machine_inventory(report, [])
    assert result["included_btrfs_count"] == 0
    assert [s["reason"] for s in result["sources"]] == [
        "unmounted_or_unverified",
        "external_opt_in",
    ]
    assert any("Unmounted" in msg for msg in result["warnings"])
    assert any("opt-in" in msg for msg in result["warnings"])


def test_partial_scan_is_not_certified():
    report = DetectionResult(
        is_partial=True,
        error_message="Permission denied on /srv",
        subvolumes=[subvol("/dev/sda", 256, "/@", "/")],
    )
    result = build_machine_inventory(report, [])
    assert result["inventory_complete"] is False
    assert "Permission denied on /srv" in result["warnings"]


def test_duplicate_ids_on_different_btrfs_filesystems_are_legitimate():
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/one", 256, "/@", "/"),
            subvol("/dev/two", 256, "/@", "/srv"),
        ]
    )
    result = build_machine_inventory(report, [])
    assert result["included_btrfs_count"] == 2
    assert not any("Duplicate" in msg for msg in result["warnings"])


def test_mounts_decode_spaces_and_skip_nonpersistent_virtual():
    rows = _mount_rows(
        "/dev/sda3 /my\\040data ext4 rw 0 0\n"
        "tmpfs /run/user/1000 tmpfs rw 0 0\n"
        "/dev/nvme0n1p1 /boot/efi vfat rw 0 0\n"
    )
    assert [r["mount_point"] for r in rows] == ["/my data", "/boot/efi"]


def test_read_only_cli_machine_inventory(monkeypatch, capsys):
    from btrfs_backup_ng.cli.checkpoint_v2_cmd import _execute

    marker = {"schema_version": 1, "complete_machine_backup": False}
    monkeypatch.setattr(
        "btrfs_backup_ng.core.machine_inventory_v2.inspect_live_machine",
        lambda: marker,
    )
    assert _execute(argparse.Namespace(checkpoint_action="machine-inventory")) == 0
    assert json.loads(capsys.readouterr().out) == marker


def test_scan_opt_in_removable_media_is_not_global(monkeypatch):
    recorded = []

    def parse(*, exclude_removable=True):
        recorded.append(exclude_removable)
        return []

    monkeypatch.setattr(scanner, "parse_proc_mounts", parse)
    scanner.scan_system(allow_partial=True)
    scanner.scan_system(allow_partial=True, include_removable=True)
    assert recorded == [True, False]


def test_machine_set_list_never_hides_corrupted_catalogs(tmp_path, capsys):
    """Corrupt archives must trigger a visible error, not disappear from recovery."""
    from btrfs_backup_ng.cli.machine_set_v2 import execute_machine_set

    import uuid

    valid_id = str(uuid.uuid4())
    invalid_id = str(uuid.uuid4())
    profile_id = str(uuid.uuid4())
    from btrfs_backup_ng.core.native_snapshots import NATIVE_FOLDER

    catalog = {
        "schema_version": 1,
        "set_id": valid_id,
        "profile_id": profile_id,
        "coverage": "btrfs-only",
        "status": "completed_btrfs_only",
        "created_utc": "2026-10-09T21:00:00+00:00",
        "members": [
            {
                "archive": f"machine20261009T210000-{valid_id[:8]}-000",
                "original_mount": "/home",
                "snapshot_path": f"/home/{NATIVE_FOLDER}/harbor-20261009T120000Z-012345abcdef",
                "status": "completed",
            }
        ],
    }
    (tmp_path / f".harbor-machine-set-{valid_id}.json").write_text(json.dumps(catalog))
    (tmp_path / f".harbor-machine-set-{invalid_id}.json").write_text("{broken")
    outside = tmp_path / "outside"
    outside.write_text("unsafe")
    (tmp_path / f".harbor-machine-set-{uuid.uuid4()}.json").symlink_to(outside)
    args = argparse.Namespace(
        checkpoint_action="set-list",
        target=str(tmp_path),
        allow_local=True,
        experimental=False,
    )
    import pytest

    # Fail closed on malformed JSON, or on a malicious symlink, even if a
    # healthy backup is present. Silently filtering them looks like data loss.
    with pytest.raises((ValueError, OSError)):
        execute_machine_set(args)
    (tmp_path / f".harbor-machine-set-{invalid_id}.json").unlink()
    with pytest.raises((ValueError, OSError)):
        execute_machine_set(args)
    for entry in tmp_path.glob(".harbor-machine-set-*.json"):
        if entry.is_symlink():
            entry.unlink()
    assert execute_machine_set(args) == 0
    found = json.loads(capsys.readouterr().out)
    assert len(found) == 1
    assert found[0]["set_id"] == valid_id
    assert found[0]["bootable"] is False
    assert outside.read_text() == "unsafe"


def test_set_lock_refuses_missing_mount_without_creating_file(tmp_path):
    """Simulated detached mount must not create a lock in an underlying folder."""
    import os
    import uuid
    import pytest

    from btrfs_backup_ng.cli.machine_set_v2 import _exclusive_set_lock

    class Detached:
        directory_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)

        def validate_fd(self, directory_fd):
            assert directory_fd == self.directory_fd
            raise RuntimeError("destination live mount ID has changed")

    guard = Detached()
    set_id = str(uuid.uuid4())
    try:
        with pytest.raises(RuntimeError, match="mount ID"):
            with _exclusive_set_lock(guard, set_id):
                raise AssertionError("must never enter acquired lock")
        assert not list(tmp_path.glob(".harbor-machine-set-*.lock"))
    finally:
        os.close(guard.directory_fd)


def test_nested_font_bind_mount_is_alias_not_separate_backup_source(
    monkeypatch, tmp_path
):
    """Regression from Pegasus: /home/arezki/.local/share/fonts is a bind mount."""
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory

    root = "/"
    home = "/home"
    font_alias = "/home/arezki/.local/share/fonts"
    mounts = (
        "/dev/mapper/system / btrfs rw,subvol=/@,subvolid=256 0 0\n"
        "/dev/mapper/system /home btrfs rw,subvol=/@home,subvolid=257 0 0\n"
        "/dev/mapper/system /home/arezki/.local/share/fonts "
        "btrfs rw,subvol=/@home,subvolid=257 0 0\n"
        "/dev/nvme0n1p1 /boot/efi vfat rw 0 0\n"
    )
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/mapper/system", 256, "/@", "/", SubvolumeClass.SYSTEM_ROOT),
            # scanner can mistakenly prefer the bind alias to the real root
            subvol(
                "/dev/mapper/system",
                257,
                "/@home",
                font_alias,
                SubvolumeClass.USER_DATA,
            ),
        ]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kwargs: report)
    monkeypatch.setattr(
        inventory,
        "_is_real_subvolume_root",
        lambda path: path in (root, home),
    )
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mounts)
    found = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert found["inventory_complete"]
    assert found["included_btrfs_count"] == 2
    assert [item["mount_point"] for item in found["sources"]] == ["/", "/home"]
    assert not any(
        item.get("mount_point") == font_alias and item.get("included_by_default")
        for item in found["sources"]
    )
    assert found["bind_mounts"] == [
        {
            "mount_point": font_alias,
            "filesystem": "btrfs",
            "device": "/dev/mapper/system",
            "subvolume_id": "257",
            "reason": "bind_alias_of_verified_btrfs_root",
        }
    ]
    from btrfs_backup_ng.cli import machine_set_v2

    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: found)
    sources, _ = machine_set_v2._sources_from_inventory()
    assert sources == ["/", "/home"]


def test_uncovered_btrfs_bind_mount_is_not_silently_skipped(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory
    from btrfs_backup_ng.cli import machine_set_v2

    bind = "/home/arezki/.local/share/fonts"
    mounts = (
        "/dev/nvme0n1p2 / btrfs rw,subvol=/@,subvolid=256 0 0\n"
        "/dev/sdb1 " + bind + " btrfs rw,subvol=/@external,subvolid=300 0 0\n"
    )
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/nvme0n1p2", 256, "/@", "/"),
            subvol("/dev/sdb1", 300, "/@external", bind),
        ]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kwargs: report)
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda path: path == "/")
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mounts)
    found = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert found["inventory_complete"] is False
    assert found["included_btrfs_count"] == 1
    assert found["bind_mounts"][0]["reason"] == "uncovered_nonroot_btrfs_mount"
    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: found)
    import pytest

    with pytest.raises(ValueError, match="Uncovered Btrfs mount: .*fonts"):
        machine_set_v2._sources_from_inventory()


def test_two_mounts_of_same_actual_root_are_deduplicated(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory

    mounts = (
        "/dev/sys /home btrfs rw,subvolid=257,subvol=/@home 0 0\n"
        "/dev/sys /home/duplicate btrfs rw,subvolid=257,subvol=/@home 0 0\n"
    )
    report = DetectionResult(
        subvolumes=[subvol("/dev/sys", 257, "/@home", "/home/duplicate")]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kwargs: report)
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda path: True)
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mounts)
    found = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert found["included_btrfs_count"] == 1
    assert found["sources"][0]["mount_point"] == "/home"


def test_snapshot_marked_from_metadata_never_auto_included():
    report = DetectionResult(
        subvolumes=[
            DetectedSubvolume(
                id=256,
                device="/dev/sys",
                path="/@",
                mount_point="/",
                classification=SubvolumeClass.SYSTEM_ROOT,
            ),
            DetectedSubvolume(
                id=311,
                device="/dev/sys",
                path="/odd_snapshot",
                mount_point="/mnt/unrelated",
                is_snapshot=True,
            ),
        ],
    )
    found = build_machine_inventory(report, [])
    assert found["included_btrfs_count"] == 1
    assert found["excluded_internal"][0]["reason"] == "snapshot"


def test_invalid_nonroot_mount_predicate_is_read_only(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory

    directory = tmp_path / "regular-dir"
    directory.mkdir()
    invoked = []
    monkeypatch.setattr(
        inventory.subprocess, "run", lambda *_args, **_kw: invoked.append(True)
    )
    assert inventory._is_real_subvolume_root(str(directory)) is False
    assert not invoked
    assert inventory._is_real_subvolume_root(str(tmp_path / "missing")) is False


def test_uncovered_mount_refuses_backup_before_any_snapshot_or_target_write(
    monkeypatch, tmp_path
):
    import argparse
    import pytest
    from btrfs_backup_ng.cli import machine_set_v2

    findings = {
        "inventory_complete": False,
        "warnings": [
            "Btrfs directory mount has no accessible subvolume root: "
            "/home/arezki/.local/share/fonts; complete-machine backup is blocked"
        ],
        "sources": [{"mount_point": "/", "included_by_default": True}],
    }
    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: findings)
    monkeypatch.setattr(
        machine_set_v2,
        "create_native_snapshot",
        lambda *a, **kw: pytest.fail("must not create any snapshot"),
    )
    monkeypatch.setattr(
        machine_set_v2,
        "_open_guard",
        lambda *a, **kw: pytest.fail("must not touch destination"),
    )
    target = tmp_path / "untouched"
    target.mkdir()
    args = argparse.Namespace(
        checkpoint_action="set-start",
        target=str(target),
        profile_id="55555555-5555-4555-8555-555555555555",
        source=None,
        snapshot_provider="harbor",
        allow_local=True,
        experimental=True,
    )
    with pytest.raises(ValueError, match="fonts"):
        machine_set_v2._execute_machine_set_unlocked(args)
    assert not list(target.iterdir())


def test_system_bind_from_external_btrfs_cannot_be_declared_covered(
    monkeypatch, tmp_path
):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory
    from btrfs_backup_ng.cli import machine_set_v2

    bind = "/home/arezki/.local/share/fonts"
    external_root = "/mnt/portable-btrfs"
    mounts = (
        "/dev/system / btrfs rw,subvolid=256,subvol=/@ 0 0\n"
        "/dev/portable "
        + external_root
        + " btrfs rw,subvolid=300,subvol=/@outside 0 0\n"
        "/dev/portable " + bind + " btrfs rw,subvolid=300,subvol=/@outside 0 0\n"
    )
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/system", 256, "/@", "/"),
            subvol("/dev/portable", 300, "/@outside", bind),
        ]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kw: report)
    monkeypatch.setattr(
        inventory, "_is_real_subvolume_root", lambda path: path in ("/", external_root)
    )
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mounts)
    result = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert result["included_btrfs_count"] == 1
    assert result["inventory_complete"] is False
    assert any(
        row["reason"] == "uncovered_btrfs_bind_alias" for row in result["bind_mounts"]
    )
    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: result)
    import pytest

    with pytest.raises(ValueError, match="fonts"):
        machine_set_v2._sources_from_inventory()


def test_same_subvolume_mounted_system_and_external_prefers_system_root(
    monkeypatch, tmp_path
):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory

    mounts = (
        "/dev/sys /mnt/x btrfs rw,subvolid=257,subvol=/@home 0 0\n"
        "/dev/sys /home/workstation/data btrfs rw,subvolid=257,subvol=/@home 0 0\n"
    )
    report = DetectionResult(
        subvolumes=[
            subvol("/dev/sys", 257, "/@home", "/mnt/x"),
        ]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kw: report)
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda path: True)
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mounts)
    result = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert result["inventory_complete"]
    assert result["included_btrfs_count"] == 1
    assert result["sources"][0]["mount_point"] == "/home/workstation/data"


def test_system_ext4_home_must_fail_before_backup(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory
    from btrfs_backup_ng.cli import machine_set_v2
    import pytest

    data = (
        "/dev/system / btrfs rw,subvolid=256,subvol=/@ 0 0\n"
        "/dev/data /home ext4 rw,relatime 0 0\n"
        "/dev/boot /boot/efi vfat rw,relatime 0 0\n"
        "/dev/usb /mnt/usb ext4 rw 0 0\n"
        "/dev/snap /snap/tool/42 squashfs ro 0 0\n"
    )
    discovered = DetectionResult(
        subvolumes=[subvol("/dev/system", 256, "/@", "/", SubvolumeClass.SYSTEM_ROOT)]
    )
    monkeypatch.setattr(inventory, "scan_system", lambda **kw: discovered)
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda path: path == "/")
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(data)
    report = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert not report["inventory_complete"]
    assert report["included_btrfs_count"] == 1
    assert report["uncovered_persistent_mounts"] == [
        {"source": "/dev/data", "mount_point": "/home", "filesystem": "ext4"}
    ]
    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: report)
    with pytest.raises(
        ValueError, match=r"Non-Btrfs system data not covered: /home \(ext4\)"
    ):
        machine_set_v2._sources_from_inventory()


def test_system_xfs_var_lib_is_not_silently_omitted(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory

    mount_info = "/dev/sys / btrfs rw,subvolid=256 0 0\n/dev/data /var/lib xfs rw 0 0\n"
    detected = DetectionResult(subvolumes=[subvol("/dev/sys", 256, "/@", "/")])
    monkeypatch.setattr(inventory, "scan_system", lambda **kw: detected)
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda path: path == "/")
    mounts_file = tmp_path / "mounts"
    mounts_file.write_text(mount_info)
    report = inventory.inspect_live_machine(mounts_path=mounts_file)
    assert not report["inventory_complete"]
    assert report["uncovered_persistent_mounts"][0]["mount_point"] == "/var/lib"


def test_external_unreadable_btrfs_does_not_poison_system_scan(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory

    mounts = (
        "/dev/sys / btrfs rw,subvolid=256 0 0\n"
        "/dev/backup /mnt/backup-drive btrfs rw,subvolid=301 0 0\n"
    )
    captured = []
    monkeypatch.setattr(
        inventory,
        "scan_system",
        lambda **kw: (
            captured.append(kw)
            or DetectionResult(subvolumes=[subvol("/dev/sys", 256, "/@", "/")])
        ),
    )
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda path: path == "/")
    file = tmp_path / "mounts"
    file.write_text(mounts)
    found = inventory.inspect_live_machine(mounts_path=file)
    assert captured == [{"allow_partial": True, "include_removable": False}]
    assert found["inventory_complete"]
    assert found["sources"][0]["mount_point"] == "/"
    assert all(
        not row.get("included_by_default")
        for row in found["sources"]
        if row.get("mount_point") == "/mnt/backup-drive"
    )


def test_inventory_error_prioritizes_actual_missing_mount(monkeypatch):
    import pytest
    from btrfs_backup_ng.cli import machine_set_v2

    report = {
        "inventory_complete": False,
        "warnings": [f"Unimportant warning {i}" for i in range(16)],
        "uncovered_persistent_mounts": [{"mount_point": "/home", "filesystem": "ext4"}],
        "sources": [],
    }
    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: report)
    with pytest.raises(ValueError, match="/home"):
        machine_set_v2._sources_from_inventory()


def test_missing_configured_btrfs_home_prevents_false_complete(monkeypatch, tmp_path):
    from btrfs_backup_ng.core import machine_inventory_v2 as inventory
    from btrfs_backup_ng.cli import machine_set_v2
    import pytest

    mounts_file = tmp_path / "mounts"
    mounts_file.write_text("/dev/system / btrfs rw,subvolid=256,subvol=/@ 0 0\n")
    fstab_file = tmp_path / "fstab"
    fstab_file.write_text(
        "UUID=os / btrfs defaults,subvol=@ 0 0\n"
        "UUID=home /home btrfs defaults,nofail,subvol=@home 0 0\n"
        "UUID=external /mnt/Backup btrfs nofail 0 0\n"
        "UUID=efi /boot/efi vfat defaults 0 2\n"
        "tmpfs /tmp tmpfs defaults 0 0\n"
    )
    monkeypatch.setattr(
        inventory,
        "scan_system",
        lambda **kwargs: DetectionResult(
            subvolumes=[subvol("/dev/system", 256, "/@", "/")]
        ),
    )
    monkeypatch.setattr(inventory, "_is_real_subvolume_root", lambda p: p == "/")
    found = inventory.inspect_live_machine(
        mounts_path=mounts_file, fstab_path=fstab_file
    )
    assert found["inventory_complete"] is False
    assert found["unmounted_configured_volumes"] == [
        {"source": "UUID=home", "mount_point": "/home", "filesystem": "btrfs"}
    ]
    monkeypatch.setattr(machine_set_v2, "inspect_live_machine", lambda: found)
    with pytest.raises(ValueError, match="Configured filesystem not mounted: /home"):
        machine_set_v2._sources_from_inventory()


def test_fstab_mount_with_escaped_spaces_and_bind_handled():
    from btrfs_backup_ng.core.machine_inventory_v2 import (
        _configured_unmounted_system_volumes,
    )

    rows = [
        {"source": "/dev/os", "mount_point": "/", "filesystem": "btrfs"},
    ]
    items = _configured_unmounted_system_volumes(
        rows,
        "UUID=foo /var/my\\040data btrfs defaults 0 0\n"
        "/mnt/common /srv/common none bind 0 0\n"
        "server:/mnt /mnt/remote nfs4 nofail 0 0\n",
    )
    assert items == [
        {"source": "UUID=foo", "mount_point": "/var/my data", "filesystem": "btrfs"}
    ]
