"""Offline tests for the backup cadence (no network, no database).

C13 (item 3.3): the schedule is daily and rotation keeps only the newest
snapshot. The DB-touching parts of `backups.py` are not exercised here — only
the pure cadence/rotation logic, with BACKUP_ROOT redirected to a temp dir.

Run them either way:
    python tests/test_backups.py
    pytest tests/test_backups.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import backups  # noqa: E402


class BackupCadenceTests(unittest.TestCase):
    """C13 (item 3.3): daily cadence, keep only the newest snapshot."""

    def test_daily_interval_and_keep_one(self):
        """Defaults are daily (1) with a single retained set (1)."""
        saved = {k: os.environ.pop(k, None)
                 for k in ("BACKUP_INTERVAL_DAYS", "BACKUP_KEEP")}
        try:
            self.assertEqual(backups.interval_days(), 1)
            self.assertEqual(backups.keep_sets(), 1)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_rotate_keeps_only_the_newest_set(self):
        """`rotate()` over two fake sets leaves exactly the newest one."""
        old = backups.BACKUP_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            backups.BACKUP_ROOT = tmp
            try:
                for name in ("20260101-000000Z", "20260102-000000Z"):
                    d = os.path.join(tmp, name)
                    os.makedirs(d)
                    with open(os.path.join(d, "manifest.json"), "w",
                              encoding="utf-8") as fh:
                        json.dump({"name": name,
                                   "created_utc": "2026-01-01T00:00:00+00:00"}, fh)
                kept = backups.rotate()
                self.assertEqual(kept, 1)
                self.assertEqual([s["name"] for s in backups.list_sets()],
                                 ["20260102-000000Z"])
            finally:
                backups.BACKUP_ROOT = old


if __name__ == "__main__":
    unittest.main()
