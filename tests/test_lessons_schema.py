"""Schema-contract tests for pipeline/studio/migrations.sql (C1a + C1b).

The migration is hand-applied and completely inert, so these tests parse the
SQL text only: no database, no network, nothing is ever executed. They pin the
root PDF's lessons model, the merged OCR ``questions`` table, the Sapienstech
read-side mirror, the FK ordering and the idempotency markers.

Run them either way:
    python tests/test_lessons_schema.py
    pytest tests/test_lessons_schema.py        (if pytest is installed)
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA_PATH = os.path.join(ROOT, "pipeline", "studio", "migrations.sql")

# Directories that are not application code (a suite run must stay offline).
SKIP_DIRS = {".venv", ".git", ".agent", "__pycache__", "backups",
             "node_modules", "tests"}

TABLES = [
    "tenants", "domains", "users", "tests", "students",
    "questions_sapienstech", "test_questions",
    "courses", "chapters", "lessons", "media_assets", "sources", "questions",
    "question_options", "short_answer_keys", "lesson_blocks", "attempts",
    "lesson_progress", "answers", "revision_reports", "revision_summary",
]

PDF_TABLES = ["courses", "chapters", "lessons", "media_assets",
              "question_options", "short_answer_keys", "lesson_blocks",
              "attempts", "lesson_progress"]

MIRROR_TABLES = ["tenants", "domains", "users", "tests", "students",
                 "questions_sapienstech", "test_questions"]


def read_schema():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return fh.read()


def squash(text):
    return re.sub(r"\s+", " ", text).strip()


def statements(sql):
    """Yield every ``;``-separated statement, comment lines stripped first.

    Comments are dropped before the split: the header one-liner contains
    semicolons of its own, which must not break statements apart.
    """
    body = "\n".join(ln for ln in sql.splitlines()
                     if not ln.lstrip().startswith("--"))
    for chunk in body.split(";"):
        text = squash(chunk)
        if text:
            yield text


def table_block(sql, name):
    prefix = "CREATE TABLE IF NOT EXISTS %s (" % name
    for stmt in statements(sql):
        if stmt.startswith(prefix):
            return stmt
    return ""


class SchemaContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sql = read_schema()
        cls.flat = squash(cls.sql)

    def test_schema_file_exists_and_is_hand_applied(self):
        self.assertTrue(os.path.isfile(SCHEMA_PATH))
        self.assertTrue(self.sql.startswith("--"))
        self.assertIn("NEVER applied", self.sql)
        self.assertIn("Idempotent", self.sql)
        self.assertIn("psql", self.sql)               # documented fallback
        self.assertIn("migrations.sql", self.sql)

    def test_every_create_is_guarded(self):
        self.assertIsNone(re.search(r"CREATE TABLE (?!IF NOT EXISTS)", self.sql))
        self.assertIsNone(re.search(r"CREATE INDEX (?!IF NOT EXISTS)", self.sql))
        self.assertIsNone(re.search(r"CREATE UNIQUE INDEX", self.sql))
        type_stmts = [s for s in statements(self.sql) if "CREATE TYPE" in s]
        self.assertEqual(len(type_stmts), 3)
        for stmt in type_stmts:
            self.assertTrue(stmt.startswith("DO $$ BEGIN"),
                            "CREATE TYPE must sit inside a DO guard: %r" % stmt)
        self.assertEqual(
            self.flat.count("EXCEPTION WHEN duplicate_object THEN NULL; END $$"),
            3)
        self.assertIsNone(re.search(r"CREATE SCHEMA|CREATE EXTENSION", self.sql))

    def test_all_declared_tables_are_created(self):
        for name in TABLES:
            header = "CREATE TABLE IF NOT EXISTS %s (" % name
            self.assertIn(header, self.flat, "%s is not created" % name)

    def test_mirror_tables_are_minimal_and_kept(self):
        users = table_block(self.sql, "users")
        self.assertIn("preferences JSONB", users)
        self.assertIn("domain_id BIGINT REFERENCES domains(id)", users)
        tests = table_block(self.sql, "tests")
        self.assertIn("created_by_user_id BIGINT REFERENCES users(id)", tests)
        self.assertIn("question_count SMALLINT", tests)
        students = table_block(self.sql, "students")
        self.assertIn("grade TEXT", students)
        bridge = table_block(self.sql, "test_questions")
        self.assertIn("test_id BIGINT NOT NULL REFERENCES tests(id)", bridge)
        self.assertIn(
            "question_id BIGINT NOT NULL REFERENCES questions_sapienstech(id)",
            bridge)

    def test_lessons_ddl_matches_root_pdf(self):
        order = [self.flat.find("CREATE TYPE block_type AS ENUM"),
                 self.flat.find("CREATE TYPE image_layout AS ENUM"),
                 self.flat.find("CREATE TYPE question_type AS ENUM")]
        self.assertNotIn(-1, order)
        self.assertEqual(order, sorted(order))
        self.assertIn(
            "('heading','paragraph','list','image','image_group',"
            "'equation','callout','question')", self.flat)
        self.assertIn("('start','end','wide','duo')", self.flat)
        self.assertIn("('mcq_single','mcq_multi','short_answer')", self.flat)
        for name in ("lessons", "media_assets", "question_options",
                     "short_answer_keys", "lesson_blocks", "attempts",
                     "lesson_progress"):
            self.assertIn("CREATE TABLE IF NOT EXISTS %s (" % name, self.flat)
        media = table_block(self.sql, "media_assets")
        self.assertIn("alt_text TEXT NOT NULL", media)
        keys = table_block(self.sql, "short_answer_keys")
        self.assertIn("keywords TEXT[]", keys)
        self.assertIn("model_answer TEXT", keys)
        blocks = table_block(self.sql, "lesson_blocks")
        self.assertIn("UNIQUE (lesson_id, position)", blocks)
        self.assertIn(
            "CHECK (type <> 'image' OR (media_id IS NOT NULL "
            "AND layout IS NOT NULL))", blocks)
        self.assertIn("CHECK (type <> 'question' OR question_id IS NOT NULL)",
                      blocks)
        lessons = table_block(self.sql, "lessons")
        for column in ("chapter_id BIGINT NOT NULL REFERENCES chapters(id)",
                       "reading_min SMALLINT", "status TEXT NOT NULL",
                       "locale TEXT NOT NULL", "UNIQUE (chapter_id, slug)"):
            self.assertIn(column, lessons)

    def test_questions_table_is_pdf_plus_ocr(self):
        block = table_block(self.sql, "questions")
        self.assertIn("id BIGSERIAL PRIMARY KEY", block)
        for column in (
                "type question_type NOT NULL", "prompt TEXT NOT NULL",
                "explanation TEXT", "hint TEXT",
                "points SMALLINT NOT NULL DEFAULT 1", "max_length SMALLINT",
                "legacy_id TEXT UNIQUE",
                "sapienstech_id BIGINT REFERENCES questions_sapienstech(id)",
                "source_id TEXT REFERENCES sources(id)",
                "question_number INTEGER",
                "options TEXT NOT NULL DEFAULT '[]'",
                "media_images JSONB NOT NULL DEFAULT '[]'::jsonb",
                "review_status TEXT NOT NULL", "revision_status TEXT NOT NULL",
                "needs_review BOOLEAN NOT NULL", "raw_ocr_text TEXT",
                "content_hash TEXT", "split TEXT",
                "duplicate BOOLEAN NOT NULL", "unreadable BOOLEAN NOT NULL",
                "created_at TIMESTAMPTZ NOT NULL"):
            self.assertIn(column, block)
        self.assertNotIn("question_text", block)
        self.assertNotIn("diagram_url", block)     # replaced by media_images
        self.assertIn("CHECK (review_status IN ('pending','approved','rejected'))",
                      block)
        self.assertIn("CHECK (jsonb_typeof(media_images) = 'array')", block)

    def test_content_hash_is_not_unique(self):
        self.assertNotIn("UNIQUE (content_hash)", self.flat)
        self.assertIsNone(re.search(r"UNIQUE INDEX[^;]*content_hash",
                                    self.flat))
        self.assertIn("content_hash TEXT",
                      table_block(self.sql, "questions"))
        self.assertIn("content_hash TEXT",
                      table_block(self.sql, "answers"))

    def test_options_and_media_are_not_null(self):
        self.assertIn("options TEXT NOT NULL DEFAULT '[]'",
                      table_block(self.sql, "questions"))
        self.assertIn("media_images JSONB NOT NULL DEFAULT '[]'::jsonb",
                      table_block(self.sql, "questions"))
        self.assertIn("media_images JSONB NOT NULL DEFAULT '[]'::jsonb",
                      table_block(self.sql, "answers"))

    def test_split_check_matches_the_interface_contract(self):
        self.assertIn(
            "CHECK (split IS NULL OR split IN ('tail','head','both'))",
            self.flat)
        self.assertEqual(self.flat.count("split IN ("), 1)

    def test_sources_carries_folder_id(self):
        block = table_block(self.sql, "sources")
        self.assertIn("folder_id TEXT NOT NULL", block)
        self.assertIn("id TEXT PRIMARY KEY", block)

    def test_answers_keep_the_legacy_question_id(self):
        block = table_block(self.sql, "answers")
        self.assertIn("question_id BIGINT REFERENCES questions(id)", block)
        self.assertIn("legacy_question_id TEXT", block)
        self.assertIn("legacy_id TEXT", block)
        self.assertIn("id TEXT PRIMARY KEY", block)

    def test_fk_targets_are_created_before_they_are_referenced(self):
        positions = {}
        for match in re.finditer(
                r"CREATE TABLE IF NOT EXISTS (\w+) \(", self.sql):
            positions[match.group(1)] = match.start()
        self.assertEqual(sorted(positions), sorted(TABLES))
        for name, start in positions.items():
            block = table_block(self.sql, name)
            for target in re.findall(r"REFERENCES (\w+)\(", block):
                self.assertIn(target, positions,
                              "%s references missing %s" % (name, target))
                self.assertLess(positions[target], start,
                                "%s must be created before %s" % (target, name))

    def test_schema_file_has_no_destructive_statement(self):
        self.assertIsNone(
            re.search(r"\bDROP\b|\bTRUNCATE\b|\bDELETE\s+FROM\b",
                      self.sql, re.IGNORECASE))

    # The one module allowed to name the schema file: it only tells the user
    # which file to apply and refuses to write without --apply (pinned in
    # tests/test_lessons_migration.py). Nothing else may reference it.
    SCHEMA_NAMERS = ("pipeline/studio/migrate_lessons.py",)

    def test_schema_file_is_never_auto_run(self):
        offenders = []
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, ROOT)
                if rel.replace(os.sep, "/") in self.SCHEMA_NAMERS:
                    continue
                with open(path, encoding="utf-8", errors="replace") as fh:
                    if "migrations.sql" in fh.read():
                        offenders.append(rel)
        self.assertEqual(offenders, [])

    def test_the_schema_namer_exception_is_still_the_one_off(self):
        """The exception may not spread: it is one file, and it is the module
        whose writes are gated behind --apply."""
        self.assertEqual(len(self.SCHEMA_NAMERS), 1)
        with open(os.path.join(ROOT, *self.SCHEMA_NAMERS[0].split("/")),
                  encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("--apply", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
