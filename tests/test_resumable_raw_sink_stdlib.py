"""Raw v2 sink contracts, runnable without loading pytest under RAM pressure."""

import hashlib
import importlib
import io
import subprocess
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SOURCE_UUID = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"
TRANSFER_ID = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"


def load_api():
    if os.environ.get("HARBOR_LIGHT_TEST") == "1":
        # Only bypass package __init__ during the standalone standard-library run.
        # The actual installed modules are exercised by normal pytest imports.
        for name, relative in (
            ("btrfs_backup_ng", "src/btrfs_backup_ng"),
            ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
            ("btrfs_backup_ng.endpoint", "src/btrfs_backup_ng/endpoint"),
        ):
            module = types.ModuleType(name)
            module.__path__ = [str(ROOT / relative)]
            sys.modules[name] = module
    core = importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")
    sink = importlib.import_module("btrfs_backup_ng.endpoint.resumable_raw")
    return core, sink


def sample_manifest(v2):
    return v2.ResumeManifest(
        schema_version=2,
        transfer_id=TRANSFER_ID,
        identity={
            "profile_id": "profile-11",
            "source_volume": "/",
            "source_uuid": SOURCE_UUID,
            "source_path": "/snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
            "encryption": "none",
            "destination_type": "raw",
            "destination_fingerprint": "verified:storage",
            "kernel_release": "6.12.0",
            "btrfs_progs_version": "7.1",
            "harbor_version": "0.2.6",
            "engine_version": "0.9.12",
        },
        checkpoint_size=16,
        state="preparing",
        checkpoints=(),
    )


class Guard:
    def __init__(self, root):
        self.root = root
        self.identity = (root.stat().st_dev, root.stat().st_ino)
        self.validations = 0

    def validate_fd(self, directory_fd):
        self.validations += 1
        actual = os.fstat(directory_fd)
        if (actual.st_dev, actual.st_ino) != self.identity:
            raise RuntimeError("pinned directory identity changed")
        if not self.root.exists():
            raise RuntimeError("destination mount identity changed")
        current = self.root.stat()
        if (current.st_dev, current.st_ino) != self.identity:
            raise RuntimeError("destination mount identity changed")


def meta_bytes(name, size):
    return json.dumps(
        {
            "version": 2,
            "name": name,
            "size": size,
            "source_uuid": SOURCE_UUID,
            "pipeline": {"compress": "zstd", "encrypt": None},
            "provenance": {"origin": "native-write", "stream_completeness": "complete"},
        }
    ).encode("utf-8")


