"""Snapper-backed source selection by immutable Btrfs UUID.

Btrfs Assistant consumes the same native Snapper snapshots; no special adapter.
A lone pre snapshot is only selectable explicitly, never as automatic latest.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Protocol

DEFAULT_SETTLE_SECONDS = 30


class SnapshotCandidate(Protocol):
    @property
    def config_name(self) -> str: ...

    @property
    def number(self) -> int: ...

    @property
    def snapshot_type(self) -> str: ...

    @property
    def date(self) -> datetime: ...

    @property
    def pre_num(self) -> int | None: ...


@dataclass(frozen=True)
class ResolvedSource:
    uuid: str
    readonly: bool
    exists: bool


@dataclass(frozen=True)
class SourceDecision:
    action: str
    snapshot: SnapshotCandidate | None
    source_uuid: str | None


def resolve_source_identity(snapshot: SnapshotCandidate) -> ResolvedSource:
    """Resolve true source send identity, refusing missing/RW Btrfs subvolumes."""
    path = getattr(snapshot, "subvolume_path", None)
    if not isinstance(path, Path) or not path.is_dir() or path.is_symlink():
        return ResolvedSource("", False, False)
    subvol = subprocess.run(
        ["btrfs", "subvolume", "show", str(path)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    ro = subprocess.run(
        ["btrfs", "property", "get", "-ts", str(path), "ro"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if subvol.returncode != 0 or ro.returncode != 0:
        return ResolvedSource("", False, False)
    fields = dict(
        (m.group(1).strip(), m.group(2).strip())
        for line in subvol.stdout.splitlines()
        if (m := re.match(r"^\s*([^:]+):\s*(.*)$", line))
    )
    received = fields.get("Received UUID", "")
    source_uuid = received if received and received != "-" else fields.get("UUID", "")
    return ResolvedSource(source_uuid, "ro=true" in ro.stdout, True)


def _aware(when: datetime) -> datetime:
    # Snapper metadata parser yields naive LOCAL time; astimezone() correctly
    # attaches the system local timezone before converting to UTC.
    return when.astimezone(timezone.utc)


def select_source(
    snapshots: Iterable[SnapshotCandidate],
    *,
    mode: Literal["latest", "selected", "create"],
    resolve: Callable[[SnapshotCandidate], ResolvedSource] = resolve_source_identity,
    remote_uuids: set[str] | None = None,
    now: datetime | None = None,
    selected_number: int | None = None,
    selected_config: str | None = None,
    settle_seconds: int = DEFAULT_SETTLE_SECONDS,
) -> SourceDecision | None:
    if mode == "create":
        return SourceDecision("create_snapshot", None, None)
    if mode not in ("latest", "selected"):
        raise ValueError(f"unsupported snapshot policy {mode}")
    if settle_seconds < 0:
        raise ValueError("invalid snapshot settle interval")
    now = _aware(now or datetime.now(timezone.utc))
    collection = list(snapshots)
    known_pre = {
        (item.config_name, item.number)
        for item in collection
        if item.snapshot_type == "pre"
    }
    candidates = sorted(
        collection, key=lambda item: (_aware(item.date), item.number), reverse=True
    )
    remote_uuids = remote_uuids or set()
    for item in candidates:
        if mode == "selected":
            if item.number != selected_number or (
                selected_config and item.config_name != selected_config
            ):
                continue
        else:
            if item.snapshot_type == "pre":
                continue
            if item.snapshot_type == "post":
                if (item.config_name, item.pre_num) not in known_pre:
                    continue
            elif item.snapshot_type == "single":
                if (now - _aware(item.date)).total_seconds() < settle_seconds:
                    continue
            else:
                continue
        resolved = resolve(item)
        if not resolved.exists or not resolved.readonly or not resolved.uuid:
            if mode == "selected":
                raise ValueError(
                    "selected snapshot is missing, not readonly, or has no UUID"
                )
            continue
        if resolved.uuid in remote_uuids:
            continue
        return SourceDecision("send_existing", item, resolved.uuid)
    if mode == "selected":
        raise ValueError("requested Snapper snapshot was not eligible or found")
    return None
