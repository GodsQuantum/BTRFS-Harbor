"""Persistent pause/stop lifecycle over durable checkpoint stream, synthetic only."""

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
        m = types.ModuleType(name)
        m.__path__ = [str(ROOT / folder)]
        sys.modules[name] = m
v2 = importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")
sinkapi = importlib.import_module("btrfs_backup_ng.endpoint.resumable_raw")
replay = importlib.import_module("btrfs_backup_ng.core.replay_v2")
worker = importlib.import_module("btrfs_backup_ng.core.resumable_worker")

SOURCE = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"
TRANSFER = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"


class Guard:
    def __init__(self, p):
        self.p = p

    def validate_fd(self, fd):
        a = os.fstat(fd)
        b = self.p.stat()
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise RuntimeError("mount changed")


def manifest():
    return v2.ResumeManifest(
        2,
        TRANSFER,
        {
            "profile_id": "test",
            "source_volume": "/",
            "source_uuid": SOURCE,
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 1,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "local:test",
            "kernel_release": "7.0",
            "btrfs_progs_version": "7",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
        16,
        "preparing",
        (),
    )


def identity():
    return replay.SourceFingerprint(
        SOURCE, None, "/snapshots/12/snapshot", "protocol=2", True
    )


def dest():
    return replay.DestinationFingerprint("raw", "local:test")


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harbor-worker-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def create(self):
        return sinkapi.ResumableRawSink.open_new(
            self.root, "snap", manifest(), Guard(self.root)
        )

    def reopen(self):
        return sinkapi.load_existing(
            self.root, "snap", TRANSFER, Guard(self.root), expected_manifest=manifest()
        )

    def test_pause_finishes_checkpoint_and_stops_reader(self):
        with self.create() as sink:
            observed = []

            def action():
                return "pause" if len(sink.manifest.checkpoints) >= 1 else "run"

            result = worker.run_worker(
                io.BytesIO(b"A" * 16 + b"B" * 16),
                sink,
                action=action,
                shutdown_source=lambda: observed.append("shutdown"),
                chunk_size=16,
                level=1,
            )
            self.assertEqual(result.status, "paused")
            self.assertEqual(len(sink.manifest.checkpoints), 1)
            self.assertEqual(observed, ["shutdown"])
        with self.reopen() as sink:
            self.assertEqual(sink.manifest.state, "paused")
            self.assertEqual(len(sink.manifest.checkpoints), 1)

    def test_resume_after_pause_replays_original_snapshot_and_only_new_frame(self):
        payload = b"A" * 16 + b"B" * 16 + b"C" * 5
        with self.create() as sink:
            worker.run_worker(
                io.BytesIO(payload),
                sink,
                action=lambda: (
                    "pause" if len(sink.manifest.checkpoints) >= 1 else "run"
                ),
                chunk_size=16,
                level=1,
            )
        with self.reopen() as sink:
            result = worker.resume_worker(
                io.BytesIO(payload),
                sink,
                identity(),
                dest(),
                action=lambda: "run",
                chunk_size=16,
                level=1,
            )
            self.assertEqual(result.status, "ready_to_finalize")
            self.assertEqual(result.new_raw_bytes, len(payload) - 16)
            self.assertEqual(len(sink.manifest.checkpoints), 3)

    def test_stop_now_keeps_valid_checkpoints_and_no_finalize(self):
        with self.create() as sink:
            result = worker.run_worker(
                io.BytesIO(b"A" * 32),
                sink,
                action=lambda: "stop" if len(sink.manifest.checkpoints) else "run",
                chunk_size=16,
                level=1,
            )
            self.assertEqual(result.status, "failed_resumable")
            self.assertEqual(len(sink.manifest.checkpoints), 1)
        with self.reopen() as sink:
            self.assertEqual(sink.manifest.state, "failed_resumable")

    def test_discard_requires_confirmation_and_cannot_delete_final(self):
        with self.create() as sink:
            worker.run_worker(
                io.BytesIO(b"A" * 16),
                sink,
                action=lambda: "pause" if len(sink.manifest.checkpoints) else "run",
                chunk_size=16,
                level=1,
            )
            with self.assertRaises(PermissionError):
                sink.discard(confirmed=False)
            self.assertTrue((self.root / sink.part_name).exists())
            sink.discard(confirmed=True)
            self.assertFalse((self.root / sink.part_name).exists())
            self.assertFalse((self.root / sink.manifest_name).exists())

    def test_failed_source_exit_cannot_be_marked_ready(self):
        with self.create() as sink:
            with self.assertRaisesRegex(RuntimeError, "source"):
                worker.run_worker(
                    io.BytesIO(b"A" * 32),
                    sink,
                    action=lambda: "run",
                    chunk_size=16,
                    level=1,
                    source_exit_status=lambda: 1,
                )
            self.assertNotEqual(sink.manifest.state, "finalizing")

    def test_failed_send_short_tail_never_commits_or_breaks_resume(self):
        # A crashed btrfs send may close stdout after writing only part of
        # the next raw checkpoint. Its short final block is NOT durable.
        with self.create() as sink:
            with self.assertRaisesRegex(RuntimeError, "source"):
                worker.run_worker(
                    io.BytesIO(b"A" * 16 + b"B" * 5),
                    sink,
                    action=lambda: "run",
                    chunk_size=16,
                    level=1,
                    source_exit_status=lambda: 17,
                )
            self.assertEqual(len(sink.manifest.checkpoints), 1)
            self.assertEqual(sink.manifest.checkpoints[0].raw_length, 16)
        with self.reopen() as sink:
            result = worker.resume_worker(
                io.BytesIO(b"A" * 16 + b"B" * 16 + b"C" * 3),
                sink,
                identity(),
                dest(),
                action=lambda: "run",
                chunk_size=16,
                level=1,
                source_exit_status=lambda: 0,
            )
            self.assertEqual(result.status, "ready_to_finalize")
            self.assertEqual(len(sink.manifest.checkpoints), 3)

    def test_pause_already_requested_before_read_does_not_read(self):
        class Refuse(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("read when paused")

        with self.create() as sink:
            result = worker.run_worker(
                Refuse(b"A" * 16), sink, action=lambda: "pause", chunk_size=16, level=1
            )
            self.assertEqual(result.status, "paused")
            self.assertEqual(len(sink.manifest.checkpoints), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