class TestResumableRawSink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v2, cls.api = load_api()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="harbor-v2-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.guard = Guard(self.root)
        self.manifest = sample_manifest(self.v2)

    def open_new(self, name="snap"):
        return self.api.ResumableRawSink.open_new(
            self.root, name, self.manifest, self.guard
        )

    def test_open_new_creates_one_part_and_hidden_manifest(self):
        with self.open_new() as sink:
            self.assertEqual(sink.part_name, f"snap.btrfs.zst.{TRANSFER_ID}.part")
            self.assertTrue(sink.manifest_name.startswith(".harbor-resume-"))
            self.assertEqual((self.root / sink.part_name).read_bytes(), b"")
            self.assertFalse((self.root / "snap.btrfs.zst").exists())
            self.assertGreaterEqual(self.guard.validations, 2)

    def test_append_durable_frame_and_reopen(self):
        with self.open_new() as sink:
            raw = b"frame1"
            frame = b"compressedframe"
            checkpoint = sink.append_frame(
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                raw_length=len(raw),
                frame=frame,
            )
            self.assertEqual(checkpoint.sequence, 0)
            self.assertEqual(checkpoint.compressed_length, len(frame))
            self.assertEqual(checkpoint.compressed_offset, 0)
            self.assertEqual((self.root / sink.part_name).read_bytes(), frame)
        with self.api.load_existing(
            self.root,
            "snap",
            TRANSFER_ID,
            Guard(self.root),
            expected_manifest=self.manifest,
        ) as reopened:
            self.assertEqual(reopened.manifest.checkpoints, (checkpoint,))
            self.assertFalse(reopened.append_allowed)

    def test_recovers_uncommitted_tail_to_committed_prefix(self):
        with self.open_new() as sink:
            sink.append_frame(raw_sha256="a" * 64, raw_length=1, frame=b"frame")
            filename = sink.part_name
        with (self.root / filename).open("ab") as f:
            f.write(b"UNCOMMITTED")
        with self.api.load_existing(
            self.root,
            "snap",
            TRANSFER_ID,
            Guard(self.root),
            expected_manifest=self.manifest,
        ):
            self.assertEqual((self.root / filename).read_bytes(), b"frame")

    def test_rejects_mismatched_resume_identity_without_changing_bytes(self):
        with self.open_new() as sink:
            sink.append_frame(raw_sha256="a" * 64, raw_length=1, frame=b"frame")
            filename = sink.part_name
        from dataclasses import replace

        changed = replace(
            self.manifest,
            identity={
                **self.manifest.identity,
                "destination_fingerprint": "different-share",
            },
        )
        with self.assertRaisesRegex(ValueError, "identity"):
            self.api.load_existing(
                self.root,
                "snap",
                TRANSFER_ID,
                Guard(self.root),
                expected_manifest=changed,
            )
        self.assertEqual((self.root / filename).read_bytes(), b"frame")

    def test_mount_replacement_refuses_append_to_underlying_directory(self):
        with self.open_new() as sink:
            detached = self.root.with_name(self.root.name + "-detached")
            self.root.rename(detached)
            self.root.mkdir()
            try:
                with self.assertRaisesRegex(RuntimeError, "destination mount"):
                    sink.append_frame(raw_sha256="a" * 64, raw_length=1, frame=b"frame")
                self.assertEqual(list(self.root.iterdir()), [])
                self.assertEqual((detached / sink.part_name).stat().st_size, 0)
            finally:
                self.root.rmdir()
                detached.rename(self.root)

    def test_final_exists_never_overwritten(self):
        with self.open_new() as sink:
            sink.append_frame(raw_sha256="a" * 64, raw_length=1, frame=b"frame")
            final = self.root / "snap.btrfs.zst"
            final.write_bytes(b"old verified backup")
            with self.assertRaises(FileExistsError):
                sink.publish(meta_bytes("snap", len(b"frame")))
            self.assertEqual(final.read_bytes(), b"old verified backup")

    def test_partial_only_not_a_final(self):
        with self.open_new() as sink:
            sink.append_frame(raw_sha256="a" * 64, raw_length=1, frame=b"frame")
            self.assertFalse((self.root / "snap.btrfs.zst").exists())
            sink.publish(meta_bytes("snap", len(b"frame")))
            self.assertEqual((self.root / "snap.btrfs.zst").read_bytes(), b"frame")
            self.assertTrue((self.root / "snap.btrfs.zst.meta").is_file())

    def test_published_stream_cannot_be_appended_to(self):
        with self.open_new() as sink:
            sink.append_frame(raw_sha256="a" * 64, raw_length=16, frame=b"frame")
            sink.publish(meta_bytes("snap", len(b"frame")))
            with self.assertRaises((ValueError, RuntimeError)):
                sink.append_frame(raw_sha256="b" * 64, raw_length=1, frame=b"BAD")
        self.assertEqual((self.root / "snap.btrfs.zst").read_bytes(), b"frame")

    def test_publish_crash_after_meta_stays_without_final(self):
        with self.open_new() as sink:
            sink.append_frame(raw_sha256="a" * 64, raw_length=1, frame=b"frame")
            original = self.api._rename_noreplace

            def fail_final(fd, old, new):
                if new == "snap.btrfs.zst":
                    raise OSError("injected publish crash")
                return original(fd, old, new)

            with patch.object(self.api, "_rename_noreplace", fail_final):
                with self.assertRaisesRegex(OSError, "injected"):
                    sink.publish(meta_bytes("snap", len(b"frame")))
            self.assertFalse((self.root / "snap.btrfs.zst").exists())
            self.assertTrue((self.root / "snap.btrfs.zst.meta").exists())
            self.assertTrue((self.root / sink.part_name).exists())

    def test_second_writer_cannot_reopen_active_partial(self):
        with self.open_new():
            with self.assertRaises(BlockingIOError):
                with self.api.load_existing(
                    self.root,
                    "snap",
                    TRANSFER_ID,
                    Guard(self.root),
                    expected_manifest=self.manifest,
                ):
                    self.fail("second writer must never share an active .part")

    def test_checkpointed_stream_final_decompresses_with_standard_zstd(self):
        from dataclasses import replace

        stream = importlib.import_module("btrfs_backup_ng.core.checkpoint_stream")
        self.manifest = replace(self.manifest, checkpoint_size=16)
        raw = bytes(range(60)) * 3 + b"terminal"
        with self.open_new() as sink:
            completed = stream.feed_checkpointed_stream(
                io.BytesIO(raw), sink, chunk_size=16, level=3, threads=1
            )
            self.assertEqual(completed.raw_bytes, len(raw))
            sink.publish(meta_bytes("snap", completed.compressed_bytes))
        decoded = subprocess.run(
            ["zstd", "-q", "-dc"],
            input=(self.root / "snap.btrfs.zst").read_bytes(),
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        self.assertEqual(decoded, raw)

    def test_replayed_prefix_unlocks_only_the_missing_tail(self):
        from dataclasses import replace

        stream = importlib.import_module("btrfs_backup_ng.core.checkpoint_stream")
        replay = importlib.import_module("btrfs_backup_ng.core.replay_v2")
        self.manifest = replace(self.manifest, checkpoint_size=16)
        prefix = b"A" * 16 + b"B" * 16
        raw = prefix + b"new tail"
        with self.open_new() as sink:
            for block in (b"A" * 16, b"B" * 16):
                sink.append_frame(
                    raw_sha256=hashlib.sha256(block).hexdigest(),
                    raw_length=len(block),
                    frame=stream.compress_frame(block, level=3, threads=1),
                )
        with self.api.load_existing(
            self.root,
            "snap",
            TRANSFER_ID,
            Guard(self.root),
            expected_manifest=self.manifest,
        ) as resumed:
            reader = io.BytesIO(raw)
            with self.assertRaises(RuntimeError):
                resumed.append_frame(raw_sha256="b" * 64, raw_length=1, frame=b"frame")
            proof = replay.replay_committed(
                reader,
                resumed.manifest,
                source=replay.SourceFingerprint(
                    uuid=SOURCE_UUID,
                    parent_uuid=None,
                    path="/snapshots/12/snapshot",
                    send_fingerprint="protocol=2",
                    readonly=True,
                ),
                destination=replay.DestinationFingerprint(
                    type="raw", fingerprint="verified:storage"
                ),
            )
            self.assertEqual(reader.read(0), b"")
            resumed.authorize_replayed_prefix(proof)
            summary = stream.feed_checkpointed_stream(
                reader, resumed, chunk_size=16, level=3, threads=1
            )
            self.assertEqual(summary.raw_bytes, len(raw) - len(prefix))
            size = sum(c.compressed_length for c in resumed.manifest.checkpoints)
            resumed.publish(meta_bytes("snap", size))
        decoded = subprocess.run(
            ["zstd", "-q", "-dc"],
            input=(self.root / "snap.btrfs.zst").read_bytes(),
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        self.assertEqual(decoded, raw)

    def test_invalid_replay_proof_does_not_enable_append(self):
        replay = importlib.import_module("btrfs_backup_ng.core.replay_v2")
        with self.open_new() as sink:
            block = b"AAAA"
            sink.append_frame(
                raw_sha256=hashlib.sha256(block).hexdigest(),
                raw_length=4,
                frame=b"frame",
            )
        with self.api.load_existing(
            self.root,
            "snap",
            TRANSFER_ID,
            Guard(self.root),
            expected_manifest=self.manifest,
        ) as resumed:
            invalid = replay.ReplayProof(
                transfer_id=TRANSFER_ID,
                matched_checkpoints=1,
                matched_raw_bytes=4,
                checkpoint_index_sha256="f" * 64,
            )
            with self.assertRaises(ValueError):
                resumed.authorize_replayed_prefix(invalid)
            self.assertFalse(resumed.append_allowed)

    def test_symlink_partial_refuses_resume(self):
        with self.open_new() as sink:
            name = sink.part_name
        outside = self.root / "outside"
        outside.write_bytes(b"do not change")
        (self.root / name).unlink()
        (self.root / name).symlink_to(outside)
        with self.assertRaises(OSError):
            self.api.load_existing(
                self.root,
                "snap",
                TRANSFER_ID,
                Guard(self.root),
                expected_manifest=self.manifest,
            )
        self.assertEqual(outside.read_bytes(), b"do not change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
