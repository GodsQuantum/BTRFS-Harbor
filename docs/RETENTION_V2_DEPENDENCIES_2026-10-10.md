# Retention v2 — dependency-safe preview (2026-10-10)

The established retention algorithm determines which complete Btrfs
machine-set restore points match the date/count policy. A new planner
protects every transitive incremental parent. Incomplete sets are protected.

Read-only command:

    btrfs-backup-ng raw checkpoint-v2 set-retention-plan --target /destination --profile-id UUID --keep 7 --min 1d --allow-local --experimental

JSON output contains keep_sets, protected_incremental_sets,
retention_eligible_sets and deletion_performed:false. No files are deleted.
Unknown archives, missing or cyclic parent chains, mixed profiles,
incomplete metadata or ambiguous identity make planning fail closed.

IMPORTANT: This is NOT automatic retention. A daily incremental chain
cannot discard its old full anchor while keeping children. An automatic
collector needs independently verified periodic full anchors, durable
tombstones, catalog updates, and destructive fault tests concurrent with
a restore. Legacy raw prune cannot simply delete grouped Btrfs streams
without invalidating the portable group catalog.

Keep the v2 scheduling UI warning until those acceptance gates pass.
