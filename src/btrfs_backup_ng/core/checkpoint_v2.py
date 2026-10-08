"""Strict, deterministic metadata for Harbor Checkpointed Raw v2.

This module does not perform filesystem writes. Resume metadata is untrusted:
callers must independently verify the source UUID, send parameters, destination
mount identity and individual checkpoint bytes before appending anything.
"""

from __future__ import annotations

import hashlib
import os
import stat
import hmac
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

SCHEMA_VERSION = 2
DEFAULT_CHECKPOINT_SIZE = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_CHECKPOINTS = 100_000
SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
VALID_STATES = frozenset(
    {
        "preparing",
        "uploading",
        "pause_requested",
        "paused",
        "replaying",
        "finalizing",
        "verifying",
        "completed",
        "failed_resumable",
        "failed_unrecoverable",
        "discarded",
    }
)
REQUIRED_IDENTITY = frozenset(
    {
        "profile_id",
        "source_volume",
        "source_uuid",
        "source_path",
        "parent_uuid",
        "send_fingerprint",
        "compression",
        "compression_level",
        "encryption",
        "destination_type",
        "destination_fingerprint",
        "kernel_release",
        "btrfs_progs_version",
        "harbor_version",
        "engine_version",
    }
)
OPTIONAL_IDENTITY = frozenset(
    {"snapper_config", "snapper_number", "snapper_type", "snapper_pre_number"}
)
CHECKPOINT_FIELDS = frozenset(
    {
        "sequence",
        "raw_offset",
        "raw_length",
        "compressed_offset",
        "compressed_length",
        "raw_sha256",
        "compressed_sha256",
        "committed_at",
    }
)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "transfer_id",
        "identity",
        "checkpoint_size",
        "state",
        "checkpoints",
        "checkpoint_index_sha256",
        "committed_raw_bytes",
        "committed_compressed_bytes",
    }
)


def _canonical(obj: Any) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _plain_int(value: Any) -> bool:
    return type(value) is int


def _valid_uuid(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value.lower()
    except ValueError:
        return False


def _valid_path(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 4096
        and value.startswith("/")
        and "\x00" not in value
        and ".." not in value.split("/")
        and str(PurePosixPath(value)) == value
    )


def _validate_identity(identity: dict[str, str | int | None]) -> None:
    if not isinstance(identity, dict):
        raise ValueError("manifest identity must be an object")
    if not REQUIRED_IDENTITY.issubset(identity):
        raise ValueError("manifest source/destination identity is incomplete")
    if identity.keys() - (REQUIRED_IDENTITY | OPTIONAL_IDENTITY):
        raise ValueError("manifest contains unsupported identity fields")
    if not _valid_uuid(identity["source_uuid"]):
        raise ValueError("source snapshot UUID is invalid")
    parent = identity["parent_uuid"]
    if parent is not None and not _valid_uuid(parent):
        raise ValueError("incremental parent UUID is invalid")
    for name in ("source_volume", "source_path"):
        if not _valid_path(identity[name]):
            raise ValueError(f"invalid source path in identity: {name}")
    for name in REQUIRED_IDENTITY - {
        "source_volume",
        "source_path",
        "source_uuid",
        "parent_uuid",
        "compression_level",
    }:
        value = identity[name]
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 4096
            or "\x00" in value
        ):
            raise ValueError(f"invalid source/destination identity: {name}")
    if identity["compression"] != "zstd" or identity["encryption"] != "none":
        raise ValueError("v2 resume requires unencrypted zstd")
    if identity["destination_type"] != "raw":
        raise ValueError("v2 resume requires a supported raw target")
    compression_level = identity["compression_level"]
    if (
        not isinstance(compression_level, int)
        or isinstance(compression_level, bool)
        or not (-7 <= compression_level <= 22)
    ):
        raise ValueError("invalid zstd compression level")
    for name in ("snapper_config", "snapper_type"):
        value = identity.get(name)
        if value is not None and (
            not isinstance(value, str)
            or not value
            or len(value) > 255
            or "/" in value
            or "\x00" in value
        ):
            raise ValueError(f"invalid snapper identity: {name}")
    for name in ("snapper_number", "snapper_pre_number"):
        value = identity.get(name)
        if value is not None and (
            not isinstance(value, int) or isinstance(value, bool) or value < 0
        ):
            raise ValueError(f"invalid snapper identity: {name}")


