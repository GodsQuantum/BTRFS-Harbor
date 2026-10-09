"""Dependency-safe retention PLAN for whole Btrfs machine sets.

No deletion, no scanning user subvolumes, no mutable operations. The existing
retention policy selects set heads. All raw parents of those sets are then
closed transitively; all sets containing those parents are preserved. If the
index is incomplete or any parent is ambiguous, refuse the plan altogether.

Automatic deletion requires a separate atomic tombstone-and-GC engine and
fresh-mount test. Never call unlink from this planner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import RetentionConfig
from ..retention import apply_retention


@dataclass(frozen=True)
class SetPoint:
    identifier: str
    created: datetime
    archives: tuple[str, ...]
    complete: bool


@dataclass(frozen=True)
class SetRetentionPlan:
    keep: frozenset[str]
    protected_dependencies: frozenset[str]
    eligible: frozenset[str]
    kept_archives: frozenset[str]


def calculate_set_retention(
    sets: list[SetPoint],
    parents: dict[str, str | None],
    policy: RetentionConfig,
    *,
    now: datetime,
) -> SetRetentionPlan:
    """Produce an auditable whole-set plan without modifying source or target.

    Note: a deleted set cannot be recovered, even if its parent happens to
    survive. We only mark sets eligible if every archive in that set has no
    retained descendant and all machine-set catalogs were inventoried.
    """
    if now.tzinfo is None:
        raise ValueError("retention clock must carry UTC timezone")
    if not sets:
        return SetRetentionPlan(frozenset(), frozenset(), frozenset(), frozenset())
    identifiers: set[str] = set()
    owners: dict[str, str] = {}
    by_id: dict[str, SetPoint] = {}
    for point in sets:
        if point.identifier in identifiers or not point.archives:
            raise ValueError("duplicate/empty backup set")
        identifiers.add(point.identifier)
        by_id[point.identifier] = point
        if point.created.tzinfo is None:
            raise ValueError("set creation time must carry UTC timezone")
        for name in point.archives:
            if name in owners or name not in parents:
                raise ValueError("duplicate or unindexed backup archive")
            owners[name] = point.identifier
    # A foreign/unindexed backup might refer to one of these parents:
    # do not prune without a complete identity model for all stored archives.
    if set(parents) != set(owners):
        raise ValueError("backup target has unmanaged/unknown dependent archives")

    visiting: set[str] = set()
    visited: set[str] = set()

    def validate_parent(name: str) -> None:
        if name in visiting:
            raise ValueError("cycle in Btrfs incremental parent chain")
        if name in visited:
            return
        visiting.add(name)
        parent = parents[name]
        if parent is not None:
            if parent not in owners or parent == name:
                raise ValueError("missing incremental parent archive")
            validate_parent(parent)
        visiting.remove(name)
        visited.add(name)

    for archive in owners:
        validate_parent(archive)

    incomplete = {point.identifier for point in sets if not point.complete}
    complete = [point for point in sets if point.complete]
    # Use the single long-established retention engine, with UTC dates made
    # naive because its time bucket implementation uses naive datetime.now().
    utc_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    selected, _ = apply_retention(
        complete,
        policy,
        now=utc_now,
        get_name=lambda item: item.identifier,
        get_timestamp=lambda item: item.created.astimezone(timezone.utc).replace(
            tzinfo=None
        ),
    )
    mandatory = {item.identifier for item in selected} | incomplete
    kept_names: set[str] = set()

    def protect_parents(name: str) -> None:
        if name in kept_names:
            return
        kept_names.add(name)
        parent = parents[name]
        if parent is not None:
            protect_parents(parent)

    for identifier in mandatory:
        for name in by_id[identifier].archives:
            protect_parents(name)

    protected = {owners[name] for name in kept_names} - mandatory
    keep = mandatory | protected
    return SetRetentionPlan(
        keep=frozenset(keep),
        protected_dependencies=frozenset(protected),
        eligible=frozenset(identifiers - keep),
        kept_archives=frozenset(kept_names),
    )
