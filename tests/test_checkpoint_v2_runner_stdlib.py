"""Connect worker to send-process lifecycle and durable sink, synthetic process."""

import importlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    for name, folder in (
        ("btrfs_backup_ng", "src/btrfs_backup_ng"),
        ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
        ("btrfs_backup_ng.endpoint", "src/btrfs_backup_ng/endpoint"),
    ):
        module = types.ModuleType(name)
        module.__path__ = [str(ROOT / folder)]
        sys.modules[name] = module
v2 = importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")
runner = importlib.import_module("btrfs_backup_ng.core.checkpoint_v2_runner")
verify = importlib.import_module("btrfs_backup_ng.core.verify_v2")
SourceFingerprint = importlib.import_module(
    "btrfs_backup_ng.core.replay_v2"
).SourceFingerprint
DestinationFingerprint = importlib.import_module(
    "btrfs_backup_ng.core.replay_v2"
).DestinationFingerprint

TRANSFER = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"
UUID = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"


def manifest():
    return v2.ResumeManifest(
        2,
        TRANSFER,
        {
            "profile_id": "test",
            "source_volume": "/",
            "source_uuid": UUID,
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "local:test",
            "kernel_release": "7.0",
            "btrfs_progs_version": "7.1",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
        16,
        "preparing",
        (),
    )


def source():
    return SourceFingerprint(UUID, None, "/snapshots/12/snapshot", "protocol=2", True)


def target():
    return DestinationFingerprint("raw", "local:test")


class Guard:
    def __init__(self, p):
        self.path = p

    def validate_fd(self, fd):
        a = os.fstat(fd)
        b = self.path.stat()
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise RuntimeError("mount moved")


class FakeProcess:
    def __init__(self, bytes_data, exit_code=0):
        self.stdout = io.BytesIO(bytes_data)
        self.code = exit_code
        self.terminated = False
        self.waited = False

    def wait(self):
        self.waited = True
        return self.code

    def terminate(self):
        self.terminated = True


class TestRunner(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harbor-v2-runner-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.bytes_data = b"A" * 16 + b"B" * 16 + b"C" * 3
        self.parts = []

    def factory(self, exit_code=0):
        def make():
            p = FakeProcess(self.bytes_data, exit_code)
            self.parts.append(p)
            return p

        return make

    def call(self, make, action=lambda: "run", resume=False):
        return runner.execute_checkpoint_job(
            target_root=self.path,
            snapshot_name="snap",
            manifest=manifest(),
            source=source(),
            destination=target(),
            guard=Guard(self.path),
            spawn=make,
            action=action,
            resume=resume,
            chunk_size=16,
            level=3,
        )

    def test_first_run_creates_complete_restorable_archive(self):
        result = self.call(self.factory())
        self.assertEqual(result.status, "completed")
        self.assertTrue(self.parts[-1].waited)
        self.assertEqual(
            verify.verify_checkpoint_index(
                self.path / "snap.btrfs.zst.meta",
                self.path / "snap.btrfs.zst",
                full=True,
            ).status,
            "ok",
        )

    def test_paused_run_does_not_publish_and_kills_source(self):
        result = self.call(
            self.factory(),
            action=lambda: (
                "pause"
                if len(self.parts) and self.parts[0].stdout.tell() >= 16
                else "run"
            ),
        )
        self.assertEqual(result.status, "paused")
        self.assertTrue(self.parts[-1].terminated)
        self.assertFalse((self.path / "snap.btrfs.zst").exists())

    def test_resume_after_pause_replays_and_finishes(self):
        self.call(
            self.factory(),
            action=lambda: (
                "pause"
                if len(self.parts) and self.parts[0].stdout.tell() >= 16
                else "run"
            ),
        )
        result = self.call(self.factory(), resume=True)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.new_raw_bytes, len(self.bytes_data) - 16)
        self.assertEqual(
            verify.verify_checkpoint_index(
                self.path / "snap.btrfs.zst.meta",
                self.path / "snap.btrfs.zst",
                full=True,
            ).status,
            "ok",
        )

    def test_failed_send_exit_keeps_partial_not_complete(self):
        with self.assertRaises(RuntimeError):
            self.call(self.factory(exit_code=3))
        self.assertFalse((self.path / "snap.btrfs.zst").exists())

    def test_wrong_source_identity_refused_before_spawning(self):
        wrong = SourceFingerprint(
            "different", None, "/snapshots/12/snapshot", "protocol=2", True
        )
        with self.assertRaises(ValueError):
            runner.execute_checkpoint_job(
                target_root=self.path,
                snapshot_name="snap",
                manifest=manifest(),
                source=wrong,
                destination=target(),
                guard=Guard(self.path),
                spawn=self.factory(),
                action=lambda: "run",
                chunk_size=16,
                level=3,
            )
        self.assertEqual(len(self.parts), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
