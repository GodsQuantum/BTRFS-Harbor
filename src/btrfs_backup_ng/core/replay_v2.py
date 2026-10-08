"""Immutable Btrfs send replay over committed checkpoint hashes.

Replay never accepts or touches a destination stream. The caller verifies
the source snapshot UUID/readonly status and the target mount before replay;
every committed raw checkpoint must hash-match before fresh upload starts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import BinaryIO, Callable

from .checkpoint_v2 import ResumeManifest, serialize_manifest

REPLAY_STATES = frozenset(
    {
        "preparing",
        "uploading",
        "pause_requested",
        "paused",
        "replaying",
        "failed_resumable",
    }
)
REPLAY_READ_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class SourceFingerprint:
    uuid: str
    parent_uuid: str | None
    path: str
    send_fingerprint: str
    readonly: bool


@dataclass(frozen=True, slots=True)
class DestinationFingerprint:
    type: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class ReplayProgress:
    matched_checkpoints: int
    matched_raw_bytes: int


@dataclass(frozen=True, slots=True)
class ReplayProof:
    transfer_id: str
    matched_checkpoints: int
    matched_raw_bytes: int
    checkpoint_index_sha256: str


def validate_resume_identity(
    manifest: ResumeManifest,
    source: SourceFingerprint,
    destination: DestinationFingerprint,
) -> None:
    """Refuse a wrong source, send mode, incremental parent or remote target."""
    serialize_manifest(manifest)
    if manifest.state not in REPLAY_STATES:
        raise ValueError(f"state {manifest.state!r} cannot resume")
    i = manifest.identity
    if source.readonly is not True:
        raise ValueError("resumable Btrfs source must be readonly")
    if (
        source.uuid != i["source_uuid"]
        or source.parent_uuid != i["parent_uuid"]
        or source.path != i["source_path"]
        or source.send_fingerprint != i["send_fingerprint"]
    ):
        raise ValueError("source snapshot / parent / send identity mismatch")
    if (
        destination.type != i["destination_type"]
        or destination.fingerprint != i["destination_fingerprint"]
    ):
        raise ValueError("destination identity mismatch")


def replay_committed(
    source_stream: BinaryIO,
    manifest: ResumeManifest,
    *,
    source: SourceFingerprint,
    destination: DestinationFingerprint,
    progress: Callable[[ReplayProgress], None] | None = None,
) -> ReplayProof:
    """Hash every committed raw prefix locally, without a destination write.

    The open source_stream is left positioned precisely after the matched
    prefix. The caller can then start the streaming compressor at that point.
    On mismatch (or premature EOF), no proof is returned and append is denied.
    """
    validate_resume_identity(manifest, source, destination)
    matched = 0
    for entry in manifest.checkpoints:
        digest = hashlib.sha256()
        remaining = entry.raw_length
        while remaining > 0:
            requested = min(REPLAY_READ_BYTES, remaining)
            payload = source_stream.read(requested)
            if not isinstance(payload, bytes) or not payload:
                raise ValueError(
                    f"replay checkpoint {entry.sequence}: missing source bytes"
                )
            if len(payload) > requested:
                raise ValueError(
                    f"replay checkpoint {entry.sequence}: source read exceeded bounds"
                )
            digest.update(payload)
            remaining -= len(payload)
        if not hmac.compare_digest(digest.hexdigest(), entry.raw_sha256):
            raise ValueError(
                f"replay checkpoint {entry.sequence}: raw SHA-256 mismatch"
            )
        matched += entry.raw_length
        if progress is not None:
            progress(ReplayProgress(entry.sequence + 1, matched))
    indexed = json.loads(serialize_manifest(manifest))
    return ReplayProof(
        transfer_id=manifest.transfer_id,
        matched_checkpoints=len(manifest.checkpoints),
        matched_raw_bytes=matched,
        checkpoint_index_sha256=indexed["checkpoint_index_sha256"],
    )
