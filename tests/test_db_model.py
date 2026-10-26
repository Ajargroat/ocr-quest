"""Offline tests for C1c: pipeline/db.py on the flat pipeline core.

No database and no network: every test stubs the ``_execute`` seam, so they pin
the SQL text and the bound parameters a real run would send. The schema is the
five-table core (sources/questions/answers/revision_reports/revision_summary);
pipeline/migrations/consolidate.sql states it (pinned by tests/test_consolidate.py).

Run them either way:
    python tests/test_db_model.py
    pytest tests/test_db_model.py        (if pytest is installed)
"""
import json
import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.db import (
    Database,
    folder_id,
    media_from_diagram,
    media_images_update,
    media_images_value,
    question_type,
    set_media_bbox,
    storage_object_name,
)
from pipeline.scanner import SourceItem

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def make_db(handler=None):
    """A Database whose _execute is a stub - nothing connects, ever."""
    db = Database(SimpleNamespace(
        postgres_host="", postgres_port=0, postgres_db="",
        postgres_user="", postgres_password="", postgres_sslmode=""))
    calls = []

    def fake_execute(sql, params=None, fetch=False):
        calls.append({"sql": sql, "params": params, "fetch": fetch})
        return handler(sql, params, fetch) if handler else []

    db._execute = fake_execute
    db.calls = calls
    return db


def question_row(**over):
    row = {
        "id": "abc123-q7",
        "source_id": "abc123",
        "question_number": 7,
        "question_text": "prompt text",
        "options": json.dumps([{"label": "1", "text": "A"}], ensure_ascii=False),
        "subject": "zist",
        "topic": "gavarish",
        "grade": 10,
        "corp": "x",
        "year": "1403",
        "difficulty": "easy",
        "tags": "[]",
        "review_status": "pending",
        "raw_ocr_text": "raw",
    }
    row.update(over)
    return row


def answer_row(**over):
    row = {
        "id": "abc123-a7",
        "source_id": "abc123",
        "question_id": "abc123-q7",
        "question_number": 7,
        "answer_explanation": "explained",
        "correct_option_label": "1",
        "correct_option_text": "A",
        "assets": "[]",
        "subject": "zist",
        "topic": "gavarish",
        "grade": 10,
        "corp": "x",
        "year": "1403",
        "difficulty": "easy",
        "tags": "[]",
        "review_status": "pending",
        "raw_ocr_text": "raw",
        "content_hash": "hash",
    }
    row.update(over)
    return row


def source_item(**over):
    fields = {
        "source_id": "abc123",
        "rel_path": "zist/10/gavarish/question/a.jpg",
        "file_name": "a.jpg",
        "mime_type": "image/jpeg",
        "file_path": "zist/10/gavarish/question/a.jpg",
        "file_size": 100,
        "subject": "zist",
        "grade": 10,
        "topic": "gavarish",
        "type": "sual",
    }
    fields.update(over)
    return SourceItem(**fields)


