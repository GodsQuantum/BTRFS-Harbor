"""Machine-set retention safety: no parent and no incomplete point is pruned."""

from datetime import datetime, timedelta, timezone

import pytest

from btrfs_backup_ng.config import RetentionConfig
from btrfs_backup_ng.core.machine_retention_v2 import (
    SetPoint,
    calculate_set_retention,
)


BASE = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
POLICY = RetentionConfig(min="0d", keep=1)


def point(name: str, hours: int, *archives: str, complete: bool = True) -> SetPoint:
    return SetPoint(name, BASE + timedelta(hours=hours), archives, complete)


def plan(sets, parents, policy=POLICY):
    return calculate_set_retention(sets, parents, policy, now=BASE + timedelta(days=10))


def test_newest_incremental_protects_all_parent_archives_and_sets():
    sets = [point("A", 0, "root0"), point("B", 1, "root1"), point("C", 2, "root2")]
    graph = {"root0": None, "root1": "root0", "root2": "root1"}
    result = plan(sets, graph)
    assert result.keep == frozenset({"A", "B", "C"})
    assert result.protected_dependencies == frozenset({"A", "B"})
    assert result.eligible == frozenset()


def test_independent_new_full_chain_allows_prior_chain_retirement():
    sets = [
        point("A", 0, "old0"),
        point("B", 1, "old1"),
        point("C", 2, "old2"),
        point("D", 3, "new0"),
        point("E", 4, "new1"),
    ]
    graph = {
        "old0": None,
        "old1": "old0",
        "old2": "old1",
        "new0": None,
        "new1": "new0",
    }
    result = plan(sets, graph)
    assert result.keep == frozenset({"D", "E"})
    assert result.eligible == frozenset({"A", "B", "C"})


def test_retains_every_subvolume_of_an_incomplete_set_and_parents():
    sets = [
        point("A", 0, "root0", "home0"),
        point("B", 1, "root1", "home1", complete=False),
        point("C", 2, "root2", "home2"),
    ]
    parents = {
        "root0": None,
        "home0": None,
        "root1": "root0",
        "home1": "home0",
        "root2": "root1",
        "home2": "home1",
    }
    result = plan(sets, parents)
    assert result.keep == frozenset({"A", "B", "C"})
    assert not result.eligible


@pytest.mark.parametrize(
    "sets,graph",
    [
        ([point("A", 0, "a"), point("A", 1, "b")], {"a": None, "b": None}),
        ([point("A", 0, "a"), point("B", 1, "a")], {"a": None}),
        ([point("A", 0, "a")], {"a": "missing"}),
        ([point("A", 0, "a")], {"a": "a"}),
        ([point("A", 0, "a"), point("B", 1, "b")], {"a": "b", "b": "a"}),
        ([point("A", 0, "a")], {"a": None, "unmanaged": "a"}),
        ([point("A", 0)], {}),
    ],
)
def test_ambiguous_or_missing_chain_never_returns_a_deletion_plan(sets, graph):
    with pytest.raises(ValueError):
        plan(sets, graph)


def test_future_or_corrupt_time_kept_conservatively():
    sets = [point("old", 0, "a"), point("future", 9999, "b")]
    result = plan(sets, {"a": None, "b": None})
    assert "future" in result.keep


def test_timezone_required_and_empty_inventory_is_safe():
    assert plan([], {}).eligible == frozenset()
    with pytest.raises(ValueError):
        calculate_set_retention(
            [point("A", 0, "root")],
            {"root": None},
            POLICY,
            now=datetime.now(),
        )


def test_large_incremental_graph_preserves_every_parent_without_recursion():
    # Retention plans must handle thousands of independent durable backups.
    # A Python recursion error is neither a valid safety verdict nor a plan.
    count = 1500
    sets = [
        SetPoint(
            f"set-{index}",
            BASE + timedelta(seconds=index),
            (f"archive-{index}",),
            True,
        )
        for index in range(count)
    ]
    graph = {
        f"archive-{index}": f"archive-{index - 1}" if index else None
        for index in range(count)
    }
    decision = plan(sets, graph)
    assert decision.keep == frozenset(point.identifier for point in sets)
    assert decision.eligible == frozenset()


def test_large_incremental_cycle_is_explicitly_refused():
    count = 1500
    sets = [
        SetPoint(
            f"set-{index}",
            BASE + timedelta(seconds=index),
            (f"archive-{index}",),
            True,
        )
        for index in range(count)
    ]
    graph = {
        f"archive-{index}": f"archive-{index - 1}" if index else f"archive-{count - 1}"
        for index in range(count)
    }
    with pytest.raises(ValueError, match="cycle"):
        plan(sets, graph)
