"""Btrfs-native readonly snapshot adapter for hosts without Snapper.

No modifications to mounts or existing Snapper configs. This provider only
creates inside a verified subvolume it owns, and never deletes an older base:
retention needs to account for completed incrementals and partial resumes.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .. import __util__
from .native_send_v2 import inspect_readonly_btrfs_source

NATIVE_FOLDER = ".btrfs-harbor-snapshots"
# Legacy names without a fractional timestamp remain readable.
NAME_PATTERN = re.compile(r"^harbor-(\d{8}T\d{6})(?:\.(\d{9}))?Z-([0-9a-f]{12})$")
MAX_NATIVE_SNAPSHOTS = 8192


@dataclass(frozen=True)
class NativeSnapshot:
    name: str
    path: Path
    uuid: str
    date: str


def native_snapshot_sort_key(name: str) -> tuple[str, int]:
    """Sort by actual timestamp, not a random UUID suffix in the same second."""
    match = NAME_PATTERN.fullmatch(name)
    if match is None:
        raise ValueError("invalid native snapshot name")
    return match.group(1), int(match.group(2) or "0")


def _subvolume_root(source: Path) -> Path:
    if not source.is_absolute():
        raise ValueError("source must be an absolute real Btrfs subvolume")
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source must be an existing, non-symlink Btrfs directory")
    result = subprocess.run(
        ["btrfs", "subvolume", "show", str(source)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError("source is not a Btrfs subvolume root")
    return source


def _snapshot_directory(source: Path, *, create: bool) -> Path:
    root = _subvolume_root(source)
    directory = root / NATIVE_FOLDER
    if directory.is_symlink():
        raise ValueError("refusing symlinked native snapshot directory")
    if create and not directory.exists():
        directory = __util__.create_below(root, NATIVE_FOLDER, mode=0o700)
    if not directory.is_dir():
        if create:
            raise ValueError("native snapshot location is not a real directory")
        return directory
    if directory.is_symlink() or directory.stat().st_dev != root.stat().st_dev:
        raise ValueError("native snapshots must remain on their source Btrfs subvolume")
    return directory


def list_native_snapshots(source: Path) -> list[NativeSnapshot]:
    directory = _snapshot_directory(source, create=False)
    if not directory.exists():
        return []
    found: list[NativeSnapshot] = []
    names = list(directory.iterdir())
    if len(names) > MAX_NATIVE_SNAPSHOTS:
        raise ValueError("too many native snapshots; manually review retention")
    for path in names:
        match = NAME_PATTERN.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_dir():
            continue
        try:
            details = inspect_readonly_btrfs_source(path)
            moment = dt.datetime.strptime(match.group(1), "%Y%m%dT%H%M%S").replace(
                tzinfo=dt.timezone.utc
            )
            moment += dt.timedelta(
                microseconds=int((match.group(2) or "000000000")[:6])
            )
        except (ValueError, OSError):
            continue
        found.append(NativeSnapshot(path.name, path, details.uuid, moment.isoformat()))
    found.sort(key=lambda item: native_snapshot_sort_key(item.name), reverse=True)
    return found


def find_native_snapshot(source: Path, name: str) -> NativeSnapshot:
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("invalid native snapshot identifier")
    found = next(
        (item for item in list_native_snapshots(source) if item.name == name), None
    )
    if found is None:
        raise ValueError("selected readonly native Btrfs snapshot is unavailable")
    return found


def create_native_snapshot(source: Path) -> NativeSnapshot:
    directory = _snapshot_directory(source, create=True)
    epoch_ns = time.time_ns()
    stamp = dt.datetime.fromtimestamp(
        epoch_ns // 1_000_000_000, tz=dt.timezone.utc
    ).strftime("%Y%m%dT%H%M%S")
    name = f"harbor-{stamp}.{epoch_ns % 1_000_000_000:09d}Z-{uuid.uuid4().hex[:12]}"
    target = directory / name
    if target.exists() or target.is_symlink():
        raise ValueError("native snapshot already exists")
    result = subprocess.run(
        ["btrfs", "subvolume", "snapshot", "-r", str(source), str(target)],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "Could not create readonly Btrfs snapshot: " + result.stderr.strip()[:250]
        )
    # A failed inspection leaves the snapshot available for manual inspection;
    # never delete the only local source of a crash-resumable backup.
    return find_native_snapshot(source, name)