class QuestionUpsertTests(unittest.TestCase):
    """The question upsert writes the merged columns, never the old ones."""

    def _upsert(self, row):
        db = make_db()
        db.upsert_questions([row])
        self.assertEqual(len(db.calls), 1)
        return db.calls[0]

    def test_question_upsert_binds_the_merged_columns(self):
        call = self._upsert(question_row())
        sql, params = call["sql"], call["params"]
        self.assertIn("INSERT INTO questions", sql)
        self.assertIn("prompt", sql)
        self.assertIn("ON CONFLICT (legacy_id)", sql)
        self.assertNotIn("UNIQUE", sql)
        self.assertIn("media_images", sql)
        self.assertIn("split", sql)
        self.assertIn("duplicate", sql)
        # the old text id is the conflict key; `id` is the DB's own bigserial
        self.assertEqual(params[0], "abc123-q7")
        self.assertEqual(params[2], "prompt text")
        # one placeholder per bound value (the trailing COALESCE(%s, now()) too)
        self.assertEqual(sql.count("%s"), len(params))

    def test_question_upsert_defaults_options_and_media(self):
        call = self._upsert(question_row(options=None))
        params = call["params"]
        self.assertEqual(params[5], "[]")          # options is NOT NULL now
        self.assertEqual(params[6], "[]")          # media_images is NOT NULL too

    def test_question_upsert_derives_type_and_media_from_a_diagram_row(self):
        row = question_row(options="[]", diagram_url=(
            "https://p.supabase.co/storage/v1/object/public/konkour-pages/abc123"),
            diagram_bbox="[1, 2, 3, 4]")
        call = self._upsert(row)
        params = call["params"]
        self.assertEqual(params[1], "short_answer")     # no options -> short answer
        media = json.loads(params[6])
        self.assertEqual(media, [{"object": "abc123", "bbox": [1, 2, 3, 4],
                                  "page": 1}])

    def test_question_upsert_keeps_the_media_array_a_row_carries(self):
        carried = json.dumps([{"object": "studio/j/k.jpg", "bbox": None,
                               "page": 2}])
        call = self._upsert(question_row(media_images=carried))
        self.assertEqual(call["params"][6], carried)
        self.assertEqual(call["params"][1], "mcq_single")

    def test_question_upsert_passes_the_new_flags_through(self):
        call = self._upsert(question_row(split="tail", duplicate=1, unreadable=0,
                                         content_hash="h"))
        params = call["params"]
        self.assertIn("tail", params)
        self.assertIn(True, params)
        self.assertIn(False, params)
        self.assertIn("h", params)


class ReimportProtectionTests(unittest.TestCase):
    """Re-imports must not erase human decisions or manually repaired links."""

    def test_question_upsert_preserves_terminal_review_status(self):
        call = make_db()
        call.upsert_questions([question_row(review_status="approved")])
        sql = call.calls[0]["sql"]
        self.assertIn("WHEN questions.review_status IN ('approved', 'rejected')", sql)
        self.assertIn("THEN questions.review_status", sql)
        self.assertIn("ELSE EXCLUDED.review_status", sql)

    def test_answer_upsert_preserves_terminal_review_status_and_existing_link(self):
        db = make_db()
        db.upsert_answers([answer_row(review_status="approved", question_id=None)])
        sql = db.calls[0]["sql"]
        self.assertIn("COALESCE(EXCLUDED.question_id, answers.question_id)", sql)
        self.assertIn("WHEN answers.review_status IN ('approved', 'rejected')", sql)
        self.assertIn("THEN answers.review_status", sql)
        self.assertIn("ELSE EXCLUDED.review_status", sql)


class FolderIdTests(unittest.TestCase):
    def test_folder_id_from_rel_path(self):
        self.assertEqual(folder_id("zist/10/gavarish/question/a.jpg"),
                         "zist/10/gavarish/question")
        self.assertEqual(folder_id("zist\\10\\gavarish\\question\\a.jpg"),
                         "zist/10/gavarish/question")
        self.assertIsNone(folder_id(""))
        self.assertIsNone(folder_id(None))

    def test_upsert_source_binds_folder_id(self):
        db = make_db()
        db.upsert_source(source_item(), "https://x/a.jpg")
        sql, params = db.calls[0]["sql"], db.calls[0]["params"]
        self.assertIn("folder_id", sql)
        self.assertEqual(params[1], "zist/10/gavarish/question")

    def test_upsert_source_without_a_folder_binds_empty_text(self):
        db = make_db()
        db.upsert_source(source_item(rel_path="", source_id="studio-abc"),
                         "https://x/a.jpg")
        # the column is NOT NULL: a folderless studio source keeps an empty id
        self.assertEqual(db.calls[0]["params"][1], "")


