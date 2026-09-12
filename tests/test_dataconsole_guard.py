"""Offline tests for the Data Console SQL guard — pure logic, no database.

Covers pipeline.dataconsole.guard_sql: what it allows (single read/write
statements over the five tables) and what it must refuse (DDL, stacked
statements, WHERE-less writes, other schemas, keyword smuggling through
comments or literals).

Run them either way:
    python tests/test_dataconsole_guard.py
    pytest tests/test_dataconsole_guard.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.dataconsole import GuardError, _jsonable, guard_sql, quote_ident


def allowed(sql):
    """Returns the guard's verdict kind, or raises GuardError."""
    return guard_sql(sql)[0]


class GuardAllows(unittest.TestCase):
    def test_simple_select(self):
        self.assertEqual(allowed("SELECT * FROM questions LIMIT 10"), "read")

    def test_select_no_semicolon(self):
        self.assertEqual(allowed("SELECT COUNT(*) FROM sources"), "read")

    def test_cte_is_not_a_table(self):
        sql = ("WITH recent AS (SELECT id FROM answers ORDER BY created_at DESC "
               "LIMIT 5) SELECT * FROM recent")
        self.assertEqual(allowed(sql), "read")

    def test_join_over_whitelisted_tables(self):
        sql = ("SELECT s.file_name FROM sources s "
               "JOIN questions q ON q.source_id = s.id LIMIT 5")
        self.assertEqual(allowed(sql), "read")

    def test_information_schema_metadata(self):
        sql = ("SELECT column_name FROM information_schema.columns "
               "WHERE table_name = 'answers'")
        self.assertEqual(allowed(sql), "read")

    def test_update_with_where(self):
        sql = "UPDATE questions SET review_status = 'pending' WHERE id = 'abc'"
        self.assertEqual(allowed(sql), "write")

    def test_delete_with_where(self):
        sql = "DELETE FROM revision_reports WHERE severity = 'err'"
        self.assertEqual(allowed(sql), "write")

    def test_insert(self):
        sql = "INSERT INTO sources (id, file_name) VALUES ('x', 'y.jpg')"
        self.assertEqual(allowed(sql), "write")

    def test_public_schema_qualifier(self):
        self.assertEqual(allowed("SELECT id FROM public.questions LIMIT 1"), "read")

    def test_trailing_semicolon_and_comments(self):
        sql = "-- a comment;\nSELECT 1 /*; trick */ FROM questions;\n"
        self.assertEqual(allowed(sql), "read")

    def test_semicolon_inside_literal_is_single_statement(self):
        sql = "SELECT id FROM questions WHERE corp = 'a;b' LIMIT 1"
        self.assertEqual(allowed(sql), "read")


class GuardRefuses(unittest.TestCase):
    def assert_blocked(self, sql, needle=""):
        with self.assertRaises(GuardError) as cm:
            guard_sql(sql)
        if needle:
            self.assertIn(needle, str(cm.exception))

    def test_empty(self):
        self.assert_blocked("   ")

    def test_ddl_verbs(self):
        for sql in ("DROP TABLE questions",
                    "TRUNCATE sources",
                    "ALTER TABLE answers DROP COLUMN raw_ocr_text",
                    "CREATE TABLE evil (id text)",
                    "CREATE OR REPLACE FUNCTION x() RETURNS int AS $$ SELECT 1 $$"):
            self.assert_blocked(sql)

    def test_privileges_and_ops(self):
        for sql in ("GRANT ALL ON questions TO PUBLIC",
                    "REVOKE ALL ON questions FROM PUBLIC",
                    "VACUUM FULL questions",
                    "CLUSTER questions",
                    "REINDEX TABLE answers",
                    "LOCK TABLE questions"):
            self.assert_blocked(sql)

    def test_dml_without_where(self):
        self.assert_blocked("DELETE FROM questions", "WHERE")
        self.assert_blocked("UPDATE answers SET review_status = 'pending'", "WHERE")

    def test_where_in_comment_does_not_count(self):
        self.assert_blocked("DELETE FROM questions -- WHERE id = 1")

    def test_stacked_statements(self):
        self.assert_blocked("SELECT 1; DROP TABLE answers", "one statement")
        self.assert_blocked("UPDATE questions SET corp = 'x' WHERE id = 'a'; "
                            "DELETE FROM sources WHERE id = 'b'", "one statement")

    def test_forbidden_functions(self):
        for sql in ("SELECT pg_sleep(60)",
                    "SELECT * FROM pg_catalog.pg_user",
                    "SELECT setval('seq', 1)",
                    "SELECT lo_import('/etc/passwd')",
                    "SELECT dblink('host=evil', 'SELECT 1')"):
            self.assert_blocked(sql)

    def test_foreign_tables(self):
        self.assert_blocked("SELECT * FROM auth.users")
        self.assert_blocked("SELECT * FROM storage.objects")
        self.assert_blocked("SELECT * FROM some_unknown_table")
        self.assert_blocked("UPDATE questions SET corp = (SELECT name FROM auth.users)")

    def test_select_into_creates_table(self):
        self.assert_blocked("SELECT * INTO evil FROM questions LIMIT 1", "INTO")

    def test_transaction_control(self):
        for sql in ("BEGIN", "COMMIT", "ROLLBACK", "SET statement_timeout = 0",
                    "RESET ALL"):
            self.assert_blocked(sql)

    def test_copy_and_do_blocks(self):
        self.assert_blocked("COPY questions TO '/tmp/x.csv'")
        self.assert_blocked("DO $$ BEGIN PERFORM 1; END $$")

    def test_keyword_smuggling_in_literal_still_blocked_by_real_verb(self):
        # the literal is scrubbed, but the second statement is real and dies
        self.assert_blocked("SELECT id FROM questions WHERE corp = 'x' "
                            "UNION SELECT password FROM pg_catalog.pw")

    def test_unterminated_literal(self):
        self.assert_blocked("SELECT * FROM questions WHERE corp = 'oops")

    def test_unterminated_block_comment(self):
        self.assert_blocked("SELECT 1 /* never closed")


class IdentAndJsonHelpers(unittest.TestCase):
    def test_quote_ident_rejects_injection(self):
        with self.assertRaises(GuardError):
            quote_ident('x"; DROP TABLE y; --')
        self.assertEqual(quote_ident("question_text"), '"question_text"')

    def test_jsonable_serializes_db_types(self):
        from datetime import datetime, timezone
        from decimal import Decimal
        self.assertEqual(_jsonable(datetime(2026, 1, 2, 3, 4, 5)), "2026-01-02T03:04:05")
        self.assertEqual(_jsonable(Decimal("1.5")), 1.5)
        self.assertEqual(_jsonable(b"\x00\x01"), "0001")
        self.assertEqual(_jsonable([Decimal("2")]), [2.0])
        self.assertIsNone(_jsonable(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
