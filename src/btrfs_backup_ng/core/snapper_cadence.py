"""Independent source snapshot and remote send cadence planning.

A pending transfer remains pinned to its originally selected source. Daily
Latest does not walk historical unsent Snapper snapshots on each send.
Scheduling is decided here; native systemd timers or explicit caller events
are the scheduler, not a new resident service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Iterable, Literal

from ..snapper.source_policy import (
    ResolvedSource,
    SnapshotCandidate,
    SourceDecision,
    select_source,
)


@dataclass(frozen=True)
class PendingTransfer:
    transfer_id: str
    source_uuid: str
    status: str


@dataclass(frozen=True)
class SendDecision:
    action: str
    source: SourceDecision | None = None
    pending_transfer_id: str | None = None


def _elapsed_due(cadence: str, now: datetime, latest: datetime | None) -> bool:
    if cadence in ("manual", "custom"):
        return False
    if latest is None:
        return True
    latest = latest.astimezone(now.tzinfo)
    if cadence == "hourly":
        return now - latest >= timedelta(hours=1)
    if cadence == "daily":
        return latest.date() != now.date()
    if cadence == "weekly":
        return latest.isocalendar()[:2] != now.isocalendar()[:2]
    return False


def plan_remote_send(
    *,
    snapshots: Iterable[SnapshotCandidate],
    source_mode: Literal["create", "latest", "selected"],
    resolve: Callable[[SnapshotCandidate], ResolvedSource],
    cadence: Literal["manual", "hourly", "daily", "weekly", "custom", "after_snapshot"],
    now: datetime,
    pending: list[PendingTransfer],
    sent: list[datetime],
    remote_uuids: set[str],
    trigger: str,
    selected_number: int | None = None,
    selected_config: str | None = None,
) -> SendDecision:
    if now.tzinfo is None:
        raise ValueError("remote cadence requires timezone-aware datetime")
    if cadence not in {
        "manual",
        "hourly",
        "daily",
        "weekly",
        "custom",
        "after_snapshot",
    }:
        raise ValueError("unknown send cadence")
    resumable = {
        "preparing",
        "uploading",
        "replaying",
        "paused",
        "pause_requested",
        "failed_resumable",
        "finalizing",
    }
    unfinished = next((job for job in pending if job.status in resumable), None)
    if unfinished:
        return SendDecision("resume_first", pending_transfer_id=unfinished.transfer_id)
    if cadence in ("manual", "custom") and trigger not in ("manual", "custom"):
        return SendDecision("not_due")
    if cadence == "after_snapshot":
        if trigger != "snapshot":
            return SendDecision("not_due")
    elif cadence not in ("manual", "custom") and not _elapsed_due(
        cadence, now, max(sent) if sent else None
    ):
        return SendDecision("not_due")
    chosen = select_source(
        snapshots,
        mode=source_mode,
        resolve=resolve,
        remote_uuids=set(),  # first determine only NEWEST, do not backfill older
        now=now,
        selected_number=selected_number,
        selected_config=selected_config,
    )
    if chosen is None or (
        chosen.source_uuid is not None and chosen.source_uuid in remote_uuids
    ):
        return SendDecision("no_snapshot")
    return SendDecision("start", chosen)
