"""Advisory, non-destructive Btrfs send-size and ETA forecasting.

The FIEMAP source measure describes allocated snapshot extents, NOT the
Btrfs send stream. The estimate is explicitly heuristic, never a quota
or a claim of a byte-perfect final transfer size.
"""

from __future__ import annotations

import math
import re
import statistics
import subprocess
from pathlib import Path

MAX_SOURCE_REFERENCE = 1 << 62


def estimate_source_bytes(path: Path, *, timeout: float = 4) -> int | None:
    """Read-only FIEMAP summary once; no quota enable/rescan, no du fallback.

    A time limit keeps source probing advisory, not an obstacle to backups.
    Mount/subvolume scope and reflink accounting can overstate a send stream.
    """
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        return None
    try:
        output = subprocess.run(
            ["btrfs", "filesystem", "du", "-s", "--raw", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=min(6, max(0.1, timeout)),
        )
        if output.returncode == 0:
            # btrfs filesystem du: Total Exclusive Set shared Filename.
            for line in output.stdout.splitlines()[1:]:
                match = re.match(r"^\s*(\d+)\s+\d+\s+\d+\s+", line)
                if match:
                    total = int(match.group(1))
                    if 0 < total < MAX_SOURCE_REFERENCE:
                        return total
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    # No quota enabling or quota rescans. If ordinary quotas are already
    # available, use only the single subvolume-level referenced-size record.
    # Ambiguous or stale qgroups are rejected, never silently summed.
    try:
        output = subprocess.run(
            ["btrfs", "qgroup", "show", "--raw", "-f", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if output.returncode == 0:
            matches = [
                int(row.group(1))
                for line in output.stdout.splitlines()
                if (row := re.match(r"^\s*0/\d+\s+(\d+)\s+\d+", line))
            ]
            if len(matches) == 1 and 0 < matches[0] < MAX_SOURCE_REFERENCE:
                return matches[0]
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return None


def forecast_machine(
    members: list[dict],
    sizes: dict[str, int],
    *,
    rate_bps: float | int | None,
) -> dict[str, object]:
    """Return coarse compressed total and ETA, never a false exact percentage.

    Recalibrate expected compressed/source ratio using completed volumes.
    Missing source measures are imputed with broad uncertainty and marked LOW.
    Completed volumes use real recorded bytes, not estimated sizes.
    """
    fields: dict[str, object] = {
        "estimated_total_bytes": None,
        "estimated_min_bytes": None,
        "estimated_max_bytes": None,
        "estimated_percent": None,
        "eta_seconds": None,
        "estimate_quality": "unavailable",
        "estimate_method": None,
        "estimate_covered_members": 0,
    }
    if not members:
        return fields
    references = {
        m["archive"]: v
        for m in members
        if type(v := m.get("source_reference_bytes")) is int
        and 0 < v < MAX_SOURCE_REFERENCE
    }
    fields["estimate_covered_members"] = len(references)
    valid_sizes = {
        k: v
        for k, v in sizes.items()
        if type(v) is int and 0 <= v < MAX_SOURCE_REFERENCE
    }
    all_done = all(m.get("status") == "completed" for m in members)
    completed = [m for m in members if m.get("status") == "completed"]
    if all_done:
        if len(valid_sizes) != len(members):
            return fields
        actual = sum(valid_sizes.get(m["archive"], 0) for m in members)
        fields.update(
            estimated_total_bytes=actual,
            estimated_min_bytes=actual,
            estimated_max_bytes=actual,
            estimated_percent=100,
            eta_seconds=0,
            estimate_quality="measured",
            estimate_method="destination-verified",
        )
        return fields
    if not references and not completed:
        return fields

    observed = sum(valid_sizes.get(m["archive"], 0) for m in members)
    calibrations = [
        (references[m["archive"]], valid_sizes[m["archive"]])
        for m in completed
        if m["archive"] in references
        and m["archive"] in valid_sizes
        and references[m["archive"]] >= 8 * 1024**2
    ]
    if calibrations:
        base = sum(reference for reference, _ in calibrations)
        ratio = min(1.8, max(0.2, sum(actual for _, actual in calibrations) / base))
    else:
        # Default 0.9 of FIEMAP extent bytes, calibrated once destinations seal.
        ratio = 0.9

    reference_values = list(references.values())
    complete_sizes = [
        valid_sizes[m["archive"]] for m in completed if m["archive"] in valid_sizes
    ]
    fallback = (
        statistics.median(reference_values)
        if reference_values
        else statistics.median(complete_sizes) / ratio
        if complete_sizes
        else 0
    )
    if not fallback:
        return fields

    center = 0.0
    min_bytes = 0.0
    max_bytes = 0.0
    missing = 0
    for member in members:
        name = member["archive"]
        measured = valid_sizes.get(name, 0)
        if member.get("status") == "completed":
            center += measured
            min_bytes += measured
            max_bytes += measured
            continue
        missing_member = name not in references
        if missing_member:
            missing += 1
        reference_size = float(references.get(name, fallback))
        projected = reference_size * ratio
        # Incomplete checkpoint bytes cannot exceed the final actual archive.
        projected = max(projected, measured * 1.08)
        floor = max(measured, projected * (0.5 if missing_member else 0.62))
        ceiling = max(projected * (2.0 if missing_member else 1.65), measured * 1.3)
        center += projected
        min_bytes += floor
        max_bytes += ceiling

    if not math.isfinite(center) or center <= 0 or center >= MAX_SOURCE_REFERENCE:
        return fields
    total = max(observed, round(center))
    floor = min(total, max(observed, round(min_bytes)))
    ceiling = max(total, round(max_bytes))
    percent = min(99.0, max(0.0, round(100 * observed / total, 1)))
    estimate_seconds = None
    if (
        rate_bps is not None
        and math.isfinite(rate_bps)
        and rate_bps > 0
        and total > observed
    ):
        estimate_seconds = min(
            100 * 365 * 24 * 3600, round((total - observed) / rate_bps)
        )
    fields.update(
        estimated_total_bytes=total,
        estimated_min_bytes=floor,
        estimated_max_bytes=ceiling,
        estimated_percent=percent,
        eta_seconds=estimate_seconds,
        estimate_quality="low" if missing or not calibrations else "calibrated",
        estimate_method="btrfs-extents+measured-compression"
        if calibrations
        else "btrfs-extents-heuristic",
    )
    return fields
