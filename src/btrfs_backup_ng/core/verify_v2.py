"""Verify completed Harbor Checkpointed Raw v2 streams, fail closed.

This does not claim Btrfs semantic receive correctness. A full structural
verify checks the raw sidecar, v2 checkpoint index, stored compressed frame
digests and zstd concatenation; real btrfs receive validation is separate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .checkpoint_v2 import MAX_MANIFEST_BYTES, read_manifest, serialize_manifest


@dataclass(frozen=True)
class VerificationResult:
    status: str
    detail: str
    checkpoints: int = 0


def _regular_file_fd(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("backup is not a regular file")
    return fd


def verify_checkpoint_index(
    meta_path: Path, stream_path: Path, *, full: bool = False
) -> VerificationResult:
    if not stream_path.is_file() or not meta_path.is_file():
        return VerificationResult(
            "incomplete", "missing stream or authoritative sidecar"
        )
    try:
        fd = _regular_file_fd(meta_path)
        try:
            with os.fdopen(fd, "rb") as f:
                meta = json.loads(f.read(MAX_MANIFEST_BYTES + 1))
        finally:
            pass
        if not isinstance(meta, dict):
            raise ValueError("invalid sidecar")
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
        return VerificationResult("corrupt", f"invalid sidecar: {err}")
    record = meta.get("harbor_checkpoint_v2")
    if record is None:
        return VerificationResult(
            "legacy", "pre-v2 complete raw stream; use legacy raw verify"
        )
    if not isinstance(record, dict):
        return VerificationResult("corrupt", "invalid v2 sidecar extension")
    transfer_id = record.get("transfer_id")
    if not isinstance(transfer_id, str):
        return VerificationResult("corrupt", "missing v2 transfer id")
    manifest_path = stream_path.parent / f".harbor-resume-{transfer_id}.json"
    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError, TypeError) as err:
        return VerificationResult(
            "unverifiable", f"v2 manifest unavailable or corrupt: {err}"
        )
    if manifest.transfer_id != transfer_id:
        return VerificationResult("corrupt", "manifest transfer identity differs")
    sealed = json.loads(serialize_manifest(manifest))
    for key in (
        "checkpoint_index_sha256",
        "committed_raw_bytes",
        "committed_compressed_bytes",
        "checkpoint_size",
    ):
        if record.get(key) != sealed.get(key):
            return VerificationResult("corrupt", f"sidecar v2 {key} mismatch")
    if record.get("checkpoint_count") != len(manifest.checkpoints):
        return VerificationResult("corrupt", "sidecar checkpoint count mismatch")
    if meta.get("source_uuid") != manifest.identity["source_uuid"]:
        return VerificationResult("corrupt", "source UUID mismatch")
    expected_size = sealed["committed_compressed_bytes"]
    if (
        type(meta.get("size")) is not int
        or meta["size"] != expected_size
        or stream_path.stat().st_size != expected_size
    ):
        return VerificationResult("corrupt", "published stream length mismatch")
    count = len(manifest.checkpoints)
    if not full:
        return VerificationResult(
            "unverified",
            "metadata/index structurally valid; full checksum not run",
            count,
        )
    fd = _regular_file_fd(stream_path)
    try:
        whole = hashlib.sha256()
        for cp in manifest.checkpoints:
            os.lseek(fd, cp.compressed_offset, os.SEEK_SET)
            hash_frame = hashlib.sha256()
            remaining = cp.compressed_length
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    return VerificationResult(
                        "corrupt", "truncated compressed frame", count
                    )
                hash_frame.update(chunk)
                whole.update(chunk)
                remaining -= len(chunk)
            if not hmac.compare_digest(hash_frame.hexdigest(), cp.compressed_sha256):
                return VerificationResult(
                    "corrupt", f"frame {cp.sequence} SHA-256 mismatch", count
                )
        checksum = meta.get("checksum") or {}
        if (
            isinstance(checksum, dict)
            and checksum.get("value")
            and not hmac.compare_digest(whole.hexdigest(), checksum["value"])
        ):
            return VerificationResult(
                "corrupt", "whole stream checksum mismatch", count
            )
    finally:
        os.close(fd)
    try:
        outcome = subprocess.run(
            ["zstd", "--test", "-q", "--", str(stream_path)],
            capture_output=True,
            timeout=3600,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return VerificationResult("error", f"zstd verifier unavailable: {err}", count)
    if outcome.returncode != 0:
        return VerificationResult(
            "corrupt", "concatenated zstd stream failed integrity test", count
        )
    return VerificationResult(
        "ok",
        "compressed frames and zstd format verified; Btrfs receive not checked",
        count,
    )
