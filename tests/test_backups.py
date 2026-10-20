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


class SourceSnapshotTests(unittest.TestCase):
    """B9 — the source tree rides along in every set, with the same rotation."""

    def test_snapshot_copies_the_tree_and_counts_bytes(self):
        with tempfile.TemporaryDirectory() as root, \
                tempfile.TemporaryDirectory() as out:
            os.makedirs(os.path.join(root, "a"))
            os.makedirs(os.path.join(root, "b"))
            sizes = 0
            for rel in ("a/one.pdf", "b/two.pdf"):
                with open(os.path.join(root, rel), "wb") as fh:
                    fh.write(b"%PDF-1.4 " + rel.encode())
                sizes += os.path.getsize(os.path.join(root, rel))
            block = backups.snapshot_sources(out, root)
            self.assertEqual(block["files"], 2)
            self.assertEqual(block["bytes"], sizes)
            self.assertTrue(os.path.isfile(
                os.path.join(out, "sources", "a", "one.pdf")))

    def test_snapshot_of_a_missing_root_reports_zero(self):
        with tempfile.TemporaryDirectory() as out:
            block = backups.snapshot_sources(out, "")
            self.assertEqual((block["files"], block["bytes"]), (0, 0))

    def test_rotation_removes_the_old_set_with_its_sources(self):
        old_root = backups.BACKUP_ROOT
        old_keep = os.environ.pop("BACKUP_KEEP", None)
        with tempfile.TemporaryDirectory() as tmp:
            backups.BACKUP_ROOT = tmp
            try:
                for name in ("20260101-000000Z", "20260102-000000Z"):
                    path = os.path.join(tmp, name)
                    os.makedirs(os.path.join(path, "sources"))
                    with open(os.path.join(path, "manifest.json"), "w",
                              encoding="utf-8") as fh:
                        json.dump({"name": name,
                                   "created_utc": "2026-01-01T00:00:00+00:00"},
                                  fh)
                backups.rotate()
                self.assertFalse(os.path.exists(
                    os.path.join(tmp, "20260101-000000Z")))
                self.assertTrue(os.path.isdir(os.path.join(
                    tmp, "20260102-000000Z", "sources")))
            finally:
                backups.BACKUP_ROOT = old_root
                if old_keep is not None:
                    os.environ["BACKUP_KEEP"] = old_keep


class TableListTests(unittest.TestCase):
    """C1d: one allowlist drives the backup job, the console and its guard."""

    LESSONS = ("courses", "chapters", "lessons", "lesson_blocks", "media_assets",
               "questions", "question_options", "short_answer_keys", "attempts",
               "lesson_progress")
    OCR = ("sources", "answers", "revision_reports", "revision_summary")

    def test_tables_cover_the_lessons_model(self):
        for table in self.LESSONS + self.OCR:
            self.assertIn(table, backups.TABLES)
        self.assertEqual(backups.TS_COL["revision_summary"], "last_at")
        self.assertEqual(backups.TS_COL["questions"], "created_at")

    def test_the_console_shares_the_same_allowlist(self):
        from pipeline.dataconsole import TABLES as console_tables
        self.assertEqual(console_tables, backups.TABLES)

    def test_guard_message_is_not_hard_coded_to_five(self):
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "pipeline", "dataconsole.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn("five pipeline tables", src)
        self.assertIn("Only the pipeline tables are reachable here", src)


if __name__ == "__main__":
    unittest.main()
