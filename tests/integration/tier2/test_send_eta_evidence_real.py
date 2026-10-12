"""Disposable real-Btrfs send/ETA calibration: never touch host source mounts."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import requires_btrfs

pytestmark = [pytest.mark.tier2, requires_btrfs]


def _run(*args: str) -> None:
    subprocess.run(["btrfs", *args], check=True, capture_output=True, timeout=90)


def _send(
    destination: Path,
    subvolume: Path,
    parent: Path | None = None,
    *,
    no_data: bool = False,
) -> tuple[int, int, float]:
    command = ["btrfs", "send"]
    if parent is not None:
        command += ["-p", str(parent)]
    if no_data:
        command += ["--no-data"]
    command.append(str(subvolume))
    start = time.monotonic()
    with destination.open("wb") as output:
        subprocess.run(
            command, stdout=output, check=True, stderr=subprocess.PIPE, timeout=90
        )
    elapsed = time.monotonic() - start
    raw = destination.read_bytes()
    compressed = len(
        subprocess.run(
            ["zstd", "-q", "-3", "-c"],
            input=raw,
            capture_output=True,
            check=True,
            timeout=30,
        ).stdout
    )
    return len(raw), compressed, elapsed


def test_full_incremental_zstd_stream_size_is_not_predictable_from_logical_bytes(
    btrfs_volume: Path, tmp_path: Path
) -> None:
    # Both files have identical logical size but incompatible compressibility.
    observed: dict[str, tuple[int, int, float]] = {}
    for kind, payload in (
        ("zeros", bytes(2 * 1024 * 1024)),
        ("random", os.urandom(2 * 1024 * 1024)),
    ):
        source = btrfs_volume / f"eta-source-{kind}"
        snap = btrfs_volume / f"eta-snapshot-{kind}"
        _run("subvolume", "create", str(source))
        (source / "same-logical-size.bin").write_bytes(payload)
        _run("subvolume", "snapshot", "-r", str(source), str(snap))
        observed[kind] = _send(tmp_path / f"{kind}.send", snap)
    assert observed["zeros"][0] > 2 * 1024 * 1024
    assert observed["random"][0] > 2 * 1024 * 1024
    assert observed["zeros"][1] * 4 < observed["random"][1]

    # Same source tree: one tiny CoW edit then a complete large overwrite.
    source = btrfs_volume / "eta-source-random"
    base = btrfs_volume / "eta-snapshot-random"
    tiny = btrfs_volume / "eta-snapshot-tiny"
    large = btrfs_volume / "eta-snapshot-large"
    with (source / "same-logical-size.bin").open("r+b") as output:
        output.write(b"z")
    _run("subvolume", "snapshot", "-r", str(source), str(tiny))
    small_delta = _send(tmp_path / "tiny.incremental.send", tiny, base)
    (source / "same-logical-size.bin").write_bytes(os.urandom(2 * 1024 * 1024))
    _run("subvolume", "snapshot", "-r", str(source), str(large))
    big_delta = _send(tmp_path / "large.incremental.send", large, tiny)
    metadata_only = _send(
        tmp_path / "nodata.incremental.send", large, tiny, no_data=True
    )
    assert small_delta[0] < big_delta[0]
    assert metadata_only[0] < big_delta[0]
    print(
        "HARBOR_REAL_BTRFS_ETA_EVIDENCE",
        {
            "full": observed,
            "incremental_tiny": small_delta,
            "incremental_large": big_delta,
            "no_data": metadata_only,
        },
    )
