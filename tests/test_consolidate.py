"""Schema-contract tests for the flat core (docs/migrations/consolidate.sql).

The app's database is the flat pipeline model: ``sources`` / ``questions`` /
``answers`` / ``revision_reports`` / ``revision_summary``. The lessons model
(courses/chapters/lessons/lesson_blocks/... and the Sapienstech mirror) is
retired; ``consolidate.py`` proves the lessons tables duplicate those rows and
then drops them in one transaction.

These tests parse the SQL text and exercise the pure helpers only: no
database, no network, nothing is executed.

Run them either way:
    python tests/test_consolidate.py
    pytest tests/test_consolidate.py        (if pytest is installed)
"""
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SQL_PATH = os.path.join(ROOT, "docs", "migrations", "consolidate.sql")

CORE = ("sources", "questions", "answers")
RETIRED = ("courses", "chapters", "lessons", "lesson_blocks", "media_assets",
           "question_options", "short_answer_keys", "attempts",
           "lesson_progress", "tenants", "domains", "users", "tests",
           "students", "questions_sapienstech", "test_questions")
REVISION = ("revision_reports", "revision_summary")


def read_sql(path=SQL_PATH):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


class SchemaShapeTests(unittest.TestCase):
    """consolidate.sql builds the five-table core and drops the rest."""

    def setUp(self):
        self.sql = read_sql()

    def test_no_bom_at_the_start(self):
        """A BOM makes Postgres reject the whole script ('syntax error ... ﻿')."""
        with open(SQL_PATH, "rb") as fh:
            self.assertFalse(fh.read(3) == b"\xef\xbb\xbf")

    def test_every_core_table_can_be_created(self):
        for table in CORE:
            self.assertIn("CREATE TABLE IF NOT EXISTS %s" % table, self.sql)

    def test_every_lessons_table_is_dropped(self):
        for table in RETIRED:
            self.assertIn("DROP TABLE IF EXISTS public.%s;" % table, self.sql)

    def test_the_drop_is_the_last_block(self):
        """Drops must follow every check: a failed check must precede them."""
        drop = self.sql.index("DROP TABLE IF EXISTS public.test_questions")
        parity = self.sql.index("$parity$")
        self.assertLess(parity, drop)
        self.assertNotIn("CREATE TABLE", self.sql[drop:])

    def test_the_mirror_goes_before_chapters_it_references(self):
        order = [self.sql.index("DROP TABLE IF EXISTS public.%s;" % t)
                 for t in ("test_questions", "questions_sapienstech",
                           "lessons", "chapters", "courses", "users",
                           "domains", "tenants")]
        self.assertEqual(order, sorted(order))

    def test_sapienstech_id_is_dropped_before_the_mirror_table(self):
        col = self.sql.index("DROP COLUMN IF EXISTS sapienstech_id")
        mirror = self.sql.index("DROP TABLE IF EXISTS public.questions_sapienstech")
        self.assertLess(col, mirror)

    def test_it_is_never_destructive_to_the_core_rows(self):
        for table in CORE:
            self.assertNotIn("DROP TABLE IF EXISTS public.%s;" % table, self.sql)
            self.assertNotIn("DELETE FROM public.%s" % table, self.sql)
            self.assertNotIn("TRUNCATE", self.sql)

    def test_the_single_bind_placeholder_is_present_and_pct_safe(self):
        """psycopg2 substitutes one %s; any other bare % breaks the execute."""
        bare = [m.start() for m in re.finditer(r"(?<!%)%(?!%|s)", self.sql)]
        self.assertEqual(bare, [])
        self.assertEqual(len(re.findall(r"(?<!%)%s", self.sql)), 1)

    def test_legacy_id_survives_because_it_names_the_crops(self):
        self.assertNotIn("DROP COLUMN IF EXISTS legacy_id", self.sql)

    def test_answers_keep_their_identity_columns_for_unpaired_matching(self):
        flat = squash(self.sql)
        for column in ("subject", "topic", "grade", "corp", "year",
                       "difficulty", "tags"):
            self.assertIn("ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS %s"
                          % column, flat)
        self.assertIn("answers_identity_pair_idx", flat)

    def test_the_backfill_only_fills_nulls(self):
        """An existing answer value is never overwritten by the host question's."""
        flat = squash(self.sql)
        self.assertIn("COALESCE(a.subject, q.subject)", flat)
        self.assertIn("COALESCE(a.topic, q.topic)", flat)

    def test_the_dormant_revision_columns_are_kept(self):
        """A revival must not need a migration: questions/answers keep them."""
        for column in ("revision_status", "revision_notes", "revised_at"):
            self.assertNotIn("DROP COLUMN IF EXISTS %s" % column, self.sql)

    def test_the_retired_revision_tables_are_not_created_here(self):
        """They are dropped by retire_revision.sql, not re-created here."""
        for table in REVISION:
            self.assertNotIn("CREATE TABLE IF NOT EXISTS %s" % table, self.sql)
            self.assertNotIn("DROP TABLE IF EXISTS public.%s;" % table, self.sql)