@dataclass(frozen=True, slots=True)
class Checkpoint:
    sequence: int
    raw_offset: int
    raw_length: int
    compressed_offset: int
    compressed_length: int
    raw_sha256: str
    compressed_sha256: str
    committed_at: str


@dataclass(frozen=True, slots=True)
class ResumeManifest:
    schema_version: int
    transfer_id: str
    identity: dict[str, str | int | None]
    checkpoint_size: int
    state: str
    checkpoints: tuple[Checkpoint, ...]


def _validate(manifest: ResumeManifest) -> tuple[int, int]:
    if (
        not _plain_int(manifest.schema_version)
        or manifest.schema_version != SCHEMA_VERSION
    ):
        raise ValueError("unsupported checkpoint manifest schema")
    if not _valid_uuid(manifest.transfer_id):
        raise ValueError("invalid transfer UUID")
    if not _plain_int(manifest.checkpoint_size) or not (
        1 <= manifest.checkpoint_size <= 1024 * 1024 * 1024
    ):
        raise ValueError("invalid checkpoint size")
    if manifest.state not in VALID_STATES:
        raise ValueError("unsupported resume state")
    _validate_identity(manifest.identity)
    if not isinstance(manifest.checkpoints, (list, tuple)):
        raise ValueError("checkpoints must be ordered")
    if len(manifest.checkpoints) > MAX_CHECKPOINTS:
        raise ValueError("checkpoint index exceeds safety limit")

    raw_end = compressed_end = 0
    for sequence, checkpoint in enumerate(manifest.checkpoints):
        if not isinstance(checkpoint, Checkpoint):
            raise ValueError("invalid checkpoint entry")
        if any(
            not _plain_int(value)
            for value in (
                checkpoint.sequence,
                checkpoint.raw_offset,
                checkpoint.raw_length,
                checkpoint.compressed_offset,
                checkpoint.compressed_length,
            )
        ):
            raise ValueError("checkpoint offsets and lengths must be integers")
        if checkpoint.sequence != sequence:
            raise ValueError("checkpoint sequence gap")
        if (
            checkpoint.raw_offset != raw_end
            or checkpoint.compressed_offset != compressed_end
        ):
            raise ValueError("checkpoint offset discontinuity")
        if not (0 < checkpoint.raw_length <= manifest.checkpoint_size):
            raise ValueError("checkpoint raw length exceeds bounds")
        if sequence < len(manifest.checkpoints) - 1 and (
            checkpoint.raw_length != manifest.checkpoint_size
        ):
            raise ValueError("non-final raw checkpoint is not full-sized")
        if not (
            0 < checkpoint.compressed_length <= 2 * manifest.checkpoint_size + 65536
        ):
            raise ValueError("checkpoint compressed length exceeds bounds")
        for digest in (checkpoint.raw_sha256, checkpoint.compressed_sha256):
            if not isinstance(digest, str) or not SHA256_HEX.fullmatch(digest):
                raise ValueError("invalid checkpoint sha256")
        try:
            dt = datetime.fromisoformat(checkpoint.committed_at)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid checkpoint commit timestamp") from exc
        if dt.tzinfo is None:
            raise ValueError("checkpoint timestamp must have an offset")
        raw_end += checkpoint.raw_length
        compressed_end += checkpoint.compressed_length
    return raw_end, compressed_end


def serialize_manifest(manifest: ResumeManifest) -> bytes:
    """Serialize only validated checkpoints to a canonical manifest."""
    raw_end, compressed_end = _validate(manifest)
    records = [asdict(checkpoint) for checkpoint in manifest.checkpoints]
    payload = {
        "schema_version": manifest.schema_version,
        "transfer_id": manifest.transfer_id,
        "identity": manifest.identity,
        "checkpoint_size": manifest.checkpoint_size,
        "state": manifest.state,
        "checkpoints": records,
        "checkpoint_index_sha256": hashlib.sha256(_canonical(records)).hexdigest(),
        "committed_raw_bytes": raw_end,
        "committed_compressed_bytes": compressed_end,
    }
    encoded = _canonical(payload)
    if len(encoded) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest exceeds maximum size")
    return encoded


