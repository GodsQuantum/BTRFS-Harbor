"""Checkpoint-boundary pause/stop lifecycle, no permanent daemon.

Runs only on an independently validated readonly send source and pinned
destination. The caller owns its source process group, cancellation signalling
and Snapper pin; the worker never silently discards a resumable transaction.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import BinaryIO, Callable, Literal

from .checkpoint_stream import (
    DEFAULT_CHECKPOINT_SIZE,
    compress_frame,
    raw_checkpoints,
)
from .replay_v2 import (
    DestinationFingerprint,
    SourceFingerprint,
    replay_committed,
)
from ..endpoint.resumable_raw import ResumableRawSink

Action = Literal["run", "pause", "stop"]


@dataclass(frozen=True)
class WorkerResult:
    status: str
    new_raw_bytes: int
    new_checkpoints: int


def run_worker(
    source_stream: BinaryIO,
    sink: ResumableRawSink,
    *,
    action: Callable[[], Action],
    chunk_size: int = DEFAULT_CHECKPOINT_SIZE,
    level: int = 3,
    threads: int = 1,
    shutdown_source: Callable[[], None] | None = None,
    source_exit_status: Callable[[], int] | None = None,
) -> WorkerResult:
    """Yield only at committed frame boundaries for persistent pause.

    A request observed before the next frame avoids source reads altogether.
    'stop' retains committed checkpoints and stops immediately at the next
    control boundary. The process owner must also signal its process group to
    interrupt a blocked read/compress on an emergency stop.
    """
    raw_bytes = frames = 0
    if not sink.append_allowed:
        raise RuntimeError("resume must verify the original source first")
    if action() not in ("run", "pause", "stop"):
        raise ValueError("unknown worker action")
    try:
        # Check requests before *every* raw read, including the first.
        checkpoints = iter(raw_checkpoints(source_stream, chunk_size=chunk_size))
        while True:
            request = action()
            if request == "pause":
                sink.transition("paused")
                if shutdown_source:
                    shutdown_source()
                return WorkerResult("paused", raw_bytes, frames)
            if request == "stop":
                sink.transition("failed_resumable")
                if shutdown_source:
                    shutdown_source()
                return WorkerResult("failed_resumable", raw_bytes, frames)
            if request != "run":
                raise ValueError("unknown worker action")
            try:
                raw = next(checkpoints)
            except StopIteration:
                break
            frame = compress_frame(raw, level=level, threads=threads)
            # A stop before commit discards only the current (uncommitted)
            # local frame, not any previously acknowledged checkpoint.
            if action() == "stop":
                sink.transition("failed_resumable")
                if shutdown_source:
                    shutdown_source()
                return WorkerResult("failed_resumable", raw_bytes, frames)
            sink.append_frame(
                raw_sha256=hashlib.sha256(raw).hexdigest(),
                raw_length=len(raw),
                frame=frame,
            )
            raw_bytes += len(raw)
            frames += 1
        if source_exit_status is not None and source_exit_status() != 0:
            sink.transition("failed_resumable")
            raise RuntimeError("source send process did not exit successfully")
        sink.transition("finalizing")
        return WorkerResult("ready_to_finalize", raw_bytes, frames)
    except BaseException:
        if not getattr(sink, "_broken", False):
            try:
                if sink.manifest.state not in (
                    "paused",
                    "failed_resumable",
                    "finalizing",
                ):
                    sink.transition("failed_resumable")
            except Exception:
                pass
        raise


def resume_worker(
    source_stream: BinaryIO,
    sink: ResumableRawSink,
    source: SourceFingerprint,
    destination: DestinationFingerprint,
    *,
    action: Callable[[], Action],
    chunk_size: int = DEFAULT_CHECKPOINT_SIZE,
    level: int = 3,
    threads: int = 1,
    shutdown_source: Callable[[], None] | None = None,
    source_exit_status: Callable[[], int] | None = None,
) -> WorkerResult:
    """Verify the committed raw prefix before touching destination again."""
    if chunk_size != sink.manifest.checkpoint_size:
        raise ValueError("resume checkpoint size differs from original job")
    proof = replay_committed(
        source_stream, sink.manifest, source=source, destination=destination
    )
    sink.authorize_replayed_prefix(proof)
    return run_worker(
        source_stream,
        sink,
        action=action,
        chunk_size=chunk_size,
        level=level,
        threads=threads,
        shutdown_source=shutdown_source,
        source_exit_status=source_exit_status,
    )
