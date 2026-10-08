"""Bounded raw Btrfs send streaming as independently decompressible zstd frames.

This is an internal, feature-gated v2 component. It does not publish backups
or auto-enable the experimental upstream --use-chunked/full-spool code path.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from typing import BinaryIO, Iterator, Protocol

DEFAULT_CHECKPOINT_SIZE = 128 * 1024 * 1024
MAX_CHECKPOINT_SIZE = 1024 * 1024 * 1024
READ_BUFFER_SIZE = 1024 * 1024


class FrameSink(Protocol):
    def append_frame(
        self, *, raw_sha256: str, raw_length: int, frame: bytes
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class TransferSummary:
    raw_bytes: int
    compressed_bytes: int
    checkpoints: int


def raw_checkpoints(
    source: BinaryIO, chunk_size: int = DEFAULT_CHECKPOINT_SIZE
) -> Iterator[bytes]:
    """Return deterministic raw byte boundaries even on short pipe reads.

    A short read from a pipe is NOT EOF. Fill the raw checkpoint across
    successive reads until chunk_size bytes or a genuine EOF is observed.
    Never read source all-at-once and never spool the full Btrfs send.
    """
    if type(chunk_size) is not int or not (1 <= chunk_size <= MAX_CHECKPOINT_SIZE):
        raise ValueError("checkpoint size must be positive and bounded")
    while True:
        block = bytearray()
        while len(block) < chunk_size:
            amount = min(READ_BUFFER_SIZE, chunk_size - len(block))
            data = source.read(amount)
            if data is None:
                raise OSError("source stream returned no bytes without EOF")
            if not isinstance(data, bytes):
                raise TypeError("source must be a binary byte stream")
            if not data:
                break
            if len(data) > amount:
                raise ValueError("source read exceeded requested bounds")
            block.extend(data)
        if not block:
            return
        yield bytes(block)
        if len(block) < chunk_size:
            return


def compress_frame(raw: bytes, *, level: int = 3, threads: int = 1) -> bytes:
    """Compress exactly one raw checkpoint as its own standard zstd frame.

    The input and returned compressed frame are bounded by the checkpoint
    size. Each invocation is independent, so concatenation is transparently
    decompressible with standard `zstd -dc`.
    """
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("cannot compress an empty/raw-invalid checkpoint")
    if type(level) is not int or not (-7 <= level <= 22):
        raise ValueError("unsupported zstd level")
    if type(threads) is not int or not (1 <= threads <= 32):
        raise ValueError("compression threads must be 1..32")
    if level < 0:
        compression_arg = [f"--fast={-level}"]
    elif level > 19:
        compression_arg = ["--ultra", f"-{level}"]
    else:
        compression_arg = [f"-{level}"]
    command = ["zstd", "-q", "-c", f"-T{threads}", *compression_arg]
    try:
        completed = subprocess.run(
            command,
            input=raw,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("zstd executable unavailable") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace")[:1024]
        raise RuntimeError(f"zstd checkpoint compression failed: {detail}") from exc
    if not completed.stdout:
        raise RuntimeError("zstd produced no independent frame")
    return completed.stdout


def feed_checkpointed_stream(
    source: BinaryIO,
    sink: FrameSink,
    *,
    chunk_size: int = DEFAULT_CHECKPOINT_SIZE,
    level: int = 3,
    threads: int = 1,
) -> TransferSummary:
    """Stream frames to a durable sink; never publish or fake success.

    The caller must independently verify btrfs send exits cleanly and the
    entire source stream before finalizing. A failed source leaves committed
    checkpoints intact and the transfer incomplete.
    """
    raw_bytes = compressed_bytes = count = 0
    for raw in raw_checkpoints(source, chunk_size=chunk_size):
        frame = compress_frame(raw, level=level, threads=threads)
        sink.append_frame(
            raw_sha256=hashlib.sha256(raw).hexdigest(),
            raw_length=len(raw),
            frame=frame,
        )
        raw_bytes += len(raw)
        compressed_bytes += len(frame)
        count += 1
    return TransferSummary(raw_bytes, compressed_bytes, count)