class AnswerUpsertTests(unittest.TestCase):
    def test_answer_upsert_resolves_legacy_question_id(self):
        db = make_db()
        db.upsert_answers([answer_row()])
        sql, params = db.calls[0]["sql"], db.calls[0]["params"]
        self.assertIn("legacy_question_id", sql)
        self.assertIn("(SELECT id FROM public.questions", sql)
        self.assertIn("legacy_id = %s", sql)
        # resolved twice (legacy id and bigserial id) and kept once as text
        self.assertEqual(params.count("abc123-q7"), 3)
        self.assertEqual(params[3], "abc123-q7")

    def test_answer_upsert_defaults_assets_and_media(self):
        db = make_db()
        db.upsert_answers([answer_row(assets=None)])
        params = db.calls[0]["params"]
        self.assertIn("[]", params)
        self.assertEqual(params[10], "[]")     # assets
        self.assertEqual(params[11], "[]")     # media_images


class ReviewSelectTests(unittest.TestCase):
    """The QBank payload now carries the media array and the flags."""

    def test_review_select_aliases_prompt_and_carries_flags(self):
        sql = Database._REVIEW_SELECT
        self.assertIn("q.prompt AS question_text", sql)
        self.assertIn("q.media_images", sql)
        self.assertIn("q.split", sql)
        self.assertIn("q.duplicate", sql)
        self.assertIn("q.unreadable", sql)
        self.assertNotIn("q.diagram_url", sql)
        self.assertNotIn("q.diagram_bbox", sql)
        for name in ("media_images", "split", "duplicate", "unreadable"):
            self.assertIn(name, Database._REVIEW_COLS)
        self.assertEqual(len(Database._REVIEW_COLS),
                         len(sql.split("SELECT")[1].split("FROM")[0].split(",")))

    def test_picture_exists_reads_the_media_array(self):
        self.assertIn("jsonb_array_length(q.media_images)",
                      Database._PICTURE_EXISTS)
        self.assertNotIn("diagram_bbox", Database._PICTURE_EXISTS)


class RevisionLaneTests(unittest.TestCase):
    """The revision lane keeps its diagram_* keys, backed by media_images."""

    def test_revision_select_exposes_diagram_keys_from_the_media_array(self):
        sql = Database._Q_REVISION_SELECT
        self.assertIn("q.prompt AS question_text", sql)
        self.assertIn("q.media_images -> 0 ->> 'object' AS diagram_url", sql)
        self.assertIn("AS diagram_bbox", sql)
        self.assertIn("q.media_images::text AS media_images", sql)
        self.assertNotIn("q.diagram_bbox", sql)
        self.assertIn("media_images", Database._Q_REVISION_COLS)

    def test_apply_question_updates_writes_prompt_and_media(self):
        db = make_db()
        db.apply_question_updates([{"id": 5, "question_text": "t",
                                    "media_images": "[]"}])
        sql, params = db.calls[0]["sql"], db.calls[0]["params"]
        self.assertIn("prompt = %s", sql)
        self.assertIn("media_images = %s", sql)
        self.assertNotIn("has_diagram", sql)
        self.assertIn("[]", params)

    def test_media_images_update_patches_one_entry_only(self):
        row = {
            "media_images": json.dumps([
                {"object": "a.jpg", "bbox": [0, 0, 0, 0], "page": 1},
                {"object": "b.jpg", "bbox": [1, 1, 1, 1], "page": 2}]),
            "diagram_url": "https://x/storage/v1/object/public/b/c.jpg",
            "diagram_bbox": "[5, 6, 7, 8]",
            "has_diagram": True,
        }
        got = json.loads(media_images_update(row))
        self.assertEqual(len(got), 2)                       # never narrowed
        self.assertEqual(got[0], {"object": "c.jpg", "bbox": [5, 6, 7, 8],
                                  "page": 1})
        self.assertEqual(got[1]["object"], "b.jpg")

    def test_media_images_update_clears_the_zero_placeholder(self):
        row = {"media_images": json.dumps([{"object": "a.jpg",
                                            "bbox": [0, 0, 0, 0], "page": 1}]),
               "diagram_bbox": None, "has_diagram": False}
        self.assertEqual(json.loads(media_images_update(row))[0]["bbox"], None)

    def test_media_images_update_builds_the_first_entry(self):
        row = {"diagram_url": "https://x/storage/v1/object/public/b/c.jpg",
               "diagram_bbox": "[5, 6, 7, 8]", "has_diagram": True}
        self.assertEqual(json.loads(media_images_update(row)),
                         [{"object": "c.jpg", "bbox": [5, 6, 7, 8], "page": 1}])


