"""Native resource profiles, no mount changes or resident management services."""

import importlib
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    for name, folder in (
        ("btrfs_backup_ng", "src/btrfs_backup_ng"),
        ("btrfs_backup_ng.core", "src/btrfs_backup_ng/core"),
    ):
        m = types.ModuleType(name)
        m.__path__ = [str(ROOT / folder)]
        sys.modules[name] = m
profile = importlib.import_module("btrfs_backup_ng.core.performance_v2")


class PerformanceTest(unittest.TestCase):
    def test_balanced_uses_bounded_zstd_threads(self):
        r = profile.resolve_performance_profile("balanced")
        self.assertGreaterEqual(r.threads, 1)
        self.assertLessEqual(r.threads, 4)
        self.assertGreaterEqual(r.nice, 0)

    def test_fast_compresses_faster_without_disabling_guards(self):
        b = profile.resolve_performance_profile("balanced")
        f = profile.resolve_performance_profile("fast")
        self.assertLessEqual(f.level, b.level)
        self.assertGreaterEqual(f.threads, b.threads)

    def test_custom_bounds_and_no_superuser_assumption(self):
        result = profile.resolve_performance_profile(
            "custom", overrides={"threads": 3, "level": 6, "nice": 8, "ionice_class": 2}
        )
        self.assertEqual((result.threads, result.level, result.nice), (3, 6, 8))
        with self.assertRaises(ValueError):
            profile.resolve_performance_profile("custom", overrides={"threads": 65})
        with self.assertRaises(ValueError):
            profile.resolve_performance_profile("custom")

    def test_native_resource_calls_no_mount_tunings(self):
        commands = []
        profile.apply_resource_policy(
            345,
            profile.resolve_performance_profile("balanced"),
            run=lambda argv: commands.append(argv),
        )
        self.assertTrue(commands)
        self.assertFalse(any(any("mount" in arg for arg in cmd) for cmd in commands))


if __name__ == "__main__":
    unittest.main()
