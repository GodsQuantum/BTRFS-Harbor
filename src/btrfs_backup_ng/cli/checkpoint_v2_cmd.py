"""Explicit, opt-in checkpointed raw v2 commands.

No system-wide installation or automatic remapping of legacy raw targets.
Without --experimental the new send and resume code cannot write backups;
without --allow-local, start refuses non-network target filesystems.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import cast

from .. import __version__
from ..core.checkpoint_control_v2 import ControlJournal
from ..core.checkpoint_v2 import (
    MAX_MANIFEST_BYTES,
    ResumeManifest,
    commit_manifest,
    parse_manifest,
    read_manifest,
    serialize_manifest,
)
from ..core.verify_v2 import verify_checkpoint_index
from ..core.checkpoint_v2_runner import SendProcess, execute_checkpoint_job
from ..core.native_snapshots import (
    list_native_snapshots,
    native_snapshot_sort_key,
    find_native_snapshot,
    create_native_snapshot,
)
from ..core.native_send_v2 import (
    destination_fingerprint,
    inspect_readonly_btrfs_source,
    spawn_btrfs_send,
)
from ..core.performance_v2 import apply_resource_policy, resolve_performance_profile
from ..core.replay_v2 import DestinationFingerprint, SourceFingerprint
from ..endpoint.mount_guard_v2 import (
    MountGuard,
    REMOTE_TYPES,
    capture_mount_identity,
)
from ..endpoint.resumable_raw import load_existing
from ..snapper.pin import LiveSnapshot, PinManager
from ..snapper.scanner import SnapperScanner
from ..snapper.source_policy import (
    resolve_source_identity,
    select_source,
)
from ..endpoint.raw_metadata import RawSnapshot, discover_raw_snapshots

STATES_RESUMABLE = frozenset(
    {
        "preparing",
        "uploading",
        "pause_requested",
        "paused",
        "replaying",
        "failed_resumable",
        "finalizing",
    }
)


def _safe_id(value: str) -> str:
    parsed = uuid.UUID(value)
    if str(parsed) != value:
        raise ValueError("invalid canonical transfer UUID")
    return value


def _state_root(specified: str | None = None) -> Path:
    if specified is not None:
        state = Path(specified)
    else:
        xdg = os.getenv("XDG_STATE_HOME")
        base = Path(xdg) if xdg else Path.home() / ".local" / "state"
        state = base / "btrfs-harbor" / "checkpoint-v2"
    if not state.is_absolute():
        raise ValueError("control state directory must be absolute")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if state.is_symlink() or not state.is_dir():
        raise ValueError("unsafe checkpoint control state directory")
    return state


def _target_root(target: str) -> Path:
    path = Path(target)
    if not path.is_absolute() or not path.is_dir() or path.is_symlink():
        raise ValueError(
            "checkpoint destination must already exist as an absolute directory"
        )
    return path


def _open_guard(
    root: Path, *, allow_local: bool, expected: str | None = None
) -> tuple[MountGuard, str]:
    current = capture_mount_identity(root)
    if current.fstype not in REMOTE_TYPES and not allow_local:
        raise ValueError(
            "checkpoint destination is not NFS/SMB; --allow-local is required "
            "to avoid writing to an underlying path after a lost mount"
        )
    stable = destination_fingerprint(current, root)
    if expected is not None and stable != expected:
        raise ValueError(
            "destination mount/export fingerprint differs from original job"
        )
    return MountGuard(root, current, required_remote=not allow_local), stable


def _manifest_path(root: Path, transfer_id: str) -> Path:
    return root / f".harbor-resume-{_safe_id(transfer_id)}.json"


def _source_check(path: str):
    return inspect_readonly_btrfs_source(Path(path))


def _snapper_query(config: str, number: int) -> LiveSnapshot | None:
    scanner = SnapperScanner()
    candidate = next(
        (s for s in scanner.get_snapshots(config) if s.number == number), None
    )
    if candidate is None:
        return None
    resolved = resolve_source_identity(candidate)
    if not resolved.exists or not resolved.uuid:
        return None
    return LiveSnapshot(resolved.uuid, candidate.cleanup)


def _pin_manager(state_dir: Path) -> PinManager:
    return PinManager(state_dir / "snapper-pins.json", query=_snapper_query)


def _validate_requested_snapper(
    *,
    snapshot_path: Path,
    config: str | None,
    number: int | None,
    source_uuid: str,
) -> None:
    if config is None and number is None:
        return
    if not config or type(number) is not int or number <= 0:
        raise ValueError("both --snapper-config and --snapper-number must be set")
    scanner = SnapperScanner()
    snap = next((s for s in scanner.get_snapshots(config) if s.number == number), None)
    if snap is None or snap.subvolume_path.resolve() != snapshot_path.resolve():
        raise ValueError("selected Snapper config/number does not match source path")
    resolved = resolve_source_identity(snap)
    if not resolved.exists or not resolved.readonly or resolved.uuid != source_uuid:
        raise ValueError("selected Snapper source changed or is not read-only")


def _resolve_source_choice(args: argparse.Namespace, target_root: Path) -> None:
    """Resolve the source once, by Snapper UUID and explicit policy.

    Only reads Snapper state for latest/selected. Create is a native Snapper
    command, executed solely after --experimental and destination preflight.
    """
    mode = args.source_mode
    if mode in ("latest-native", "selected-native", "create-native"):
        if not args.source or args.snapper_config or args.snapper_number:
            raise ValueError(
                "native mode requires a subvolume path but no Snapper identity"
            )
        live_root = Path(args.source)
        if mode == "create-native":
            chosen_native = create_native_snapshot(live_root)
        elif mode == "selected-native":
            chosen_native = find_native_snapshot(live_root, args.native_name or "")
        else:
            candidates = list_native_snapshots(live_root)
            if not candidates:
                chosen_native = create_native_snapshot(live_root)
            else:
                chosen_native = candidates[0]
        if chosen_native.uuid in {
            item.source_uuid for item in discover_raw_snapshots(target_root)
        }:
            raise ValueError(
                "this native snapshot is already backed up; create a new one"
            )
        args.native_root = str(live_root)
        args.source = str(chosen_native.path)
        return
    if mode == "path":
        if not args.source:
            raise ValueError("source --source is required for source-mode path")
        if args.snapper_config is None and args.snapper_number is not None:
            raise ValueError("Snapper number requires a config")
        return
    config = args.snapper_config
    if not isinstance(config, str) or not re.fullmatch(
        r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", config
    ):
        raise ValueError("Snapper mode requires a valid --snapper-config")
    if args.source is not None:
        raise ValueError("Snapper source mode must not also specify --source")
    if mode == "selected-snapper" and (
        type(args.snapper_number) is not int or args.snapper_number <= 0
    ):
        raise ValueError("selected Snapper mode requires --snapper-number")
    if mode == "create-snapper":
        if args.snapper_number is not None:
            raise ValueError("create Snapper does not accept an existing number")
        result = subprocess.run(
            [
                "snapper",
                "-c",
                config,
                "create",
                "--type",
                "single",
                "--print-number",
                "--cleanup-algorithm",
                "",
                "--description",
                "Btrfs Harbor checkpointed backup",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Snapper snapshot creation failed: {result.stderr.strip()[:240]}"
            )
        try:
            args.snapper_number = int(result.stdout.strip())
        except ValueError as exc:
            raise RuntimeError(
                "Snapper did not report the new snapshot number"
            ) from exc
    scanner = SnapperScanner()
    all_snapshots = scanner.get_snapshots(config)
    remote = {
        snapshot.source_uuid
        for snapshot in discover_raw_snapshots(target_root)
        if snapshot.source_uuid
    }
    selected = select_source(
        all_snapshots,
        mode="selected" if mode != "latest-snapper" else "latest",
        resolve=resolve_source_identity,
        remote_uuids=set() if mode == "latest-snapper" else remote,
        selected_number=args.snapper_number,
        selected_config=config,
    )
    if selected is None or selected.snapshot is None:
        raise ValueError(
            "no eligible new Snapper snapshot: latest may already be backed up"
        )
    if mode == "latest-snapper" and selected.source_uuid in remote:
        raise ValueError(
            "latest stable Snapper snapshot already backed up; "
            "do not send older snapshots for a daily-latest policy"
        )
    source_path = getattr(selected.snapshot, "subvolume_path", None)
    if not isinstance(source_path, Path) or not source_path.is_absolute():
        raise ValueError("Snapper snapshot has no absolute subvolume path")
    args.source = str(source_path)
    args.snapper_number = selected.snapshot.number


def _valid_saved_chain(
    item: RawSnapshot, by_name: dict[str, RawSnapshot], visited: set[str]
) -> bool:
    name = getattr(item, "name", None)
    if not isinstance(name, str) or name in visited or len(visited) > 256:
        return False
    if not (
        getattr(item, "provenance_origin", None) == "native-write"
        and getattr(item, "stream_completeness", None) == "complete"
        and isinstance(getattr(item, "checksum_value", None), str)
        and re.fullmatch("[0-9a-f]{64}", str(item.checksum_value))
        and getattr(item, "checksum_algorithm", "sha256") == "sha256"
        and item.stream_path.is_file()
        and not item.stream_path.is_symlink()
        and item.metadata_path.is_file()
        and not item.metadata_path.is_symlink()
    ):
        return False
    parent_uuid = getattr(item, "parent_uuid", None)
    parent_name = getattr(item, "parent_name", None)
    if not parent_uuid:
        return not parent_name
    ancestor = by_name.get(parent_name) if parent_name else None
    if ancestor is None or getattr(ancestor, "source_uuid", None) != parent_uuid:
        return False
    return _valid_saved_chain(ancestor, by_name, visited | {name})


def _incremental_depth(snapshot: RawSnapshot, entries: dict[str, RawSnapshot]) -> int:
    """Depth of an already-verified remote chain from its most recent full."""
    current = snapshot
    count = 0
    visited: set[str] = set()
    while current.parent_name:
        if current.name in visited or count > 256:
            raise ValueError("invalid incremental ancestor chain")
        visited.add(current.name)
        parent = entries.get(current.parent_name)
        if parent is None or parent.source_uuid != current.parent_uuid:
            raise ValueError("unrestorable incremental parent chain")
        current = parent
        count += 1
    return count


def _select_automatic_incremental_parent(
    args: argparse.Namespace, target_root: Path
) -> None:
    """Use the latest *restorable* remote base backed by a local readonly Snapper source.

    First transfer of a selected snapshot is FULL (one self-contained base).
    Never invent a differential against an absent snapshot or a broken chain.
    Explicit --parent retains the existing strict validation path.
    """
    if args.parent is not None:
        return
    maximum = getattr(args, "max_incremental_depth", 7)
    if type(maximum) is not int or not 1 <= maximum <= 256:
        raise ValueError("max incremental depth must be between 1 and 256")
    if getattr(args, "native_root", None):
        stored = discover_raw_snapshots(target_root)
        chain = {item.name: item for item in stored}
        remote_by_uuid = {
            item.source_uuid: item
            for item in stored
            if item.source_uuid and _valid_saved_chain(item, chain, set())
        }
        selected = Path(args.source)
        for native_candidate in list_native_snapshots(Path(args.native_root)):
            if native_candidate.path == selected:
                continue
            if (
                native_snapshot_sort_key(native_candidate.name)
                < native_snapshot_sort_key(selected.name)
                and native_candidate.uuid in remote_by_uuid
            ):
                # Periodic independent full sends allow older incremental
                # chains to expire without ever orphaning a kept descendant.
                if (
                    _incremental_depth(remote_by_uuid[native_candidate.uuid], chain)
                    >= maximum
                ):
                    return
                args.parent = str(native_candidate.path)
                return
        return
    if args.source_mode == "path" or not args.snapper_config:
        return
    snapshots = SnapperScanner().get_snapshots(args.snapper_config)
    saved = discover_raw_snapshots(target_root)
    by_name = {item.name: item for item in saved}

    # Match UUIDs, not filename/creation time: no unrelated parent can qualify.
    remote = {
        item.source_uuid: item
        for item in saved
        if isinstance(getattr(item, "source_uuid", None), str)
        and item.source_uuid
        and _valid_saved_chain(item, by_name, set())
    }
    if not remote:
        return
    selected_path = Path(args.source)
    chosen = next(
        (snap for snap in snapshots if snap.subvolume_path == selected_path),
        None,
    )
    if chosen is None:
        return
    candidates = sorted(
        (
            snap
            for snap in snapshots
            if snap.subvolume_path != selected_path
            and snap.config_name == args.snapper_config
            and (snap.date, snap.number) < (chosen.date, chosen.number)
        ),
        key=lambda snap: (snap.date, snap.number),
        reverse=True,
    )
    for candidate in candidates:
        path = candidate.subvolume_path
        if not isinstance(path, Path) or not path.is_dir() or path.is_symlink():
            continue
        resolved = resolve_source_identity(candidate)
        if resolved.exists and resolved.readonly and resolved.uuid in remote:
            if _incremental_depth(remote[resolved.uuid], by_name) >= maximum:
                return
            args.parent = str(path)
            args.parent_snapper_number = candidate.number
            return


def _saved_parent_name(root: Path, uuid_string: str) -> str:
    """Refuse an absent/corrupt parent or an otherwise broken restore chain."""
    saved = discover_raw_snapshots(root)
    by_name = {item.name: item for item in saved}
    for snapshot in saved:
        if snapshot.source_uuid == uuid_string and _valid_saved_chain(
            snapshot, by_name, set()
        ):
            return snapshot.name
    raise ValueError(
        "incremental parent chain is not authoritative and complete at this "
        "destination; save a full base before using an incremental send"
    )


def _release_snapper_pins(state: Path, manifest: ResumeManifest) -> None:
    config = manifest.identity.get("snapper_config")
    source_number = manifest.identity.get("snapper_number")
    if not isinstance(config, str) or type(source_number) is not int:
        return
    pins = _pin_manager(state)
    pins.release(
        config,
        source_number,
        str(manifest.identity["source_uuid"]),
        manifest.transfer_id,
    )
    parent_number = manifest.identity.get("parent_snapper_number")
    parent_uuid = manifest.identity.get("parent_uuid")
    if type(parent_number) is int and isinstance(parent_uuid, str):
        pins.release(config, parent_number, parent_uuid, manifest.transfer_id)


def _release_if_never_started(
    *,
    root: Path,
    name: str,
    manifest: ResumeManifest,
    state: Path,
    allow_local: bool,
) -> bool:
    """Undo native Snapper pins only when NO durable transfer entry exists.

    Filesystem failure, lost NFS mount, manifest, partial, or final stream:
    fail closed, preserve leases for later reconciliation. This is a cleanup
    of pre-send failures, never a way to discard an interrupted backup.
    """
    try:
        guard, _ = _open_guard(
            root,
            allow_local=allow_local,
            expected=str(manifest.identity["destination_fingerprint"]),
        )
        with guard:
            guard.validate_fd(guard.directory_fd)
            names = (
                f"{name}.btrfs.zst.{manifest.transfer_id}.part",
                f".harbor-resume-{manifest.transfer_id}.json",
                f"{name}.btrfs.zst",
            )
            for item in names:
                try:
                    os.stat(item, dir_fd=guard.directory_fd, follow_symlinks=False)
                    return False
                except FileNotFoundError:
                    pass
            _release_snapper_pins(state, manifest)
            return True
    except (OSError, RuntimeError, ValueError):
        return False


def _new_manifest(
    args: argparse.Namespace, stable: str
) -> tuple[ResumeManifest, SourceFingerprint, Path | None]:
    source = _source_check(args.source)
    if not source.readonly:
        raise ValueError("Btrfs source is not read-only")
    parent_path = Path(args.parent) if args.parent else None
    if parent_path is not None:
        parent = _source_check(str(parent_path))
        if not parent.readonly:
            raise ValueError("incremental parent is not read-only")
        source = replace(source, parent_uuid=parent.uuid)
        if args.source == args.parent or parent.uuid == source.uuid:
            raise ValueError("Btrfs incremental parent cannot equal source")
        saved_parent_name = _saved_parent_name(Path(args.target), parent.uuid)
    else:
        saved_parent_name = None
    size = args.checkpoint_size_mib
    if type(size) is not int or not 1 <= size <= 1024:
        raise ValueError("checkpoint size must be 1..1024 MiB")
    profile = resolve_performance_profile(args.performance)
    identity: dict[str, str | int | None] = {
        "profile_id": args.profile_id,
        "archive_name": args.name,
        "source_volume": getattr(args, "native_root", args.source),
        "source_uuid": source.uuid,
        "source_path": source.path,
        "parent_uuid": source.parent_uuid,
        "send_fingerprint": source.send_fingerprint,
        "compression": "zstd",
        "compression_level": profile.level,
        "encryption": "none",
        "destination_type": "raw",
        "destination_fingerprint": stable,
        "kernel_release": os.uname().release,
        "btrfs_progs_version": _btrfs_version(),
        "harbor_version": "0.2.6",
        "engine_version": __version__,
    }
    if parent_path is not None:
        identity["parent_path"] = str(parent_path)
        identity["parent_backup_name"] = saved_parent_name
        parent_snapper = getattr(args, "parent_snapper_number", None)
        if type(parent_snapper) is int:
            identity["parent_snapper_number"] = parent_snapper
    if args.snapper_config is not None:
        identity["snapper_config"] = args.snapper_config
        identity["snapper_number"] = args.snapper_number
    m = ResumeManifest(
        schema_version=2,
        transfer_id=str(uuid.uuid4()),
        identity=identity,
        checkpoint_size=size * 1024 * 1024,
        state="preparing",
        checkpoints=(),
    )
    serialize_manifest(m)
    return m, source, parent_path


def _btrfs_version() -> str:
    result = subprocess.run(
        ["btrfs", "--version"], capture_output=True, text=True, timeout=10
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError("btrfs-progs version not available")
    return result.stdout.strip()[:255]


def _send_worker(
    *,
    root: Path,
    name: str,
    manifest: ResumeManifest,
    source: SourceFingerprint,
    parent: Path | None,
    state_dir: Path,
    allow_local: bool,
    resume: bool,
) -> dict:
    guard, fingerprint = _open_guard(
        root,
        allow_local=allow_local,
        expected=str(manifest.identity["destination_fingerprint"]),
    )
    with guard:
        control = ControlJournal(state_dir, manifest.transfer_id)
        control.request("run")
        dest = DestinationFingerprint(type="raw", fingerprint=fingerprint)
        recorded_level = manifest.identity["compression_level"]
        if type(recorded_level) is not int:
            raise ValueError("manifest compression level is not an integer")
        settings = resolve_performance_profile(
            "custom", overrides={"threads": 2, "level": recorded_level}
        )

        def spawn() -> SendProcess:
            process = spawn_btrfs_send(
                Path(str(manifest.identity["source_path"])), parent
            )
            if process.stdout is None:
                process.kill()
                process.wait()
                raise RuntimeError("btrfs send did not expose a stdout stream")
            pid = getattr(process, "pid", None)
            if type(pid) is int and pid > 1:
                try:
                    control.register_send(pid)
                except BaseException:
                    process.terminate()
                    process.wait()
                    stderr_log = getattr(process, "_harbor_stderr_log", None)
                    if stderr_log is not None:
                        stderr_log.close()
                    raise
                warnings = apply_resource_policy(pid, settings)
                for warning in warnings:
                    print(
                        f"checkpoint-v2 resource policy warning: {warning}",
                        file=sys.stderr,
                    )
            return cast(SendProcess, process)

        print(
            json.dumps(
                {
                    "event": "started",
                    "transfer_id": manifest.transfer_id,
                    "resume": resume,
                    "snapshot": name,
                }
            ),
            flush=True,
        )
        try:
            result = execute_checkpoint_job(
                target_root=root,
                snapshot_name=name,
                manifest=manifest,
                source=source,
                destination=dest,
                guard=guard,
                spawn=spawn,
                action=control.action,
                resume=resume,
                chunk_size=manifest.checkpoint_size,
                level=settings.level,
                threads=settings.threads,
            )
        finally:
            control.clear_active_send()
        return {
            "transfer_id": result.transfer_id,
            "status": result.status,
            "new_raw_bytes": result.new_raw_bytes,
            "new_checkpoints": result.new_checkpoints,
        }


def execute_checkpoint_v2(args: argparse.Namespace) -> int:
    """Public dispatcher for opt-in commands; error means no fake completion."""
    try:
        return _execute(args)
    except (OSError, RuntimeError, ValueError, PermissionError) as exc:
        print(f"checkpoint-v2: {exc}", file=sys.stderr)
        return 2


def _list_v2(root: Path, *, max_entries: int = 100) -> list[dict[str, object]]:
    """Read-only, bounded, nofollow preview, safe to run through native polkit."""
    entries: list[dict[str, object]] = []
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        names = sorted(os.listdir(directory_fd))[:8192]
        for filename in names:
            if len(entries) >= max_entries:
                break
            if not (
                filename.startswith(".harbor-resume-") and filename.endswith(".json")
            ):
                continue
            transfer_id = filename[len(".harbor-resume-") : -len(".json")]
            try:
                if _safe_id(transfer_id) != transfer_id:
                    continue
                fd = os.open(
                    filename,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(fd).st_mode):
                        continue
                    if os.fstat(fd).st_size > MAX_MANIFEST_BYTES:
                        continue
                    with os.fdopen(fd, "rb", closefd=False) as reader:
                        manifest = parse_manifest(reader.read(MAX_MANIFEST_BYTES + 1))
                finally:
                    os.close(fd)
            except (OSError, ValueError, TypeError):
                continue
            if manifest.transfer_id != transfer_id:
                continue
            match_suffix = f".btrfs.zst.{transfer_id}.part"
            names_found = [
                name[: -len(match_suffix)]
                for name in names
                if name.endswith(match_suffix)
                and not name.startswith(".")
                and 0 < len(name[: -len(match_suffix)]) <= 200
                and "/" not in name
            ]
            archive_name = names_found[0] if len(names_found) == 1 else None
            reserved_name = manifest.identity.get("archive_name")
            if reserved_name is not None:
                if not isinstance(reserved_name, str):
                    continue
                if archive_name is not None and archive_name != reserved_name:
                    continue
                archive_name = reserved_name
            # A completed stream no longer has a partial: check its sidecar
            # transaction ID read-only and with a nofollow handle.
            if archive_name is None and manifest.state == "completed":
                for metadata_name in names:
                    if not metadata_name.endswith(".btrfs.zst.meta"):
                        continue
                    try:
                        meta_fd = os.open(
                            metadata_name,
                            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=directory_fd,
                        )
                        try:
                            if not stat.S_ISREG(os.fstat(meta_fd).st_mode):
                                continue
                            if os.fstat(meta_fd).st_size > MAX_MANIFEST_BYTES:
                                continue
                            with os.fdopen(meta_fd, "rb", closefd=False) as stream:
                                data = json.loads(stream.read(MAX_MANIFEST_BYTES + 1))
                        finally:
                            os.close(meta_fd)
                        if (
                            data.get("harbor_checkpoint_v2", {}).get("transfer_id")
                            == transfer_id
                        ):
                            archive_name = metadata_name[: -len(".btrfs.zst.meta")]
                            break
                    except (OSError, ValueError, TypeError, AttributeError):
                        continue
            payload = json.loads(serialize_manifest(manifest))
            entries.append(
                {
                    "transfer_id": transfer_id,
                    "name": archive_name,
                    "state": manifest.state,
                    "source": manifest.identity["source_path"],
                    "checkpoint_count": len(manifest.checkpoints),
                    "committed_raw_bytes": payload["committed_raw_bytes"],
                    "committed_compressed_bytes": payload["committed_compressed_bytes"],
                    "last_checkpoint_at": (
                        manifest.checkpoints[-1].committed_at
                        if manifest.checkpoints
                        else None
                    ),
                    "resumable": manifest.state in STATES_RESUMABLE
                    and archive_name is not None,
                    "verified": False,
                }
            )
    finally:
        os.close(directory_fd)
    entries.sort(key=lambda r: str(r["last_checkpoint_at"] or ""), reverse=True)
    return entries


def _execute(args: argparse.Namespace) -> int:
    action = args.checkpoint_action
    if action == "ssh-receiver-init":
        from ..ssh_checkpoint_v2_receiver import initialize_receiver

        result = initialize_receiver(Path(args.root), allow_local=args.allow_local)
        print(json.dumps(result, sort_keys=True))
        return 0
    if action == "ssh-receiver":
        from ..ssh_checkpoint_v2_receiver import receiver_main

        return receiver_main(args.root)
    if action == "ssh-mirror":
        if not args.experimental:
            raise ValueError("--experimental required for checkpointed SSH")
        from ..ssh_checkpoint_v2_client import SSHMirror, mirror_machine_set
        from ..core.native_send_v2 import destination_fingerprint
        from ..endpoint.mount_guard_v2 import capture_mount_identity, REMOTE_TYPES

        root = _target_root(args.target)
        identity = capture_mount_identity(root)
        if identity.fstype not in REMOTE_TYPES and not args.allow_local:
            raise ValueError("local source staging requires --allow-local")
        from .machine_set_v2 import _read

        record = _read(root, args.set_id)
        if record["destination_fingerprint"] != destination_fingerprint(identity, root):
            raise ValueError("SSH source destination identity changed")
        client = SSHMirror(
            host=args.host,
            user=args.user,
            port=args.port,
            key=Path(args.identity_file),
            known_hosts=Path(args.known_hosts),
        )
        response = mirror_machine_set(root, args.set_id, client)
        print(json.dumps({"mirrored": True, "files": response}, sort_keys=True))
        return 0
    if action == "schedule-run":
        from .scheduled_v2 import execute_scheduled_checkpoint_v2

        return execute_scheduled_checkpoint_v2(args)
    if action in (
        "set-start",
        "set-resume",
        "set-status",
        "set-restore",
        "set-rear-copy",
        "set-rear-recover",
        "set-rescue-iso",
        "set-list",
        "set-retention-plan",
    ):
        from .machine_set_v2 import execute_machine_set

        return execute_machine_set(args)
    if action == "machine-inventory":
        from ..core.machine_inventory_v2 import inspect_live_machine

        print(json.dumps(inspect_live_machine(), sort_keys=True))
        return 0
    if action == "native-list":
        items = list_native_snapshots(Path(args.source))
        print(
            json.dumps(
                [
                    {
                        "name": item.name,
                        "path": str(item.path),
                        "uuid": item.uuid,
                        "date": item.date,
                    }
                    for item in items
                ]
            )
        )
        return 0
    if action == "list":
        print(json.dumps(_list_v2(_target_root(args.target)), sort_keys=True))
        return 0
    if action == "status":
        root = _target_root(args.target)
        m = read_manifest(_manifest_path(root, args.transfer_id))
        payload = json.loads(serialize_manifest(m))
        payload["resumable"] = m.state in STATES_RESUMABLE
        # The estimated full raw size is NOT known from a Btrfs send until
        # its source exits. Do not present a fake percent complete or ETA.
        payload["progress"] = {
            "phase": m.state,
            "checkpoint_count": len(m.checkpoints),
            "committed_raw_bytes": payload["committed_raw_bytes"],
            "committed_compressed_bytes": payload["committed_compressed_bytes"],
            "last_committed_at": m.checkpoints[-1].committed_at
            if m.checkpoints
            else None,
            "total_raw_bytes": None,
            "percent": None,
            "eta_seconds": None,
            "can_resume": m.state in STATES_RESUMABLE,
            "can_pause": m.state in ("preparing", "uploading", "replaying"),
            "can_stop": m.state in ("preparing", "uploading", "replaying"),
            "can_discard": m.state != "completed",
        }
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    if action in ("pause", "stop"):
        control = ControlJournal(
            _state_root(args.state_dir), _safe_id(args.transfer_id)
        )
        control.request(action)
        signal_sent = control.signal_registered_send() if action == "stop" else False
        print(
            json.dumps(
                {
                    "requested": action,
                    "transfer_id": args.transfer_id,
                    "status": "signalled active btrfs send"
                    if signal_sent
                    else "queued for checkpoint boundary",
                    "signal_sent": signal_sent,
                }
            )
        )
        return 0
    if action == "discard":
        if not args.confirm:
            print(
                "checkpoint-v2: --confirm is mandatory to discard a partial",
                file=sys.stderr,
            )
            return 2
        root = _target_root(args.target)
        manifest = read_manifest(_manifest_path(root, args.transfer_id))
        guard, _ = _open_guard(
            root,
            allow_local=args.allow_local,
            expected=str(manifest.identity["destination_fingerprint"]),
        )
        with guard:
            if manifest.state == "completed":
                raise ValueError("completed restore points cannot be discarded")
            sink = load_existing(
                root, args.name, args.transfer_id, guard, expected_manifest=manifest
            )
            with sink:
                sink.discard(confirmed=True)
        # Unpin both the source and any automatically selected readonly parent.
        _release_snapper_pins(_state_root(args.state_dir), manifest)
        print(json.dumps({"discarded": args.transfer_id}))
        return 0
    if action not in ("start", "resume"):
        raise ValueError("unknown checkpoint action")
    if not args.experimental:
        print(
            "checkpoint-v2: explicit --experimental required; bootable recovery and "
            "checkpointed SSH are not yet certified",
            file=sys.stderr,
        )
        return 2
    root = _target_root(args.target)
    guard, stable = _open_guard(root, allow_local=args.allow_local)
    guard.close()
    state = _state_root(args.state_dir)
    if action == "start":
        _resolve_source_choice(args, root)
        _select_automatic_incremental_parent(args, root)
        manifest, source, parent = _new_manifest(args, stable)
        _validate_requested_snapper(
            snapshot_path=Path(str(manifest.identity["source_path"])),
            config=args.snapper_config,
            number=args.snapper_number,
            source_uuid=source.uuid,
        )
        pinned = args.snapper_config is not None
        if pinned:
            pins = _pin_manager(state)
            pins.acquire(
                args.snapper_config,
                args.snapper_number,
                source.uuid,
                manifest.transfer_id,
                restore_cleanup=(
                    "number" if args.source_mode == "create-snapper" else None
                ),
            )
            parent_number = manifest.identity.get("parent_snapper_number")
            if parent is not None and type(parent_number) is int:
                try:
                    pins.acquire(
                        args.snapper_config,
                        parent_number,
                        str(manifest.identity["parent_uuid"]),
                        manifest.transfer_id,
                    )
                except BaseException:
                    # No send has started: safe to unwind only the just-acquired
                    # source pin. Never unlock a parent while an active transfer
                    # uses it; no v2 partial can exist yet.
                    pins.release(
                        args.snapper_config,
                        args.snapper_number,
                        source.uuid,
                        manifest.transfer_id,
                    )
                    raise
        try:
            result = _send_worker(
                root=root,
                name=args.name,
                manifest=manifest,
                source=source,
                parent=parent,
                state_dir=state,
                allow_local=args.allow_local,
                resume=False,
            )
        except BaseException:
            # No part + no journal + unchanged NFS identity means the worker
            # failed before it could create a resumable transaction. Unpin
            # its Snapper source; otherwise leave leases intact for recovery.
            _release_if_never_started(
                root=root,
                name=args.name,
                manifest=manifest,
                state=state,
                allow_local=args.allow_local,
            )
            raise
    else:
        manifest = read_manifest(_manifest_path(root, args.transfer_id))
        reserved_name = manifest.identity.get("archive_name")
        if reserved_name is not None and reserved_name != args.name:
            raise ValueError(
                "requested archive differs from immutable checkpoint identity"
            )
        # A crash may occur after the final stream and .meta are durable but
        # before the journal could be switched to completed. Never regenerate
        # a huge btrfs send merely to acknowledge a finished archive.
        final = root / f"{args.name}.btrfs.zst"
        metadata = root / f"{args.name}.btrfs.zst.meta"
        if final.exists():
            guard, _ = _open_guard(
                root,
                allow_local=args.allow_local,
                expected=str(manifest.identity["destination_fingerprint"]),
            )
            with guard:
                proof = verify_checkpoint_index(metadata, final, full=True)
                if proof.status != "ok":
                    raise ValueError(
                        "a final stream already exists but did not pass full v2 verification: "
                        + proof.detail
                    )
                completed = replace(manifest, state="completed")
                commit_manifest(
                    _manifest_path(root, manifest.transfer_id),
                    completed,
                    validate_destination=lambda: guard.validate_fd(guard.directory_fd),
                )
                _release_snapper_pins(state, manifest)
            print(
                json.dumps(
                    {
                        "transfer_id": manifest.transfer_id,
                        "status": "completed",
                        "recovered_final_publish": True,
                        "new_raw_bytes": 0,
                        "new_checkpoints": 0,
                    },
                    sort_keys=True,
                )
            )
            return 0
        if manifest.state not in STATES_RESUMABLE:
            raise ValueError("this checkpoint transaction is not resumable")
        source = _source_check(str(manifest.identity["source_path"]))
        parent_path = manifest.identity.get("parent_path")
        parent = Path(parent_path) if isinstance(parent_path, str) else None
        if parent is not None:
            parent_probe = _source_check(str(parent))
            source = replace(source, parent_uuid=parent_probe.uuid)
            recorded_name = manifest.identity.get("parent_backup_name")
            if (
                recorded_name is not None
                and _saved_parent_name(root, parent_probe.uuid) != recorded_name
            ):
                raise ValueError("incremental parent archive identity changed")
        result = _send_worker(
            root=root,
            name=args.name,
            manifest=manifest,
            source=source,
            parent=parent,
            state_dir=state,
            allow_local=args.allow_local,
            resume=True,
        )
    if result["status"] == "completed":
        _release_snapper_pins(state, manifest)
    print(json.dumps(result, sort_keys=True))
    return 0
