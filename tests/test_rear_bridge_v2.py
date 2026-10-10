"""Non-destructive ReaR bridge planning and identity tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from btrfs_backup_ng.core.rear_bridge_v2 import (
    apply_rear_restore,
    find_received_subvolume,
    plan_rear_restore,
    reconcile_fstab,
    recorded_uuids,
    _preflight_recovered_fstab,
    _replace_fstab_pinned,
)

A = "aaaaaaa1-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
B = "bbbbbbb1-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def fixture(tmp_path: Path):
    root = tmp_path / "mounted-fresh-rear-root"
    root.mkdir()
    (root / "home").mkdir()
    (root / "boot/efi").mkdir(parents=True)
    staged = tmp_path / "stage"
    (staged / "volume-000/root-snapshot").mkdir(parents=True)
    (staged / "volume-000/parent-snapshot").mkdir()
    (staged / "volume-001/home-snapshot").mkdir(parents=True)
    (staged / "boot-files/boot-000/EFI/Boot").mkdir(parents=True)
    (staged / "boot-files/boot-000/EFI/Boot/BOOTX64.EFI").write_bytes(b"EFI")
    catalog = {
        "status": "completed_btrfs_and_boot_files",
        "bootable": False,
        "members": [
            {"original_mount": "/", "snapshot_uuid": A, "status": "completed"},
            {"original_mount": "/home", "snapshot_uuid": B, "status": "completed"},
        ],
        "boot_members": [
            {"mount_point": "/boot/efi", "filesystem": "vfat", "status": "completed"}
        ],
    }
    uuids = {
        "root-snapshot": A,
        "home-snapshot": B,
        "parent-snapshot": "ccccccc1-cccc-4ccc-8ccc-cccccccccccc",
    }
    return (
        catalog,
        staged,
        root,
        lambda path: uuids.get(path.name),
        lambda path: "vfat" if path == root / "boot/efi" else "btrfs",
    )


def test_restore_order_exact_received_uuid_mount_identity(tmp_path: Path):
    catalog, stage, root, probe, filesystem = fixture(tmp_path)
    result = plan_rear_restore(catalog, stage, root, probe=probe, filesystem=filesystem)
    assert [(r.original_mount, r.is_boot_files) for r in result] == [
        ("/", False),
        ("/home", False),
        ("/boot/efi", True),
    ]
    assert result[0].source.name == "root-snapshot"
    assert result[1].source.name == "home-snapshot"
    assert result[2].source == stage / "boot-files/boot-000"


def test_recovery_requires_complete_verified_source(tmp_path: Path):
    catalog, stage, root, probe, filesystem = fixture(tmp_path)
    catalog["status"] = "pending"
    with pytest.raises(ValueError, match="not completed"):
        plan_rear_restore(catalog, stage, root, probe=probe, filesystem=filesystem)


def test_recovery_refuses_wrong_efi_mount_type(tmp_path: Path):
    catalog, stage, root, probe, _ = fixture(tmp_path)
    with pytest.raises(ValueError, match="mount type differs"):
        plan_rear_restore(
            catalog, stage, root, probe=probe, filesystem=lambda _: "btrfs"
        )


def test_recovery_refuses_symlink_mount_escape(tmp_path: Path):
    catalog, stage, root, probe, filesystem = fixture(tmp_path)
    home = root / "home"
    home.rmdir()
    home.symlink_to(tmp_path)
    with pytest.raises(ValueError, match="did not create system mount"):
        plan_rear_restore(catalog, stage, root, probe=probe, filesystem=filesystem)


def test_recovery_refuses_parent_or_missing_mount(tmp_path: Path):
    catalog, stage, root, probe, filesystem = fixture(tmp_path)
    catalog["members"][1]["original_mount"] = "/../etc"
    with pytest.raises(ValueError, match="invalid recorded"):
        plan_rear_restore(catalog, stage, root, probe=probe, filesystem=filesystem)


def test_btrfs_incremental_received_snapshot_selected_by_uuid(tmp_path: Path):
    catalog, stage, root, probe, filesystem = fixture(tmp_path)
    assert (
        find_received_subvolume(stage / "volume-000", A, probe=probe).name
        == "root-snapshot"
    )
    with pytest.raises(ValueError, match="ambiguous/missing"):
        find_received_subvolume(stage / "volume-000", B, probe=probe)


def test_host_recovery_requires_rescue_environment(tmp_path: Path):
    catalog, stage, root, _, _ = fixture(tmp_path)
    with pytest.raises(PermissionError, match="only allowed in ReaR"):
        apply_rear_restore(catalog, stage, system_root=root)
    assert (root / "boot/efi").exists()


def test_fstab_rewrites_only_original_partition_uuid_for_mounted_system() -> None:
    initial = (
        "# Preserve comment\n"
        "UUID=OLD-ROOT\t/ btrfs subvol=@,compress=zstd 0 0\n"
        "UUID=OLD-ROOT /home btrfs subvol=@home 0 0\n"
        "PARTUUID=PART-OLD /boot/efi vfat umask=0077 0 2\n"
    )
    original = {
        "/": ("OLD-ROOT", "PART-ROOT"),
        "/home": ("OLD-ROOT", "PART-ROOT"),
        "/boot/efi": ("OLD-ESP", "PART-OLD"),
    }
    current = {
        "/": ("NEW-ROOT", "PART-NEW"),
        "/home": ("NEW-ROOT", "PART-NEW"),
        "/boot/efi": ("NEW-ESP", "PART-ESP-NEW"),
    }
    actual = reconcile_fstab(initial, original, current)
    assert actual == initial.replace("UUID=OLD-ROOT", "UUID=NEW-ROOT").replace(
        "PARTUUID=PART-OLD", "PARTUUID=PART-ESP-NEW"
    )
    with pytest.raises(ValueError, match="recorded backup"):
        reconcile_fstab("UUID=SPOOF / btrfs defaults 0 0\n", original, current)
    with pytest.raises(ValueError, match="outside Harbor"):
        reconcile_fstab("UUID=SWAP none swap sw 0 0\n", original, current)


def test_disk_layout_records_old_uuids_for_root_home_and_efi() -> None:
    layout = {
        "blockdevices": [
            {
                "path": "/dev/vda",
                "type": "disk",
                "children": [
                    {
                        "path": "/dev/vda1",
                        "uuid": "efi-uuid",
                        "partuuid": "efi-part",
                        "mountpoints": ["/boot/efi"],
                    },
                    {
                        "path": "/dev/vda2",
                        "uuid": "root-uuid",
                        "partuuid": "root-part",
                        "mountpoints": ["/", "/home"],
                    },
                ],
            }
        ]
    }
    assert recorded_uuids(layout) == {
        "/": ("root-uuid", "root-part"),
        "/home": ("root-uuid", "root-part"),
        "/boot/efi": ("efi-uuid", "efi-part"),
    }


def test_fstab_refuses_missing_replacement_uuid() -> None:
    with pytest.raises(ValueError, match="new filesystem UUID"):
        reconcile_fstab(
            "UUID=OLD / btrfs defaults 0 0\n", {"/": ("OLD", None)}, {"/": (None, None)}
        )


def test_rear_preflight_rejects_live_crypttab_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import btrfs_backup_ng.core.rear_bridge_v2 as bridge

    catalog, stage, root, probe, filesystem = fixture(tmp_path)
    system = stage / "volume-000/root-snapshot/etc"
    system.mkdir()
    (system / "fstab").write_text("UUID=old / btrfs defaults 0 0\n")
    (system / "crypttab").write_text("cryptroot UUID=secrets none luks\n")
    catlayout = {
        "blockdevices": [
            {"uuid": "old", "partuuid": "part", "mountpoints": ["/", "/home"]},
            {"uuid": "old-esp", "partuuid": "esp-part", "mountpoints": ["/boot/efi"]},
        ]
    }
    catalog["disk_layout"] = catlayout
    plan = plan_rear_restore(catalog, stage, root, probe=probe, filesystem=filesystem)
    monkeypatch.setattr(bridge, "_mounted_uuid", lambda _: ("new", "new-part"))
    with pytest.raises(ValueError, match="encrypted filesystems"):
        _preflight_recovered_fstab(catalog, plan)
    (system / "crypttab").unlink()
    assert (
        _preflight_recovered_fstab(catalog, plan) == "UUID=new / btrfs defaults 0 0\n"
    )


def test_rear_fstab_replacement_is_atomic_and_refuses_symlink(tmp_path: Path) -> None:
    etc = tmp_path / "etc"
    etc.mkdir()
    fstab = etc / "fstab"
    fstab.write_text("UUID=old / btrfs defaults 0 0\n")
    _replace_fstab_pinned(tmp_path, "UUID=new / btrfs defaults 0 0\n")
    assert fstab.read_text() == "UUID=new / btrfs defaults 0 0\n"
    assert not list(etc.glob("*.tmp"))
    fstab.unlink()
    target = tmp_path / "unrelated"
    target.write_text("preserve")
    fstab.symlink_to(target)
    with pytest.raises(ValueError, match="not a regular file"):
        _replace_fstab_pinned(tmp_path, "malicious")
    assert target.read_text() == "preserve"
