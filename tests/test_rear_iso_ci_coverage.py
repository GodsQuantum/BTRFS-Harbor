"""A recovery code change must re-run the disposable ReaR ISO acceptance."""

from pathlib import Path

import pytest


WORKFLOW = (
    Path(__file__).resolve().parents[1] / ".github/workflows/rear-iso-acceptance.yml"
)

RECOVERY_INPUTS = (
    "packaging/rear/**",
    "packaging/build-engine-pyz.sh",
    "src/btrfs_backup_ng/core/rear_iso_v2.py",
    "src/btrfs_backup_ng/core/rear_bridge_v2.py",
    "src/btrfs_backup_ng/core/boot_files_v2.py",
    "src/btrfs_backup_ng/cli/machine_set_v2.py",
    "src/btrfs_backup_ng/cli/restore.py",
    "src/btrfs_backup_ng/endpoint/mount_guard_v2.py",
)


@pytest.mark.parametrize("path", RECOVERY_INPUTS)
def test_iso_workflow_reacts_to_recovery_changes(path: str) -> None:
    contents = WORKFLOW.read_text(encoding="utf-8")
    push_filters = contents.split("  workflow_dispatch:", 1)[0]
    assert f"      - {path}" in push_filters.splitlines()