class RetireRevisionTests(unittest.TestCase):
    """retire_revision.sql is the focused, idempotent revision-table drop."""

    RETIRE_PATH = os.path.join(ROOT, "docs", "migrations", "retire_revision.sql")

    def setUp(self):
        self.sql = read_sql(self.RETIRE_PATH)

    def test_both_revision_tables_are_dropped_idempotently(self):
        for table in REVISION:
            self.assertIn("DROP TABLE IF EXISTS public.%s;" % table, self.sql)

    def test_it_never_touches_a_live_table(self):
        for table in CORE:
            self.assertNotIn("DROP TABLE IF EXISTS public.%s;" % table, self.sql)
            self.assertNotIn("DELETE FROM public.%s" % table, self.sql)

    def test_it_takes_no_bind_placeholders(self):
        """No % substitution happens, so a bare % (a NOTICE) is safe here."""
        self.assertNotIn("%s", self.sql)
        with open(self.RETIRE_PATH, "rb") as fh:
            self.assertNotEqual(fh.read(3), b"\xef\xbb\xbf")

    def test_the_runner_owns_the_transaction(self):
        with open(os.path.join(ROOT, "pipeline", "migrations", "consolidate.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("def retire_revision"):src.index("def verify")]
        self.assertNotIn("commit()", body)


class SafetyTests(unittest.TestCase):
    """The runner refuses to drop without a complete backup and clean checks."""

    def setUp(self):
        from pipeline.migrations import consolidate
        self.mod = consolidate

    def _set(self, root, name, tables, sources_files=0):
        path = os.path.join(root, name)
        os.makedirs(path)
        manifest = {"name": name, "tables": {t: {"rows": 1} for t in tables},
                    "total_rows": len(tables)}
        if sources_files:
            manifest["sources"] = {"dir": "sources", "files": sources_files}
            os.makedirs(os.path.join(path, "sources"))
            open(os.path.join(path, "sources", "a.txt"), "w").close()
        for t in tables:
            open(os.path.join(path, t + ".json"), "w").close()
            open(os.path.join(path, t + ".sql"), "w").close()
        with open(os.path.join(path, "manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh)

    def test_a_set_without_every_table_file_is_not_complete(self):
        with tempfile.TemporaryDirectory() as root:
            self._set(root, "20260101-000000Z", ["sources", "questions"])
            os.remove(os.path.join(root, "20260101-000000Z", "questions.sql"))
            self.assertIsNone(self.mod.complete_backup(root))

    def test_an_empty_directory_is_not_a_backup(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "20260101-000000Z"))
            self.assertIsNone(self.mod.complete_backup(root))

    def test_a_complete_set_is_found(self):
        with tempfile.TemporaryDirectory() as root:
            self._set(root, "20260101-000000Z", CORE, sources_files=2)
            found = self.mod.complete_backup(root)
            self.assertIsNotNone(found)
            self.assertEqual(found["dir"], "20260101-000000Z")

    def test_a_missing_source_snapshot_disqualifies_the_set(self):
        with tempfile.TemporaryDirectory() as root:
            self._set(root, "20260101-000000Z", CORE, sources_files=2)
            import shutil
            shutil.rmtree(os.path.join(root, "20260101-000000Z", "sources"))
            self.assertIsNone(self.mod.complete_backup(root))

    def test_the_newest_complete_set_wins(self):
        with tempfile.TemporaryDirectory() as root:
            self._set(root, "20260101-000000Z", CORE)
            self._set(root, "20260102-000000Z", CORE)
            self.assertEqual(self.mod.complete_backup(root)["dir"],
                             "20260102-000000Z")

    def test_parity_payload_only_names_measured_tables(self):
        facts = {"tables": {"sources": 5, "questions": 7, "answers": 0,
                            "revision_reports": 3, "revision_summary": 1,
                            "lessons": 5, "lesson_blocks": 7, "courses": 2}}
        self.assertEqual(self.mod.parity_payload(facts),
                         {"sources": 5, "questions": 7,
                          "lessons": 5, "lesson_blocks": 7})

    def test_the_core_and_retired_lists_match_the_sql(self):
        self.assertEqual(tuple(self.mod.CORE_TABLES), CORE)
        for table in self.mod.LESSON_TABLES + self.mod.MIRROR_TABLES:
            self.assertIn(table, RETIRED)
        self.assertEqual(tuple(self.mod.REVISION_TABLES), REVISION)

    def test_it_never_commits_inside_the_sql_step(self):
        """run_sql/retire_revision must not commit; main() owns the commit."""
        with open(os.path.join(ROOT, "pipeline", "migrations", "consolidate.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        for start in ("def run_sql", "def retire_revision"):
            body = src[src.index(start):src.index("def verify")]
            self.assertNotIn("commit()", body, start)
        # main() commits once on the merge path and once on the retire path,
        # each guarded by a rollback on failure.
        main_body = src[src.index("def main"):]
        self.assertEqual(main_body.count("conn.commit()"), 2)
        self.assertGreaterEqual(main_body.count("conn.rollback()"), 2)


if __name__ == "__main__":
    unittest.main()
