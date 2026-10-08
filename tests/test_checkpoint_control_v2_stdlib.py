"""Native restart-persistent control files: pause, stop and explicit resume."""

import importlib
import sys
import types
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    for name, path in (
        ("btrfs_backup_ng", "src/btrfs_backup_ng"),
        ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
    ):
        obj = types.ModuleType(name)
        obj.__path__ = [str(ROOT / path)]
        sys.modules[name] = obj
c = importlib.import_module("btrfs_backup_ng.core.checkpoint_control_v2")
ID = "c370ef15-0c25-426f-9d0f-478541219133"


class ControlTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="harbor-v2-control-")
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)

    def test_pause_state_survives_new_process_instance(self):
        first = c.ControlJournal(self.root, ID)
        first.request("pause")
        self.assertEqual(c.ControlJournal(self.root, ID).action(), "pause")

    def test_resume_explicitly_clears_pause_request(self):
        first = c.ControlJournal(self.root, ID)
        first.request("pause")
        first.request("run")
        self.assertEqual(c.ControlJournal(self.root, ID).action(), "run")

    def test_reject_unknown_control_action_and_transfer(self):
        with self.assertRaises(ValueError):
            c.ControlJournal(self.root, "bad/uuid")
        ctrl = c.ControlJournal(self.root, ID)
        with self.assertRaises(ValueError):
            ctrl.request("delete-all")

    def test_control_file_is_not_world_readable(self):
        j = c.ControlJournal(self.root, ID)
        j.request("pause")
        p = self.root / f".harbor-control-{ID}.json"
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_symlink_control_never_followed(self):
        j = c.ControlJournal(self.root, ID)
        outside = self.root / "outside"
        outside.write_text("protected")
        (self.root / f".harbor-control-{ID}.json").symlink_to(outside)
        with self.assertRaises(OSError):
            j.action()
        self.assertEqual(outside.read_text(), "protected")

    def test_stop_signal_requires_confirmed_matching_worker_identity(self):
        j = c.ControlJournal(self.root, ID)
        j.request("stop")
        with (
            patch.object(c, "_proc_starttime", return_value="different"),
            patch.object(c.os, "pidfd_open") as opening,
        ):
            self.assertFalse(j.signal_active_send(1234, "expected"))
            opening.assert_not_called()


if __name__ == "__main__":
    unittest.main()
