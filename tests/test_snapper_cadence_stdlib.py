"""Send schedule must never erase/rebase a paused transfer."""

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
    ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
    ("btrfs_backup_ng.snapper", "src/btrfs_backup_ng/snapper"),
):
    m = types.ModuleType(name)
    m.__path__ = [str(ROOT / folder)]
    sys.modules[name] = m
mod = importlib.import_module("btrfs_backup_ng.core.snapper_cadence")
NOW = datetime(2026, 10, 8, 21, 0, tzinfo=timezone.utc)


@dataclass
class Snap:
    config_name: str
    number: int
    snapshot_type: str
    date: datetime
    uuid: str
    pre_num: int | None = None
    readonly: bool = True
    exists: bool = True


def choose(
    cadence="daily", pending=(), sent=(), remote=(), items=None, trigger="timer"
):
    if items is None:
        items = [
            Snap("root", 3, "single", NOW - timedelta(minutes=1), "u3"),
            Snap("root", 4, "single", NOW - timedelta(seconds=35), "u4"),
        ]
    return mod.plan_remote_send(
        snapshots=items,
        source_mode="latest",
        resolve=lambda s: mod.ResolvedSource(s.uuid, s.readonly, s.exists),
        cadence=cadence,
        now=NOW,
        pending=list(pending),
        sent=list(sent),
        remote_uuids=set(remote),
        trigger=trigger,
    )


class CadenceTest(unittest.TestCase):
    def test_daily_latest_chooses_only_newest(self):
        result = choose()
        self.assertEqual(result.action, "start")
        self.assertEqual(result.source.snapshot.number, 4)

    def test_daily_never_replaces_pending_resume(self):
        p = mod.PendingTransfer("tx-old", "original-uuid", "paused")
        result = choose(pending=[p])
        self.assertEqual(result.action, "resume_first")
        self.assertEqual(result.pending_transfer_id, "tx-old")
        self.assertIsNone(result.source)

    def test_stop_and_failed_resumable_also_protected(self):
        for status in ("failed_resumable", "replaying", "pause_requested"):
            self.assertEqual(
                choose(pending=[mod.PendingTransfer("tx", "u0", status)]).action,
                "resume_first",
            )

    def test_daily_sent_today_no_second_send(self):
        self.assertEqual(choose(sent=[NOW - timedelta(hours=3)]).action, "not_due")

    def test_newer_local_snapshot_not_all_transferred(self):
        self.assertEqual(choose(remote=["u4"]).action, "no_snapshot")

    def test_after_snapshot_needs_event(self):
        self.assertEqual(
            choose(cadence="after_snapshot", trigger="timer").action, "not_due"
        )
        self.assertEqual(
            choose(cadence="after_snapshot", trigger="snapshot").action, "start"
        )

    def test_hourly_respects_last_run(self):
        self.assertEqual(
            choose(cadence="hourly", sent=[NOW - timedelta(minutes=5)]).action,
            "not_due",
        )
        self.assertEqual(
            choose(cadence="hourly", sent=[NOW - timedelta(hours=2)]).action, "start"
        )

    def test_lone_pre_skipped(self):
        result = choose(
            items=[Snap("root", 12, "pre", NOW - timedelta(minutes=1), "u12")]
        )
        self.assertEqual(result.action, "no_snapshot")

    def test_two_snapper_configs_need_explicit_source_scope(self):
        a = Snap("home", 7, "single", NOW - timedelta(minutes=1), "home-u")
        b = Snap("root", 8, "single", NOW - timedelta(seconds=31), "root-u")
        self.assertEqual(choose(items=[a, b]).source.source_uuid, "root-u")


if __name__ == "__main__":
    unittest.main()
