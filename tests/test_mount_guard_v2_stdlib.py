"""Mounted destination identity tests. No actual mounts or mount changes needed."""

import tempfile
import unittest
from pathlib import Path

from btrfs_backup_ng.endpoint.mount_guard_v2 import (
    capture_mount_identity,
    MountGuard,
    MountIdentity,
    parse_mountinfo,
)


class MountGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mount"
        self.root.mkdir()

    def test_capture_live_mount_identity_and_validate_fd(self):
        identity = capture_mount_identity(self.root)
        self.assertTrue(identity.fstype)
        with MountGuard(self.root, identity) as guard:
            guard.validate_fd(guard.directory_fd)

    def test_renaming_mount_root_and_replacing_refuses_writes(self):
        guard = MountGuard(self.root, capture_mount_identity(self.root))
        old = self.root.with_name("old-mount")
        self.root.rename(old)
        self.root.mkdir()
        try:
            with self.assertRaises(RuntimeError):
                guard.validate_fd(guard.directory_fd)
        finally:
            guard.close()
            self.root.rmdir()
            old.rename(self.root)

    def test_remote_required_rejects_underlying_mount(self):
        identity = capture_mount_identity(self.root)
        with self.assertRaises(ValueError):
            MountGuard(self.root, identity, required_remote=True)

    def test_reboot_identity_not_rejected_only_due_to_kernel_mount_id(self):
        identity = capture_mount_identity(self.root)
        replacement = MountIdentity(
            fstype=identity.fstype,
            source=identity.source,
            root=identity.root,
            mount_point=identity.mount_point,
            dev=identity.dev,
            mount_id=identity.mount_id + 1000,
            unique_mount_id=None,
        )
        self.assertTrue(identity.matches_persistent(replacement))
        self.assertFalse(identity.matches_live(replacement))

    def test_export_source_change_not_same_storage(self):
        a = MountIdentity("nfs", "host-a:/backup", "/", "/mnt/backup", "0:77", 71, None)
        b = MountIdentity("nfs", "host-b:/backup", "/", "/mnt/backup", "0:77", 72, None)
        self.assertFalse(a.matches_persistent(b))

    def test_mountinfo_decodes_spaces(self):
        content = "41 24 0:37 / /mnt/shared\\040drive rw,relatime - nfs4 host:/share\\040disk rw\n"
        info = parse_mountinfo(content)
        self.assertEqual(info[0].mount_point, "/mnt/shared drive")
        self.assertEqual(info[0].source, "host:/share disk")
        self.assertEqual(info[0].fstype, "nfs4")

    def test_cross_reboot_can_reopen_same_export_and_revalidate(self):
        identity = capture_mount_identity(self.root)
        with MountGuard(self.root, identity) as first:
            first.validate_fd(first.directory_fd)
        with MountGuard(self.root, identity) as second:
            second.validate_fd(second.directory_fd)
