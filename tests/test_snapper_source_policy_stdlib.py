"""Snapper stable candidate selection; sources tested by UUID not number."""

import importlib
import sys
import types
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for name, folder in (
    ("btrfs_backup_ng", "src/btrfs_backup_ng"),
    ("btrfs_backup_ng.snapper", "src/btrfs_backup_ng/snapper"),
):
    obj = types.ModuleType(name)
    obj.__path__ = [str(ROOT / folder)]
    sys.modules[name] = obj
p = importlib.import_module("btrfs_backup_ng.snapper.source_policy")
NOW = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)


@dataclass
class FakeSnapshot:
    config_name: str
    number: int
    snapshot_type: str
    date: datetime
    uuid: str
    pre_num: int | None = None
    readonly: bool = True
    exists: bool = True


def choose(items, remote=frozenset(), kind="latest", selected=None):
    return p.select_source(
        items,
        mode=kind,
        resolve=lambda s: p.ResolvedSource(s.uuid, s.readonly, s.exists),
        remote_uuids=set(remote),
        now=NOW,
        selected_number=selected,
    )


class SourcePolicy(unittest.TestCase):
    def test_single_after_short_settle(self):
        a = FakeSnapshot("root", 1, "single", NOW - timedelta(seconds=30), "u1")
        b = FakeSnapshot("root", 2, "single", NOW - timedelta(seconds=10), "u2")
        self.assertEqual(choose([a, b]).snapshot.number, 1)

    def test_no_one_hour_delay(self):
        a = FakeSnapshot("root", 1, "single", NOW - timedelta(seconds=42), "u1")
        self.assertEqual(choose([a]).snapshot.number, 1)

    def test_lone_pre_is_not_automatically_chosen(self):
        self.assertIsNone(
            choose([FakeSnapshot("root", 2, "pre", NOW - timedelta(minutes=1), "u2")])
        )

    def test_matched_post_preference_and_unmatched_post_skipped(self):
        pre = FakeSnapshot("root", 3, "pre", NOW - timedelta(seconds=50), "u3")
        post = FakeSnapshot(
            "root", 4, "post", NOW - timedelta(seconds=5), "u4", pre_num=3
        )
        unmatched = FakeSnapshot("root", 8, "post", NOW, "u8", pre_num=7)
        self.assertEqual(choose([pre, post, unmatched]).snapshot.number, 4)

    def test_remote_uuid_already_saved_skipped(self):
        one = FakeSnapshot("root", 1, "single", NOW - timedelta(minutes=1), "u1")
        two = FakeSnapshot("root", 2, "single", NOW - timedelta(seconds=40), "u2")
        self.assertEqual(choose([one, two], remote={"u2"}).snapshot.number, 1)

    def test_explicit_pre_permitted_and_readonly_required(self):
        pre = FakeSnapshot("root", 2, "pre", NOW - timedelta(minutes=1), "u2")
        self.assertEqual(choose([pre], kind="selected", selected=2).snapshot.number, 2)
        pre.readonly = False
        with self.assertRaises(ValueError):
            choose([pre], kind="selected", selected=2)

    def test_missing_or_changed_uuid_rejected(self):
        s = FakeSnapshot(
            "root", 2, "single", NOW - timedelta(minutes=1), "u2", exists=False
        )
        self.assertIsNone(choose([s]))
        s.exists = True
        s.uuid = ""
        self.assertIsNone(choose([s]))

    def test_create_new_is_explicit_action(self):
        self.assertEqual(choose([], kind="create").action, "create_snapshot")


if __name__ == "__main__":
    unittest.main()
