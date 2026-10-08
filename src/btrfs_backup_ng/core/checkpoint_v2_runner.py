"""Internal end-to-end checkpoint orchestration over a prevalidated send process.

The caller must resolve a readonly Btrfs source and native Snapper pin before
calling this function. This is NOT yet the installed CLI dispatch path. The
runner must not be exposed to production until real Btrfs/NFS tests pass.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Literal, Protocol

from .checkpoint_v2 import ResumeManifest
from .replay_v2 import (
    DestinationFingerprint,
    SourceFingerprint,
    validate_resume_identity,
)
from .resumable_worker import resume_worker, run_worker
from ..endpoint.resumable_raw import DirectoryGuard, ResumableRawSink, load_existing


class SendProcess(Protocol):
    stdout: BinaryIO

    def wait(self) -> int: ...
    def terminate(self) -> None: ...


@dataclass(frozen=True)
class JobResult:
    transfer_id: str
    status: str
    new_raw_bytes: int
    new_checkpoints: int


def _sealed_sidecar(sink: ResumableRawSink) -> bytes:
    """Make the standard RawSnapshot-compatible sidecar from committed bytes."""
    total = sum(item.compressed_length for item in sink.manifest.checkpoints)
    os.lseek(sink._part_fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while chunk := os.read(sink._part_fd, 1024 * 1024):
        digest.update(chunk)
    if os.fstat(sink._part_fd).st_size != total:
        raise ValueError("cannot publish a stream with an uncommitted tail")
    metadata = {
        "version": 2,
        "name": sink.snapshot_name,
        "uuid": "",
        "source_uuid": sink.manifest.identity["source_uuid"],
        "received_subvolume_name": Path(
            str(sink.manifest.identity["source_path"])
        ).name,
        "parent_uuid": sink.manifest.identity["parent_uuid"],
        "parent_name": None,
        "created": datetime.now(timezone.utc).isoformat(),
        "size": total,
        "pipeline": {
            "compress": "zstd",
            "encrypt": None,
            "gpg_recipient": None,
            "openssl_cipher": None,
        },
        "checksum": {"algorithm": "sha256", "value": digest.hexdigest()},
        "provenance": {
            "origin": "native-write",
            "stream_completeness": "complete",
        },
    }
    return json.dumps(metadata, sort_keys=True).encode("utf-8")


def execute_checkpoint_job(
    *,
    target_root: Path,
    snapshot_name: str,
    manifest: ResumeManifest,
    source: SourceFingerprint,
    destination: DestinationFingerprint,
    guard: DirectoryGuard,
    spawn: Callable[[], SendProcess],
    action: Callable[[], Literal["run", "pause", "stop"]],
    resume: bool = False,
    chunk_size: int = 128 * 1024 * 1024,
    level: int = 3,
    threads: int = 1,
    on_completed: Callable[[], None] | None = None,
) -> JobResult:
    """Bounded send → framed writer, with secure Resume replay before append."""
    validate_resume_identity(manifest, source, destination)
    if chunk_size != manifest.checkpoint_size:
        raise ValueError("job checkpoint size may not change on resume")
    if resume:
        sink = load_existing(
            target_root,
            snapshot_name,
            manifest.transfer_id,
            guard,
            expected_manifest=manifest,
        )
    else:
        sink = ResumableRawSink.open_new(
            target_root,
            snapshot_name,
            manifest,
            guard,
        )
    with sink:
        proc = spawn()
        try:

            def shutdown() -> None:
                proc.terminate()
                proc.wait()

            if resume:
                result = resume_worker(
                    proc.stdout,
                    sink,
                    source,
                    destination,
                    action=action,
                    chunk_size=chunk_size,
                    level=level,
                    threads=threads,
                    shutdown_source=shutdown,
                    source_exit_status=proc.wait,
                )
            else:
                result = run_worker(
                    proc.stdout,
                    sink,
                    action=action,
                    chunk_size=chunk_size,
                    level=level,
                    threads=threads,
                    shutdown_source=shutdown,
                    source_exit_status=proc.wait,
                )
            if result.status != "ready_to_finalize":
                return JobResult(
                    manifest.transfer_id,
                    result.status,
                    result.new_raw_bytes,
                    result.new_checkpoints,
                )
            sink.publish(_sealed_sidecar(sink))
            if on_completed:
                on_completed()
            return JobResult(
                manifest.transfer_id,
                "completed",
                result.new_raw_bytes,
                result.new_checkpoints,
            )
        finally:
            proc.stdout.close()
            stderr_log = getattr(proc, "_harbor_stderr_log", None)
            if stderr_log is not None:
                stderr_log.close()
