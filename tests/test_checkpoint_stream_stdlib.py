"""Small synthetic tests for bounded raw streaming and independent zstd frames."""

import hashlib
import importlib
import io
import os
import subprocess
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def api():
    if os.environ.get("HARBOR_LIGHT_TEST") == "1":
        for name, relative in (
            ("btrfs_backup_ng", "src/btrfs_backup_ng"),
            ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
        ):
            module = types.ModuleType(name)
            module.__path__ = [str(ROOT / relative)]
            sys.modules[name] = module
    return importlib.import_module("btrfs_backup_ng.core.checkpoint_stream")


def decoded(data):
    return subprocess.run(
        ["zstd", "-q", "-dc"],
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    ).stdout


class FragmentedReader(io.BytesIO):
    def __init__(self, content, max_read):
        super().__init__(content)
        self.max_read = max_read
        self.largest_requested = 0

    def read(self, count=-1):
        if count <= 0:
            raise AssertionError("streaming reader must never read the whole source")
        self.largest_requested = max(self.largest_requested, count)
        return super().read(min(count, self.max_read))


class CollectedSink:
    def __init__(self):
        self.records = []

    def append_frame(self, *, raw_sha256, raw_length, frame):
        self.records.append((raw_sha256, raw_length, frame))
        return len(self.records) - 1


class TestCheckpointStreaming(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.stream = api()

    def test_short_os_pipe_reads_fill_fixed_checkpoint_boundaries(self):
        reader = FragmentedReader(b"abcdefghijklmnopqrstuvwxyz", max_read=3)
        checkpoints = list(self.stream.raw_checkpoints(reader, chunk_size=7))
        self.assertEqual(checkpoints, [b"abcdefg", b"hijklmn", b"opqrstu", b"vwxyz"])
        self.assertLessEqual(reader.largest_requested, 7)

    def test_empty_stream_yields_no_checkpoint(self):
        self.assertEqual(list(self.stream.raw_checkpoints(io.BytesIO(b""), 8)), [])

    def test_independent_frames_decompress_to_exact_original(self):
        blocks = [b"abcdefg", b"1234", b"\x00\x01" * 2048]
        frames = [
            self.stream.compress_frame(block, level=1, threads=1) for block in blocks
        ]
        self.assertTrue(
            all(decoded(frame) == block for frame, block in zip(frames, blocks))
        )
        self.assertEqual(decoded(b"".join(frames)), b"".join(blocks))

    def test_feed_stream_preserves_raw_hashes_and_final_short_checkpoint(self):
        source = bytes(range(256)) * 55 + b"short"
        sink = CollectedSink()
        reader = FragmentedReader(source, max_read=11)
        result = self.stream.feed_checkpointed_stream(
            reader, sink, chunk_size=127, level=3, threads=1
        )
        self.assertEqual(
            b"".join(decoded(frame) for _, _, frame in sink.records), source
        )
        self.assertEqual(sum(raw_len for _, raw_len, _ in sink.records), len(source))
        self.assertEqual(result.raw_bytes, len(source))
        self.assertEqual(result.checkpoints, len(sink.records))
        self.assertEqual(
            result.compressed_bytes, sum(len(frame) for _, _, frame in sink.records)
        )
        self.assertLessEqual(reader.largest_requested, 127)
        for i, (digest, raw_len, frame) in enumerate(sink.records):
            raw = decoded(frame)
            self.assertEqual(len(raw), raw_len)
            self.assertEqual(digest, hashlib.sha256(raw).hexdigest())
            if i != len(sink.records) - 1:
                self.assertEqual(raw_len, 127)
        self.assertLess(sink.records[-1][1], 127)

    def test_invalid_chunk_sizes_and_levels_fail_before_source_read(self):
        reader = FragmentedReader(b"abc", max_read=3)
        with self.assertRaises(ValueError):
            list(self.stream.raw_checkpoints(reader, chunk_size=0))
        with self.assertRaises(ValueError):
            self.stream.compress_frame(b"abc", level=99, threads=1)
        with self.assertRaises(ValueError):
            self.stream.compress_frame(b"abc", level=3, threads=0)
        self.assertEqual(reader.tell(), 0)

    def test_compression_does_not_hide_transfer_errors(self):
        class ExplodingReader(io.BytesIO):
            def read(self, size=-1):
                raise OSError("injected source failure")

        sink = CollectedSink()
        with self.assertRaisesRegex(OSError, "source failure"):
            self.stream.feed_checkpointed_stream(
                ExplodingReader(b"abc"), sink, chunk_size=32, level=3, threads=1
            )
        self.assertEqual(sink.records, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
