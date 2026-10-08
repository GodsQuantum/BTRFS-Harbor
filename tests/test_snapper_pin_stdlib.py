"""Native Snapper cleanup pin transaction, tested without real Snapper changes."""

import importlib
import sys
import types
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name, folder in (
    ("btrfs_backup_ng", "src/btrfs_backup_ng"),
    ("btrfs_backup_ng.snapper", "src/btrfs_backup_ng/snapper"),
):
    obj = types.ModuleType(name)
    obj.__path__ = [str(ROOT / folder)]
    sys.modules[name] = obj
m = importlib.import_module("btrfs_backup_ng.snapper.pin")


class PinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harbor-pin-")
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "pin.json"
        self.state = m.LiveSnapshot("uuid-first", "timeline")
        self.actions = []

        def query(config, number):
            return self.state

        def modify(config, number, cleanup):
            self.actions.append((config, number, cleanup))
            self.state = m.LiveSnapshot(self.state.uuid, cleanup)

        self.pins = m.PinManager(self.path, query=query, modify=modify)

    def test_acquire_native_cleanup_before_send(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        self.assertEqual(self.state.cleanup, "")
        self.assertEqual(self.actions, [("root", 12, "")])
        self.assertEqual(self.pins.reconcile(), {"root:12": "healthy"})

    def test_concurrent_transfer_locks_and_last_release_restores(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        self.pins.acquire("root", 12, "uuid-first", "t2")
        self.assertEqual(len(self.actions), 1)
        self.pins.release("root", 12, "uuid-first", "t1")
        self.assertEqual(self.state.cleanup, "")
        self.pins.release("root", 12, "uuid-first", "t2")
        self.assertEqual(self.state.cleanup, "timeline")
        self.assertEqual(self.actions, [("root", 12, ""), ("root", 12, "timeline")])

    def test_changed_uuid_never_restores_cleanup(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        self.state = m.LiveSnapshot("new-uuid", "")
        self.pins.release("root", 12, "uuid-first", "t1")
        self.assertEqual(self.actions, [("root", 12, "")])

    def test_manual_source_removal_retains_pin_for_diagnostics(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        self.state = None
        self.assertEqual(self.pins.reconcile(), {"root:12": "needs-source"})

    def test_crash_after_native_cleanup_can_reconcile_without_repin(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        again = m.PinManager(
            self.path,
            query=lambda c, n: self.state,
            modify=lambda c, n, v: self.actions.append((c, n, v)),
        )
        self.assertEqual(again.reconcile(), {"root:12": "healthy"})
        self.assertEqual(self.actions, [("root", 12, "")])

    def test_external_cleanup_restoration_detected_not_silent(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        self.state = m.LiveSnapshot("uuid-first", "timeline")
        self.assertEqual(self.pins.reconcile(), {"root:12": "re-pin-needed"})

    def test_refuse_discard_unknown_transfer(self):
        self.pins.acquire("root", 12, "uuid-first", "t1")
        with self.assertRaises(ValueError):
            self.pins.release("root", 12, "uuid-first", "t-other")
        self.assertEqual(self.state.cleanup, "")


if __name__ == "__main__":
    unittest.main()
