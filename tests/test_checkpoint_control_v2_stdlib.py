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


class ActiveProcessRegistryTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(prefix="harbor-active-")
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        self.journal = c.ControlJournal(
            self.root, "c370ef15-0c25-426f-9d0f-478541219133"
        )

    def test_registered_send_identity_persists_across_new_controller(self):
        with patch.object(c, "_proc_starttime", return_value="fake-start-123"):
            self.journal.register_send(10234)
        restarted = c.ControlJournal(self.root, "c370ef15-0c25-426f-9d0f-478541219133")
        assert restarted.read_active_send() == (10234, "fake-start-123")

    def test_stop_uses_pidfd_only_for_matching_registered_process(self):
        with patch.object(c, "_proc_starttime", return_value="fake-start-123"):
            self.journal.register_send(10234)
        self.journal.request("stop")
        with patch.object(
            self.journal, "signal_active_send", return_value=True
        ) as signaller:
            assert self.journal.signal_registered_send()
            signaller.assert_called_once_with(10234, "fake-start-123")

    def test_missing_active_send_never_sends_signal(self):
        self.journal.request("stop")
        assert not self.journal.signal_registered_send()

    def test_clearing_active_record_never_clears_control_request(self):
        with patch.object(c, "_proc_starttime", return_value="fake-start-123"):
            self.journal.register_send(10234)
        self.journal.request("pause")
        self.journal.clear_active_send()
        assert self.journal.read_active_send() is None
        assert self.journal.action() == "pause"


if __name__ == "__main__":
    unittest.main()
