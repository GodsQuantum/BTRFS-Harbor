"""Native, non-resident resource policy for checkpointed Btrfs Harbor v2."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class PerformanceSettings:
    level: int
    threads: int
    nice: int
    ionice_class: int


def resolve_performance_profile(
    name: str, overrides: dict[str, int] | None = None
) -> PerformanceSettings:
    cpus = min(max(1, os.cpu_count() or 1), 16)
    if name == "balanced":
        values = {"level": 3, "threads": min(cpus, 2), "nice": 8, "ionice_class": 2}
    elif name == "fast":
        values = {"level": 1, "threads": min(cpus, 4), "nice": 2, "ionice_class": 2}
    elif name == "custom":
        if not overrides:
            raise ValueError("custom performance profile requires settings")
        values = {"level": 3, "threads": 2, "nice": 8, "ionice_class": 2}
    else:
        raise ValueError("unknown performance profile")
    if overrides:
        if name != "custom":
            raise ValueError("only custom profile accepts overrides")
        if overrides.keys() - values.keys():
            raise ValueError("unknown performance settings")
        values.update(overrides)
    if any(type(value) is not int for value in values.values()):
        raise ValueError("performance settings must be integers")
    if not (
        -7 <= values["level"] <= 22
        and 1 <= values["threads"] <= 16
        and 0 <= values["nice"] <= 19
        and values["ionice_class"] in (2, 3)
    ):
        raise ValueError("performance settings exceed safe bounds")
    return PerformanceSettings(**values)


def apply_resource_policy(
    pid: int,
    settings: PerformanceSettings,
    *,
    run: Callable[[list[str]], object] | None = None,
) -> list[str]:
    """Use existing Linux renice/ionice; no cgroup or mount modifications.

    If host utilities are absent or rights insufficient, fall back without
    blocking backup. The caller sees warnings; this does not claim a limit
    that could not be enforced.
    """
    if type(pid) is not int or pid <= 1:
        raise ValueError("invalid worker PID")
    commands = [
        ["renice", "-n", str(settings.nice), "-p", str(pid)],
        ["ionice", "-c", str(settings.ionice_class), "-n", "6", "-p", str(pid)],
    ]
    errors: list[str] = []
    for cmd in commands:
        if run is not None:
            run(cmd)
            continue
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{cmd[0]}: {exc}")
            continue
        if result.returncode != 0:
            errors.append(f"{cmd[0]}: {result.stderr.strip()[:240]}")
    return errors
