"""Offline tests for C2: pipeline/studio/migrate_lessons.py.

The module is a hand-run one-off, so nothing here connects to a database: the
CLI and the driver are exercised with stub connections, and every repair is a
pure function over plain dicts. The source-side shapes are the live model the
probes measured on 2026-10-19 (3,931 questions, 406 recurring -q<N> suffixes,
329 /unknown diagram rows).

Run them either way:
    python tests/test_lessons_migration.py
    pytest tests/test_lessons_migration.py        (if pytest is installed)
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.studio import migrate_lessons as mig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOHOM = "\u062f\u0647\u0645"                      # "dahom" (grade 10)
ZIST = "\u0632\u06cc\u0633\u062a\u200c\u0634\u0646\u0627\u0633\u06cc"
OBJ = "https://p.supabase.co/storage/v1/object/public/konkour-pages/"


def question(**over):
    row = {"id": "abc123-q1", "source_id": "abc123", "question_number": 1,
           "question_text": "text", "options": "[]", "subject": "zist",
           "topic": "gavarish", "grade": 10, "corp": "x", "year": "1403",
           "difficulty": "easy", "tags": "[]", "review_status": "pending",
           "raw_ocr_text": "raw", "content_hash": "h", "diagram_url": None,
           "diagram_bbox": None, "correct_option_label": None,
           "created_at": None}
    row.update(over)
    return row


class FakeCursor:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def mogrify(self, template, args):
        return template

    def fetchall(self):
        rows, self.rows = self.rows, []      # a queued result is read once
        return rows

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class FakeConn:
    """A connection that only records commit/rollback - no DB, no network."""

    def __init__(self, dsn="postgresql://same", cursor=None):
        self.dsn = dsn
        self._cursor = cursor or FakeCursor()
        self.committed = 0
        self.rolled_back = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1


class CliTests(unittest.TestCase):
    def test_apply_is_required_to_write(self):
        self.assertFalse(mig.parse_args(["--target-dsn", "x"]).apply)
        self.assertTrue(mig.parse_args(["--target-dsn", "x", "--apply"]).apply)

    def test_refuses_same_database(self):
        same = FakeConn(dsn="postgresql://one")
        real_connect = mig.connect
        mig.connect = lambda cfg=None, dsn=None: same
        try:
            self.assertEqual(mig.main(["--target-dsn", "postgresql://one"]), 2)
        finally:
            mig.connect = real_connect
        self.assertEqual(same.committed, 0)          # nothing was touched


class RunTests(unittest.TestCase):
    def test_run_refuses_a_target_without_the_lessons_schema(self):
        src = FakeConn()
        tgt = FakeConn(cursor=FakeCursor(rows=[(None,)]))
        self.assertIsNone(mig.run(src, tgt, apply=True))
        self.assertEqual(tgt.committed, 0)

    def test_dry_run_rolls_back(self):
        src = FakeConn()
        tgt = FakeConn(cursor=FakeCursor(rows=[("courses",)]))
        counts = mig.run(src, tgt, apply=False, only=["accounts"])
        self.assertEqual(counts, {"accounts": 0})
        self.assertEqual(tgt.rolled_back, 1)
        self.assertEqual(tgt.committed, 0)

    def test_the_module_is_never_imported_by_the_app(self):
        """No startup path, importer or endpoint may reference the one-off."""
        app_dirs = [os.path.join(ROOT, "pipeline"), os.path.join(ROOT, "ui")]
        files = [os.path.join(ROOT, "main.py")]
        for base in app_dirs:
            for dirpath, dirs, names in os.walk(base):
                dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
                files += [os.path.join(dirpath, n) for n in names
                          if n.endswith(".py")]
        hits = []
        for path in files:
            if os.path.basename(path) == "migrate_lessons.py":
                continue
            with open(path, encoding="utf-8") as fh:
                if "migrate_lessons" in fh.read():
                    hits.append(os.path.relpath(path, ROOT))
        self.assertEqual(hits, [])


class FenceLatexTests(unittest.TestCase):
    def test_fences_a_bare_array(self):
        self.assertEqual(mig.fence_latex("\\begin{array}{c|c}1&2\\end{array}"),
                         "$$\\begin{array}{c|c}1&2\\end{array}$$")

    def test_fences_only_the_environment(self):
        got = mig.fence_latex("see:\n\\begin{array}{c}1\\end{array}\ntail")
        self.assertEqual(
            got, "see:\n$$\\begin{array}{c}1\\end{array}$$\ntail")
        both = mig.fence_latex("\\begin{array}{c}a\\end{array} and "
                               "\\begin{cases}b\\end{cases}")
        self.assertEqual(
            both, "$$\\begin{array}{c}a\\end{array}$$ and "
                  "$$\\begin{cases}b\\end{cases}$$")

    def test_fences_a_multiline_environment(self):
        got = mig.fence_latex("\\begin{array}{c}1\\\\\n2\\end{array}")
        self.assertEqual(got, "$$\\begin{array}{c}1\\\\\n2\\end{array}$$")

    def test_leaves_fenced_text_alone(self):
        for text in ("$$\\begin{array}{c}1\\end{array}$$",
                     "\\[ \\begin{array}{c}1\\end{array} \\]",
                     "plain", None, "\\begin{array}{c}1"):
            self.assertEqual(mig.fence_latex(text), text)


class FenceWiringTests(unittest.TestCase):
    def test_question_rows_fence_the_prompt(self):
        rows = mig.question_rows([{"id": "a-q1", "source_id": "a",
                                   "question_text": "\\begin{array}{c}1\\end{array}",
                                   "options": "[]"}])
        self.assertEqual(rows[0]["prompt"],
                         "$$\\begin{array}{c}1\\end{array}$$")

    def test_answer_rows_fence_the_explanation(self):
        rows = mig.answer_rows([{"id": "a-a1", "question_id": "a-q1",
                                 "answer_explanation": "\\begin{array}{c}1\\end{array}"}])
        self.assertEqual(rows[0]["answer_explanation"],
                         "$$\\begin{array}{c}1\\end{array}$$")


class MediaTests(unittest.TestCase):
    def test_unknown_object_falls_back_to_the_source(self):
        got = mig.media_from_legacy(OBJ + "unknown", "[1,2,3,4]", OBJ + "abc")
        self.assertEqual(got, ([{"object": "abc", "bbox": [1, 2, 3, 4],
                                 "page": 1}], False))

    def test_zero_bbox_is_null(self):
        entries, missing = mig.media_from_legacy(OBJ + "abc", "[0,0,0,0]", None)
        self.assertFalse(missing)
        self.assertIsNone(entries[0]["bbox"])

    def test_a_blank_diagram_means_no_picture(self):
        self.assertEqual(mig.media_from_legacy(None, None, None), ([], False))
        self.assertEqual(mig.media_from_legacy("", None, OBJ + "src"), ([], False))
        self.assertEqual(mig.media_from_legacy("   ", None, None), ([], False))

    def test_an_unusable_diagram_without_a_source_marks_unreadable(self):
        self.assertEqual(mig.media_from_legacy(OBJ + "unknown", None, None),
                         ([], True))

    def test_the_diagram_url_wins_over_the_source(self):
        entries, _ = mig.media_from_legacy(OBJ + "crop.jpg", None, OBJ + "src.pdf")
        self.assertEqual(entries[0]["object"], "crop.jpg")


class DuplicateTests(unittest.TestCase):
    def test_duplicate_needs_the_number_and_the_same_text(self):
        rows = [{"id": "a-q231", "source_id": "a", "question_text": "same"},
                {"id": "b-q231", "source_id": "b", "question_text": "same"},
                {"id": "a-q99", "source_id": "a", "question_text": "first"},
                {"id": "a-q99", "source_id": "a", "question_text": "second"},
                {"id": "studio-job-1", "source_id": "studio-job"}]
        got = mig.duplicate_legacy_ids(rows)
        self.assertEqual(got, {"a-q231", "b-q231"})

    def test_no_suffix_is_never_flagged(self):
        self.assertEqual(mig.duplicate_legacy_ids(
            [{"id": "studio-job-1", "source_id": "s"}]), set())


class OptionTests(unittest.TestCase):
    def test_question_kind(self):
        self.assertEqual(mig.question_kind("[]"), "short_answer")
        self.assertEqual(mig.question_kind(None), "short_answer")
        self.assertEqual(mig.question_kind("not json"), "short_answer")
        self.assertEqual(mig.question_kind('["a", "b"]'), "mcq_single")
        self.assertEqual(mig.question_kind('[{"label": "1", "text": "a"}]'),
                         "mcq_single")

    def test_options_rows_marks_the_correct_label(self):
        options = json.dumps([{"label": "1", "text": "a"},
                              {"label": "2", "text": "b"}])
        got = mig.options_rows(options, "2")
        self.assertEqual(got, [{"label": "1", "position": 1, "is_correct": False},
                               {"label": "2", "position": 2, "is_correct": True}])

    def test_grade_and_subject_vocabulary(self):
        self.assertEqual(mig.grade_label(10), DOHOM)
        self.assertIsNone(mig.grade_label(7))
        self.assertEqual(mig.subject_fa("zist"), ZIST)
        self.assertEqual(mig.subject_fa("other"), "other")


class RowBuildingTests(unittest.TestCase):
    def test_one_course_per_subject_grade(self):
        rows = mig.course_rows([question(), question(id="abc123-q2")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["slug"], "zist-10")
        self.assertIn(DOHOM, rows[0]["title"])
        self.assertEqual(rows[0]["key"], ("zist", 10))

    def test_chapters_follow_topics(self):
        rows = mig.chapter_rows(
            [question(), question(id="b-q2", topic="diger")],
            {("zist", 10): 4})
        self.assertEqual(len(rows), 2)
        self.assertEqual(sorted(r["position"] for r in rows), [1, 2])
        self.assertEqual({r["course_id"] for r in rows}, {4})

    def test_lesson_per_source_and_block_per_question(self):
        sources = [{"id": "abc123", "file_name": "f1.pdf", "subject": "zist",
                    "grade": 10, "topic": "gavarish", "created_at": None}]
        lessons = mig.lesson_rows(sources, {("zist", 10, "gavarish"): 7})
        self.assertEqual(len(lessons), 1)
        self.assertEqual(lessons[0]["title"], "f1.pdf")
        self.assertEqual(lessons[0]["chapter_id"], 7)
        blocks = mig.block_rows([question(), question(id="abc123-q2",
                                                      question_number=2)],
                                {"abc123": 5})
        self.assertEqual(len(blocks), 2)
        self.assertEqual([b["position"] for b in blocks], [1, 2])
        self.assertEqual([b["question_id"] for b in blocks],
                         ["abc123-q1", "abc123-q2"])
        self.assertTrue(all(b["type"] == "question" for b in blocks))

    def test_repeated_numbers_take_the_next_free_block_position(self):
        blocks = mig.block_rows([question(), question(id="abc123-q1b",
                                                      question_number=1)],
                                {"abc123": 5})
        self.assertEqual([b["position"] for b in blocks], [1, 2])

    def test_short_answer_key_only_for_optionless_questions(self):
        answers = [{"id": "a1", "question_id": "abc123-q1",
                    "answer_explanation": "model answer"}]
        mcq = question(options='["a", "b"]', correct_option_label="1")
        keys = mig.short_answer_key_rows([question(), mcq], answers)
        self.assertEqual(keys, [{"question_id": "abc123-q1",
                                 "model_answer": "model answer",
                                 "keywords": None}])

    def test_options_are_built_per_question(self):
        mcq = question(options=json.dumps([{"label": "1", "text": "a"}]),
                       correct_option_label="1")
        rows = mig.option_row_list([mcq, question()], {"abc123-q1": 11})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["question_id"], 11)
        self.assertTrue(rows[0]["is_correct"])

    def test_question_rows_move_the_legacy_columns(self):
        rows = mig.question_rows(
            [question(options='["a"]', diagram_url=OBJ + "unknown",
                      diagram_bbox="[0,0,0,0]")],
            source_urls={"abc123": OBJ + "src.pdf"},
            sapienstech={"abc123-q1": 30},
            duplicates={"abc123-q1"})
        row = rows[0]
        self.assertEqual(row["legacy_id"], "abc123-q1")
        self.assertEqual(row["prompt"], "text")
        self.assertEqual(row["type"], "mcq_single")
        self.assertTrue(row["duplicate"])
        self.assertFalse(row["unreadable"])          # the source file is the picture
        self.assertEqual(row["sapienstech_id"], 30)
        self.assertIsNone(row["split"])
        self.assertEqual(json.loads(row["media_images"])[0]["object"], "src.pdf")

    def test_answer_rows_keep_the_legacy_question_id(self):
        rows = mig.answer_rows(
            [{"id": "abc123-a1", "source_id": "abc123",
              "question_id": "abc123-q1", "question_number": 1,
              "answer_explanation": "e", "assets": None}],
            {"abc123-q1": 11})
        self.assertEqual(rows[0]["id"], "abc123-a1")
        self.assertEqual(rows[0]["question_id"], 11)
        self.assertEqual(rows[0]["legacy_question_id"], "abc123-q1")
        self.assertEqual(rows[0]["assets"], "[]")
        self.assertEqual(rows[0]["media_images"], "[]")


class JsonValueTests(unittest.TestCase):
    """JSONB values come back as dicts from psycopg2; execute_values rejects them."""

    def test_jsonb_dicts_are_adapted_and_other_types_pass_through(self):
        from psycopg2.extras import Json

        self.assertIsInstance(mig._json_value({"a": 1}), Json)
        self.assertEqual(mig._json_value(["a"]), ["a"])
        self.assertIsNone(mig._json_value(None))


if __name__ == "__main__":
    unittest.main()