def _unique_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate manifest JSON key")
        result[key] = value
    return result


def parse_manifest(data: bytes) -> ResumeManifest:
    """Fail closed on missing, malformed, conflicting or mutated resume metadata."""
    if not isinstance(data, bytes) or len(data) > MAX_MANIFEST_BYTES:
        raise ValueError("manifest byte size exceeds safety limit")
    try:
        payload = json.loads(data, object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid manifest JSON") from exc
    if not isinstance(payload, dict) or payload.keys() != MANIFEST_FIELDS:
        raise ValueError("invalid manifest fields")
    records = payload["checkpoints"]
    if not isinstance(records, list) or len(records) > MAX_CHECKPOINTS:
        raise ValueError("invalid checkpoint index")
    if any(
        not isinstance(entry, dict) or entry.keys() != CHECKPOINT_FIELDS
        for entry in records
    ):
        raise ValueError("invalid checkpoint record")
    digest = hashlib.sha256(_canonical(records)).hexdigest()
    if not isinstance(
        payload["checkpoint_index_sha256"], str
    ) or not hmac.compare_digest(digest, payload["checkpoint_index_sha256"]):
        raise ValueError("checkpoint index digest mismatch")
    manifest = ResumeManifest(
        schema_version=payload["schema_version"],
        transfer_id=payload["transfer_id"],
        identity=payload["identity"],
        checkpoint_size=payload["checkpoint_size"],
        state=payload["state"],
        checkpoints=tuple(Checkpoint(**entry) for entry in records),
    )
    raw_end, compressed_end = _validate(manifest)
    if (
        not _plain_int(payload["committed_raw_bytes"])
        or not _plain_int(payload["committed_compressed_bytes"])
        or payload["committed_raw_bytes"] != raw_end
        or payload["committed_compressed_bytes"] != compressed_end
    ):
        raise ValueError("manifest committed byte totals mismatch")
    return manifest


def _open_regular_nofollow(path: Path) -> int:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("resume data must be a regular file")
    return fd


def read_manifest(manifest_path: Path) -> ResumeManifest:
    """Load a bounded, non-symlink manifest; missing/invalid is never Resume."""
    fd = _open_regular_nofollow(manifest_path)
    try:
        with os.fdopen(fd, "rb") as stream:
            data = stream.read(MAX_MANIFEST_BYTES + 1)
    except BaseException:
        # The context manager owns fd after os.fdopen succeeded.
        raise
    return parse_manifest(data)


def commit_manifest(
    manifest_path: Path,
    manifest: ResumeManifest,
    *,
    validate_destination: Callable[[], None],
) -> None:
    """Persist v2 metadata atomically, requiring durable directory rename.

    The calling sink must have fsynced the complete compressed frame first.
    A returned success means the rename and containing-directory fsync both
    completed; an error must never be advertised as a committed checkpoint.
    """
    encoded = serialize_manifest(manifest)
    validate_destination()
    temporary = manifest_path.with_name(f".{manifest_path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
    )
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        validate_destination()
        os.replace(temporary, manifest_path)
        directory_fd = os.open(
            manifest_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def reconcile_part(
    part_path: Path,
    manifest: ResumeManifest,
    *,
    validate_destination: Callable[[], None],
) -> int:
    """Drop only uncommitted tail; never invent a missing checkpoint.

    No create/truncate-to-zero fallback is permitted when the part is absent,
    shorter than the recorded committed prefix, or not a regular file.
    """
    expected = _validate(manifest)[1]
    validate_destination()
    fd = os.open(part_path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("resume part must be a regular file")
        actual = os.fstat(fd).st_size
        if actual < expected:
            raise ValueError("partial stream shorter than committed manifest")
        if actual > expected:
            validate_destination()
            os.ftruncate(fd, expected)
            os.fsync(fd)
        validate_destination()
        return expected
    finally:
        os.close(fd)
