"""Native Btrfs send preflight/argv; synthetic only, never mounts a filesystem."""

import importlib
import sys
import types
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    for name, path in (
        ("btrfs_backup_ng", "src/btrfs_backup_ng"),
        ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
        ("btrfs_backup_ng.endpoint", "src/btrfs_backup_ng/endpoint"),
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(ROOT / path)]
        sys.modules[name] = module
app = importlib.import_module("btrfs_backup_ng.core.native_send_v2")
mount = importlib.import_module("btrfs_backup_ng.endpoint.mount_guard_v2")


class NativeBtrfsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harbor-btrfs-preflight-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)

    def test_source_must_be_readonly_snapshot_with_real_uuid(self):
        source = self.path / "snapshot"
        source.mkdir()

        def fake_run(args, **kwargs):
            if args[1:3] == ["subvolume", "show"]:
                return MagicMock(
                    returncode=0, stdout="UUID: abc-def\nReceived UUID: -\n"
                )
            return MagicMock(returncode=0, stdout="ro=true\n")

        with patch.object(app.subprocess, "run", side_effect=fake_run):
            result = app.inspect_readonly_btrfs_source(source)
        self.assertEqual(result.uuid, "abc-def")
        self.assertTrue(result.readonly)

    def test_nonreadonly_source_must_not_be_sent(self):
        source = self.path / "snapshot"
        source.mkdir()
        with patch.object(
            app.subprocess,
            "run",
            side_effect=[
                MagicMock(returncode=0, stdout="UUID: abc-def\n"),
                MagicMock(returncode=0, stdout="ro=false\n"),
            ],
        ):
            with self.assertRaises(ValueError):
                app.inspect_readonly_btrfs_source(source)

    def test_destination_fingerprint_survives_mount_id_renewal(self):
        a = mount.MountIdentity("nfs4", "nas:/data", "/", "/mnt/backup", "0:77", 5, 12)
        b = mount.MountIdentity("nfs4", "nas:/data", "/", "/mnt/backup", "0:77", 55, 99)
        c = mount.MountIdentity(
            "nfs4", "other:/data", "/", "/mnt/backup", "0:77", 5, 12
        )
        self.assertEqual(
            app.destination_fingerprint(a, self.path),
            app.destination_fingerprint(b, self.path),
        )
        self.assertNotEqual(
            app.destination_fingerprint(a, self.path),
            app.destination_fingerprint(c, self.path),
        )

    def test_send_argv_has_explicit_protocol_and_parent_no_shell(self):
        source = self.path / "snapshot"
        source.mkdir()
        parent = self.path / "parent"
        parent.mkdir()
        with patch.object(app.subprocess, "Popen") as launch:
            app.spawn_btrfs_send(source, parent)
        args, kwargs = launch.call_args
        self.assertEqual(
            args[0], ["btrfs", "send", "--proto", "2", "-p", str(parent), str(source)]
        )
        self.assertTrue(kwargs["start_new_session"])
        self.assertFalse(kwargs.get("shell", False))

    def test_send_argv_is_refused_for_nonabsolute_source(self):
        with self.assertRaises(ValueError):
            app.spawn_btrfs_send(Path("relative"), None)


if __name__ == "__main__":
    unittest.main()
