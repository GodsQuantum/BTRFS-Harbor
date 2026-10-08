"""Finalization and recovery: v2 streams are verifiable or fail closed."""

import hashlib
import importlib
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    for name, path in (
        ("btrfs_backup_ng", "src/btrfs_backup_ng"),
        ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
        ("btrfs_backup_ng.endpoint", "src/btrfs_backup_ng/endpoint"),
    ):
        mod = types.ModuleType(name)
        mod.__path__ = [str(ROOT / path)]
        sys.modules[name] = mod
v2 = importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")
sinkapi = importlib.import_module("btrfs_backup_ng.endpoint.resumable_raw")
frames = importlib.import_module("btrfs_backup_ng.core.checkpoint_stream")
verify = importlib.import_module("btrfs_backup_ng.core.verify_v2")


class Guard:
    def __init__(self, p):
        self.p = p

    def validate_fd(self, fd):
        a = os.fstat(fd)
        b = self.p.stat()
        if (a.st_dev, a.st_ino) != (b.st_dev, b.st_ino):
            raise RuntimeError("mount changed")


ID = "c370ef15-0c25-426f-9d0f-478541219133"
UUID = "62741350-03b5-4688-b17a-fd9c9016e1d9"


def manifest():
    return v2.ResumeManifest(
        2,
        ID,
        {
            "profile_id": "test",
            "source_volume": "/",
            "source_uuid": UUID,
            "source_path": "/.snapshots/12/snapshot",
            "parent_uuid": None,
            "send_fingerprint": "protocol=2",
            "compression": "zstd",
            "compression_level": 3,
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


def meta(size, checksum):
    import json

    return json.dumps(
        {
            "version": 2,
            "name": "snap",
            "source_uuid": UUID,
            "size": size,
            "pipeline": {"compress": "zstd", "encrypt": None},
            "checksum": {"algorithm": "sha256", "value": checksum},
            "provenance": {"origin": "native-write", "stream_completeness": "complete"},
        }
    ).encode()


class VerifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="harbor-v2-verify-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def create(self):
        sink = sinkapi.ResumableRawSink.open_new(
            self.root, "snap", manifest(), Guard(self.root)
        )
        block = b"GOOD CHECKPOINT"
        frame = frames.compress_frame(block, level=3, threads=1)
        sink.append_frame(
            raw_sha256=hashlib.sha256(block).hexdigest(),
            raw_length=len(block),
            frame=frame,
        )
        return sink, frame

    def test_complete_zstd_stream_verifies(self):
        sink, frame = self.create()
        sink.publish(meta(len(frame), hashlib.sha256(frame).hexdigest()))
        result = verify.verify_checkpoint_index(
            self.root / "snap.btrfs.zst.meta", self.root / "snap.btrfs.zst", full=True
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.checkpoints, 1)

    def test_part_only_is_never_final(self):
        sink, frame = self.create()
        sink.close()
        result = verify.verify_checkpoint_index(
            self.root / "snap.btrfs.zst.meta", self.root / "snap.btrfs.zst"
        )
        self.assertEqual(result.status, "incomplete")

    def test_tampered_compressed_payload_detected(self):
        sink, frame = self.create()
        sink.publish(meta(len(frame), hashlib.sha256(frame).hexdigest()))
        path = self.root / "snap.btrfs.zst"
        data = bytearray(path.read_bytes())
        data[5] ^= 1
        path.write_bytes(data)
        self.assertEqual(
            verify.verify_checkpoint_index(
                self.root / "snap.btrfs.zst.meta", path, full=True
            ).status,
            "corrupt",
        )

    def test_missing_checkpoint_manifest_not_claimed_verified(self):
        sink, frame = self.create()
        sink.publish(meta(len(frame), hashlib.sha256(frame).hexdigest()))
        (self.root / f".harbor-resume-{ID}.json").unlink()
        self.assertEqual(
            verify.verify_checkpoint_index(
                self.root / "snap.btrfs.zst.meta", self.root / "snap.btrfs.zst"
            ).status,
            "unverifiable",
        )

    def test_post_publish_crash_does_not_mark_complete_while_final_missing(self):
        sink, frame = self.create()
        original = sinkapi._rename_noreplace

        def fail(fd, old, new):
            if new == "snap.btrfs.zst":
                raise OSError("publish crash")
            return original(fd, old, new)

        from unittest.mock import patch

        with patch.object(sinkapi, "_rename_noreplace", fail):
            with self.assertRaises(OSError):
                sink.publish(meta(len(frame), hashlib.sha256(frame).hexdigest()))
        self.assertEqual(
            verify.verify_checkpoint_index(
                self.root / "snap.btrfs.zst.meta", self.root / "snap.btrfs.zst"
            ).status,
            "incomplete",
        )


if __name__ == "__main__":
    unittest.main()