class MediaBboxTests(unittest.TestCase):
    def test_set_media_bbox_rewrites_one_entry(self):
        media = [{"object": "a", "bbox": None, "page": 1},
                 {"object": "b", "bbox": [1, 2, 3, 4], "page": 2}]
        got = set_media_bbox(media, 0, [5, 6, 7, 8])
        self.assertEqual(got[0]["bbox"], [5, 6, 7, 8])
        self.assertEqual(got[1], media[1])
        self.assertIsNone(media[0]["bbox"])          # input untouched

    def test_set_media_bbox_out_of_range_is_a_copy(self):
        media = [{"object": "a", "bbox": None, "page": 1}]
        got = set_media_bbox(media, 4, [5, 6, 7, 8])
        self.assertEqual(got, media)
        self.assertIsNot(got, media)

    def test_update_media_bbox_reads_then_writes(self):
        def handler(sql, params, fetch):
            if "SELECT media_images" in sql:
                return [('[{"object": "a", "bbox": null, "page": 1}]',)]
            return []

        db = make_db(handler)
        db.update_media_bbox(5, 0, [9, 9, 9, 9])
        self.assertEqual(len(db.calls), 2)
        self.assertIn("SELECT media_images", db.calls[0]["sql"])
        self.assertIn("UPDATE public.questions SET media_images = %s",
                      db.calls[1]["sql"])
        self.assertEqual(json.loads(db.calls[1]["params"][0]),
                         [{"object": "a", "bbox": [9, 9, 9, 9], "page": 1}])


class HelperTests(unittest.TestCase):
    def test_storage_object_name_skips_the_unknown_object(self):
        self.assertEqual(
            storage_object_name(
                "https://p.supabase.co/storage/v1/object/public/k/abc.jpg"),
            "abc.jpg")
        self.assertIsNone(storage_object_name(
            "https://p.supabase.co/storage/v1/object/public/k/unknown"))
        self.assertIsNone(storage_object_name("crops/p1.jpg"))
        self.assertIsNone(storage_object_name(None))

    def test_media_from_diagram_flags_a_missing_object(self):
        self.assertEqual(media_from_diagram(None, "[1,2,3,4]"), ([], True))
        self.assertEqual(
            media_from_diagram(
                "https://p.supabase.co/storage/v1/object/public/k/unknown",
                "[1,2,3,4]"), ([], True))
        entries, missing = media_from_diagram(
            "https://p.supabase.co/storage/v1/object/public/k/abc.jpg",
            "[0,0,0,0]")
        self.assertFalse(missing)
        self.assertIsNone(entries[0]["bbox"])       # zero bbox is not a bbox

    def test_media_images_value_prefers_the_carried_array(self):
        carried = '[{"object": "x", "bbox": null, "page": 1}]'
        self.assertEqual(media_images_value({"media_images": carried}), carried)
        self.assertEqual(media_images_value({}), "[]")

    def test_question_type_derives_from_the_options(self):
        self.assertEqual(question_type(None, "[]"), "short_answer")
        self.assertEqual(question_type(None, '["a", "b"]'), "mcq_single")
        self.assertEqual(question_type("mcq_multi", "[]"), "mcq_multi")
        self.assertEqual(question_type(None, "not json"), "short_answer")


class BboxEndpointTests(unittest.TestCase):
    """The bbox endpoint passes the media index through (C1c integration)."""

    def test_bbox_endpoint_carries_the_media_index(self):
        with open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn("index: int = 0", src)
        self.assertIn("db.update_media_bbox(body.id, body.index", src)
        self.assertNotIn("update_diagram_bbox", src)


if __name__ == "__main__":
    unittest.main()
