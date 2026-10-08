"""Replay is source-local only and must never write committed destination bytes."""

import hashlib
import importlib
import io
import os
import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_UUID = "b0cbeb30-9917-44d8-9f27-1ed967f91a2d"
TRANSFER_ID = "3b88e8c1-5cb8-4f67-aa10-f9544b6228f0"


def load_modules():
    if os.environ.get("HARBOR_LIGHT_TEST") == "1":
        for name, relative in (
            ("btrfs_backup_ng", "src/btrfs_backup_ng"),
            ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
        ):
            module = types.ModuleType(name)
            module.__path__ = [str(ROOT / relative)]
            sys.modules[name] = module
    manifest = importlib.import_module("btrfs_backup_ng.core.checkpoint_v2")
    replay = importlib.import_module("btrfs_backup_ng.core.replay_v2")
    return manifest, replay


def sample_manifest(v2):
    raw = (b"ABCD", b"EFGH")
    checkpoints = []
    for seq, payload in enumerate(raw):
        checkpoints.append(
            v2.Checkpoint(
                sequence=seq,
                raw_offset=seq * 4,
                raw_length=4,
                compressed_offset=seq * 3,
                compressed_length=3,
                raw_sha256=hashlib.sha256(payload).hexdigest(),
                compressed_sha256="a" * 64,
                committed_at="2026-10-08T20:00:00+00:00",
            )
        )
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
        checkpoint_size=4,
        state="paused",
        checkpoints=tuple(checkpoints),
    )


class TestReplayV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.v2, cls.api = load_modules()

    def setUp(self):
        self.manifest = sample_manifest(self.v2)
        self.source = self.api.SourceFingerprint(
            uuid=SOURCE_UUID,
            parent_uuid=None,
            path="/snapshots/12/snapshot",
            send_fingerprint="protocol=2",
            readonly=True,
        )
        self.destination = self.api.DestinationFingerprint(
            type="raw", fingerprint="verified:storage"
        )

    def replay(
        self, reader, *, manifest=None, source=None, destination=None, progress=None
    ):
        return self.api.replay_committed(
            reader,
            manifest or self.manifest,
            source=source or self.source,
            destination=destination or self.destination,
            progress=progress,
        )

    def test_replay_skips_every_committed_checkpoint_and_leaves_tail_unread(self):
        reader = io.BytesIO(b"ABCDEFGHTAIL_NEW")
        progress = []
        proof = self.replay(reader, progress=progress.append)
        self.assertEqual(reader.read(), b"TAIL_NEW")
        self.assertEqual(proof.transfer_id, TRANSFER_ID)
        self.assertEqual(proof.matched_raw_bytes, 8)
        self.assertEqual(proof.matched_checkpoints, 2)
        self.assertEqual([p.matched_checkpoints for p in progress], [1, 2])

    def test_first_hash_mismatch_aborts_before_consuming_tail(self):
        reader = io.BytesIO(b"ABCDXXXXTAIL")
        with self.assertRaisesRegex(ValueError, "checkpoint 1"):
            self.replay(reader)
        self.assertEqual(reader.read(), b"TAIL")

    def test_readonly_uuid_parent_and_options_checked_before_read(self):
        class NoRead(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("source must not be read after preflight mismatch")

        for changed in (
            replace(self.source, readonly=False),
            replace(self.source, uuid="69f7f777-6ba6-4484-badb-47e2e4d03911"),
            replace(self.source, parent_uuid="69f7f777-6ba6-4484-badb-47e2e4d03911"),
            replace(self.source, send_fingerprint="protocol=1"),
            replace(self.source, path="/snapshots/99/snapshot"),
        ):
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                self.replay(NoRead(), source=changed)

    def test_target_identity_revalidated_before_source_replay(self):
        different = replace(self.destination, fingerprint="other-volume")
        with self.assertRaises(ValueError):
            self.replay(io.BytesIO(b"ABCDEFGH"), destination=different)

    def test_source_shorter_than_committed_prefix_is_not_resumable(self):
        with self.assertRaisesRegex(ValueError, "checkpoint 1"):
            self.replay(io.BytesIO(b"ABCDE"))

    def test_version_change_is_diagnostic_only_when_raw_hashes_match(self):
        changed = replace(
            self.manifest,
            identity={
                **self.manifest.identity,
                "kernel_release": "7.3",
                "engine_version": "0.10.0",
            },
        )
        proof = self.replay(io.BytesIO(b"ABCDEFGH"), manifest=changed)
        self.assertEqual(proof.matched_raw_bytes, 8)

    def test_completed_or_discarded_manifest_cannot_replay(self):
        class NoRead(io.BytesIO):
            def read(self, size=-1):
                raise AssertionError("should fail before read")

        for state in ("completed", "discarded"):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.replay(NoRead(), manifest=replace(self.manifest, state=state))


if __name__ == "__main__":
    unittest.main(verbosity=2)
