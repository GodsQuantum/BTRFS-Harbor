"""Checkpointed Raw v2 *internal* sink: single durable partial + manifest.

Not yet wired to transfer dispatch. In particular, no resumable send should be
offered until immutable-source replay and the production mount guard exist.
All mutating operations use a pinned directory FD, never a reopened path.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import hashlib
import hmac
import json
import os
import stat
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..core.replay_v2 import ReplayProof
from ..core.checkpoint_v2 import (
    MAX_MANIFEST_BYTES,
    Checkpoint,
    ResumeManifest,
    parse_manifest,
    serialize_manifest,
)

_RENAME_NOREPLACE = 1
_LIBC = ctypes.CDLL(None, use_errno=True)
_RENAMEAT2 = getattr(_LIBC, "renameat2", None)
if _RENAMEAT2 is not None:
    _RENAMEAT2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _RENAMEAT2.restype = ctypes.c_int


class DirectoryGuard(Protocol):
    """Host-validated mount identity check, implemented by mount_guard_v2."""

    def validate_fd(self, directory_fd: int) -> None: ...


def _filename(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in (".", "..")
        or "/" in value
        or "\x00" in value
        or len(os.fsencode(value)) > 200
    ):
        raise ValueError("invalid raw snapshot filename")
    return value


def _rename_noreplace(directory_fd: int, old: str, new: str) -> None:
    """Publish an existing regular file without replacing another directory entry.

    NFSv4 on real NAS exports may return EINVAL for renameat2(RENAME_NOREPLACE).
    On that filesystem, an in-directory linkat is also atomic and no-clobber,
    but works for a regular file. After linking, unlink the old entry. A crash
    between the two calls can leave both filenames, *never* an overwritten
    final archive. Callers fsync the directory after publication.
    """
    if _RENAMEAT2 is not None:
        rc = _RENAMEAT2(
            directory_fd,
            os.fsencode(old),
            directory_fd,
            os.fsencode(new),
            _RENAME_NOREPLACE,
        )
        if rc == 0:
            return
        err = ctypes.get_errno()
        if err == errno.EEXIST:
            raise FileExistsError(err, os.strerror(err), new)
        if err not in (errno.EINVAL, errno.ENOSYS, errno.EOPNOTSUPP):
            raise OSError(err, os.strerror(err), new)
    # Only files can be published with this fallback. It never follows a
    # symlink at either end, never crosses directories or clobbers a target.
    src = os.stat(old, dir_fd=directory_fd, follow_symlinks=False)
    if not stat.S_ISREG(src.st_mode):
        raise ValueError("atomic NFS fallback only accepts regular source files")
    os.link(
        old,
        new,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )
    os.unlink(old, dir_fd=directory_fd)


def _dir_open(root: Path) -> int:
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _is_file(fd: int) -> bool:
    return stat.S_ISREG(os.fstat(fd).st_mode)


def _exists(directory_fd: int, filename: str) -> bool:
    try:
        os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def _write_all(fd: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        n = os.write(fd, view)
        if n <= 0:
            raise OSError(errno.EIO, "short checkpoint write")
        view = view[n:]


def _lock_part(fd: int) -> None:
    """Prevent concurrent writers; fail closed if filesystem locks unsupported."""
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _read_manifest(directory_fd: int, name: str) -> ResumeManifest:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    try:
        if not _is_file(fd):
            raise ValueError("resume manifest is not a regular file")
        with os.fdopen(fd, "rb") as source:
            fd = -1
            data = source.read(MAX_MANIFEST_BYTES + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    return parse_manifest(data)


def _manifest_commit(
    directory_fd: int,
    manifest_name: str,
    manifest: ResumeManifest,
    guard: DirectoryGuard,
    *,
    initial: bool = False,
) -> None:
    payload = serialize_manifest(manifest)
    guard.validate_fd(directory_fd)
    temp_name = f".harbor-resume-{uuid.uuid4().hex}.tmp"
    fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        guard.validate_fd(directory_fd)
        if initial:
            _rename_noreplace(directory_fd, temp_name, manifest_name)
        else:
            os.replace(
                temp_name,
                manifest_name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _validate_meta(meta: bytes, snapshot_name: str, manifest: ResumeManifest) -> bytes:
    if not isinstance(meta, bytes) or len(meta) > MAX_MANIFEST_BYTES:
        raise ValueError("invalid final metadata size")
    try:
        data = json.loads(meta)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("final metadata is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("final metadata is not an object")
    if (
        data.get("name") != snapshot_name
        or data.get("source_uuid") != manifest.identity["source_uuid"]
        or data.get("size") != sum(c.compressed_length for c in manifest.checkpoints)
        or not isinstance(data.get("size"), int)
    ):
        raise ValueError("final metadata does not match the committed stream")
    pipeline = data.get("pipeline")
    if not isinstance(pipeline, dict) or (
        pipeline.get("compress") != "zstd" or pipeline.get("encrypt") is not None
    ):
        raise ValueError("final metadata must identify unencrypted zstd")
    provenance = data.get("provenance")
    if not isinstance(provenance, dict) or (
        provenance.get("stream_completeness") != "complete"
    ):
        raise ValueError("final metadata must confirm complete stream")
    validated = json.loads(serialize_manifest(manifest))
    data["harbor_checkpoint_v2"] = {
        "transfer_id": manifest.transfer_id,
        "checkpoint_size": manifest.checkpoint_size,
        "checkpoint_count": len(manifest.checkpoints),
        "checkpoint_index_sha256": validated["checkpoint_index_sha256"],
        "committed_raw_bytes": validated["committed_raw_bytes"],
        "committed_compressed_bytes": validated["committed_compressed_bytes"],
    }
    return json.dumps(data, sort_keys=True, ensure_ascii=False).encode("utf-8")


class ResumableRawSink:
    """Feature-gated raw checkpoint sink; it never generates a Btrfs send."""

    def __init__(
        self,
        root: Path,
        dir_fd: int,
        part_fd: int,
        snapshot_name: str,
        manifest: ResumeManifest,
        guard: DirectoryGuard,
        *,
        append_allowed: bool,
    ) -> None:
        self.root = root
        self._dir_fd = dir_fd
        self._part_fd = part_fd
        self.snapshot_name = snapshot_name
        self.manifest = manifest
        self.guard = guard
        self.append_allowed = append_allowed
        self._broken = False
        self.part_name = f"{snapshot_name}.btrfs.zst.{manifest.transfer_id}.part"
        self.manifest_name = f".harbor-resume-{manifest.transfer_id}.json"
        self.final_name = f"{snapshot_name}.btrfs.zst"
        self._closed = False

    @classmethod
    def open_new(
        cls,
        root: Path,
        snapshot_name: str,
        manifest: ResumeManifest,
        guard: DirectoryGuard,
    ) -> ResumableRawSink:
        _filename(snapshot_name)
        serialize_manifest(manifest)
        directory_fd = _dir_open(root)
        part_fd = -1
        part_name = f"{snapshot_name}.btrfs.zst.{manifest.transfer_id}.part"
        manifest_name = f".harbor-resume-{manifest.transfer_id}.json"
        try:
            guard.validate_fd(directory_fd)
            if _exists(directory_fd, f"{snapshot_name}.btrfs.zst"):
                raise FileExistsError("a completed snapshot already exists")
            if _exists(directory_fd, manifest_name):
                raise FileExistsError("a resume manifest already exists")
            part_fd = os.open(
                part_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory_fd,
            )
            _lock_part(part_fd)
            try:
                _manifest_commit(
                    directory_fd, manifest_name, manifest, guard, initial=True
                )
            except Exception:
                guard.validate_fd(directory_fd)
                os.unlink(part_name, dir_fd=directory_fd)
                raise
            return cls(
                root,
                directory_fd,
                part_fd,
                snapshot_name,
                manifest,
                guard,
                append_allowed=True,
            )
        except BaseException:
            if part_fd >= 0:
                os.close(part_fd)
            os.close(directory_fd)
            raise

    def _check(self) -> None:
        if self._closed:
            raise ValueError("closed resume sink")
        if self._broken:
            raise RuntimeError("checkpoint append failed; reopen and reconcile")
        self.guard.validate_fd(self._dir_fd)

    def append_frame(
        self, *, raw_sha256: str, raw_length: int, frame: bytes
    ) -> Checkpoint:
        """Fsync frame and journal before reporting a committed checkpoint."""
        self._check()
        if not self.append_allowed:
            raise RuntimeError("resumed transfer requires verified source replay")
        if (
            type(raw_length) is not int
            or not 0 < raw_length <= self.manifest.checkpoint_size
            or not isinstance(frame, bytes)
            or not 0 < len(frame) <= self.manifest.checkpoint_size * 2 + 65536
            or not isinstance(raw_sha256, str)
            or len(raw_sha256) != 64
            or any(char not in "0123456789abcdef" for char in raw_sha256)
        ):
            raise ValueError("invalid checkpoint frame or SHA-256")
        if self.manifest.checkpoints and (
            self.manifest.checkpoints[-1].raw_length < self.manifest.checkpoint_size
        ):
            raise ValueError("cannot append after a short terminal checkpoint")
        committed_compressed = sum(
            c.compressed_length for c in self.manifest.checkpoints
        )
        committed_raw = sum(c.raw_length for c in self.manifest.checkpoints)
        if os.fstat(self._part_fd).st_size != committed_compressed:
            raise RuntimeError("partial length differs from committed manifest")
        checkpoint = Checkpoint(
            sequence=len(self.manifest.checkpoints),
            raw_offset=committed_raw,
            raw_length=raw_length,
            compressed_offset=committed_compressed,
            compressed_length=len(frame),
            raw_sha256=raw_sha256,
            compressed_sha256=hashlib.sha256(frame).hexdigest(),
            committed_at=datetime.now(timezone.utc).isoformat(),
        )
        updated = replace(
            self.manifest,
            state="uploading",
            checkpoints=(*self.manifest.checkpoints, checkpoint),
        )
        try:
            self.guard.validate_fd(self._dir_fd)
            os.lseek(self._part_fd, 0, os.SEEK_END)
            _write_all(self._part_fd, frame)
            os.fsync(self._part_fd)
            _manifest_commit(self._dir_fd, self.manifest_name, updated, self.guard)
        except BaseException:
            self._broken = True
            raise
        self.manifest = updated
        return checkpoint

    def transition(self, state: str) -> None:
        """Persist an explicit worker state without inventing a checkpoint."""
        from ..core.checkpoint_v2 import VALID_STATES

        self._check()
        if state not in VALID_STATES or state in ("completed", "discarded"):
            raise ValueError("worker may not mark completed/discarded via transition")
        updated = replace(self.manifest, state=state)
        _manifest_commit(self._dir_fd, self.manifest_name, updated, self.guard)
        self.manifest = updated

    def discard(self, *, confirmed: bool) -> None:
        """Explicitly remove only this transaction, never finalized restore points."""
        if not confirmed:
            raise PermissionError("partial discard requires explicit confirmation")
        self._check()
        if _exists(self._dir_fd, self.final_name):
            raise RuntimeError("a finalized snapshot exists: discard cannot remove it")
        self.guard.validate_fd(self._dir_fd)
        os.unlink(self.part_name, dir_fd=self._dir_fd)
        self.guard.validate_fd(self._dir_fd)
        os.unlink(self.manifest_name, dir_fd=self._dir_fd)
        os.fsync(self._dir_fd)
        self.close()

    def authorize_replayed_prefix(self, proof: ReplayProof) -> None:
        """Enable remote append only for a fully hash-matched committed prefix.

        A reopened sink starts write-locked. The replay stage must consume and
        verify every checkpoint in the same immutable btrfs send before this
        method can unlock the first missing checkpoint.
        """
        self._check()
        if self.append_allowed:
            raise RuntimeError("fresh sink does not require a replay proof")
        expected = json.loads(serialize_manifest(self.manifest))
        if (
            not isinstance(proof, ReplayProof)
            or proof.transfer_id != self.manifest.transfer_id
            or proof.matched_checkpoints != len(self.manifest.checkpoints)
            or proof.matched_raw_bytes != expected["committed_raw_bytes"]
            or not hmac.compare_digest(
                proof.checkpoint_index_sha256,
                expected["checkpoint_index_sha256"],
            )
        ):
            raise ValueError("resume proof does not match committed checkpoints")
        if os.fstat(self._part_fd).st_size != expected["committed_compressed_bytes"]:
            raise ValueError("resumable part has unexpected bytes")
        self.guard.validate_fd(self._dir_fd)
        self.append_allowed = True

    def publish(self, meta: bytes) -> Path:
        """Sidecar-first, no-replace publication; never publish a partial alone."""
        self._check()
        if not self.append_allowed:
            raise RuntimeError("source replay not verified before finalization")
        if not self.manifest.checkpoints:
            raise ValueError("cannot publish empty checkpoint stream")
        data = _validate_meta(meta, self.snapshot_name, self.manifest)
        self.guard.validate_fd(self._dir_fd)
        if _exists(self._dir_fd, self.final_name):
            raise FileExistsError("final restore point already exists")
        if os.fstat(self._part_fd).st_size != sum(
            c.compressed_length for c in self.manifest.checkpoints
        ):
            raise RuntimeError("checkpointed stream has an uncommitted tail")
        os.fsync(self._part_fd)
        meta_name = self.final_name + ".meta"
        if _exists(self._dir_fd, meta_name):
            # A prior sidecar-first publish might have crashed before final rename.
            fd = os.open(meta_name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self._dir_fd)
            try:
                with os.fdopen(fd, "rb") as existing:
                    current_data = existing.read(MAX_MANIFEST_BYTES + 1)
                    try:
                        previous = json.loads(current_data)
                        proposed = json.loads(data)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise FileExistsError(
                            "previous sidecar is not trustworthy"
                        ) from exc
                    if not isinstance(previous, dict) or not isinstance(proposed, dict):
                        raise FileExistsError("invalid existing final sidecar")
                    # A crash after committing the sidecar but before stream
                    # publication should not strand the backup just because
                    # a retried RawSnapshot has a new discovery timestamp.
                    previous.pop("created", None)
                    proposed.pop("created", None)
                    if previous != proposed:
                        raise FileExistsError("different authoritative sidecar exists")
            except Exception:
                raise
        else:
            temp = f".harbor-final-{uuid.uuid4().hex}.tmp"
            fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._dir_fd,
            )
            try:
                with os.fdopen(fd, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                self.guard.validate_fd(self._dir_fd)
                _rename_noreplace(self._dir_fd, temp, meta_name)
                os.fsync(self._dir_fd)
            finally:
                try:
                    os.unlink(temp, dir_fd=self._dir_fd)
                except FileNotFoundError:
                    pass
        self.guard.validate_fd(self._dir_fd)
        _rename_noreplace(self._dir_fd, self.part_name, self.final_name)
        os.fsync(self._dir_fd)
        completed = replace(self.manifest, state="completed")
        _manifest_commit(self._dir_fd, self.manifest_name, completed, self.guard)
        self.manifest = completed
        final_path = self.root / self.final_name
        self.append_allowed = False
        self.close()
        return final_path

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._part_fd)
            os.close(self._dir_fd)

    def __enter__(self) -> ResumableRawSink:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _verify_committed_frames(
    fd: int, manifest: ResumeManifest, directory_fd: int, guard: DirectoryGuard
) -> None:
    """Verify existing compressed checkpoint bytes without retransmission.

    The index digest alone does not prove the disk still contains the
    compressed payload it originally described. Hash each committed frame
    before authorizing new appends, and never truncate/correct damaged frames.
    """
    for entry in manifest.checkpoints:
        guard.validate_fd(directory_fd)
        os.lseek(fd, entry.compressed_offset, os.SEEK_SET)
        digest = hashlib.sha256()
        remaining = entry.compressed_length
        while remaining:
            chunk = os.read(fd, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("checkpoint compressed payload is unexpectedly short")
            digest.update(chunk)
            remaining -= len(chunk)
        if not hmac.compare_digest(digest.hexdigest(), entry.compressed_sha256):
            raise ValueError(
                f"compressed checkpoint {entry.sequence} checksum mismatch"
            )


def load_existing(
    root: Path,
    snapshot_name: str,
    transfer_id: str,
    guard: DirectoryGuard,
    *,
    expected_manifest: ResumeManifest,
) -> ResumableRawSink:
    """Reopen and reconcile, but never permit append until replay is verified."""
    _filename(snapshot_name)
    if str(uuid.UUID(transfer_id)) != transfer_id:
        raise ValueError("invalid transfer UUID")
    directory_fd = _dir_open(root)
    part_fd = -1
    try:
        guard.validate_fd(directory_fd)
        manifest_name = f".harbor-resume-{transfer_id}.json"
        manifest = _read_manifest(directory_fd, manifest_name)
        if (
            manifest.transfer_id != transfer_id
            or manifest.identity != expected_manifest.identity
            or manifest.checkpoint_size != expected_manifest.checkpoint_size
        ):
            raise ValueError("resume identity mismatch")
        part_name = f"{snapshot_name}.btrfs.zst.{transfer_id}.part"
        part_fd = os.open(
            part_name,
            os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory_fd,
        )
        if not _is_file(part_fd):
            raise ValueError("resume partial is not a regular file")
        _lock_part(part_fd)
        target = sum(c.compressed_length for c in manifest.checkpoints)
        current = os.fstat(part_fd).st_size
        if current < target:
            raise ValueError("partial shorter than committed checkpoints")
        _verify_committed_frames(part_fd, manifest, directory_fd, guard)
        if current > target:
            guard.validate_fd(directory_fd)
            os.ftruncate(part_fd, target)
            os.fsync(part_fd)
        guard.validate_fd(directory_fd)
        return ResumableRawSink(
            root,
            directory_fd,
            part_fd,
            snapshot_name,
            manifest,
            guard,
            append_allowed=False,
        )
    except BaseException:
        if part_fd >= 0:
            os.close(part_fd)
        os.close(directory_fd)
        raise
