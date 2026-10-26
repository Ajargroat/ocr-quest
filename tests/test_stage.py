"""Offline tests for pipeline.stage — one OCR result -> local staging only.

A real multi-page PDF is built in memory, so the page split, the per-page
render, the crop and the row identity are all exercised end to end. The DB
object passed in is a sentinel that raises if it is touched by a WRITE: an OCR
file must stage without a single Postgres or Supabase write.
"""
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import stage  # noqa: E402
from pipeline.staging import StagingStore, source_id_for  # noqa: E402


@dataclass
class Item:
    source_id: str
    rel_path: str
    subject: str
    grade: object
    topic: str
    type: str
    file_name: str
    file_path: str
    file_size: int
    mime_type: str


class ReadOnlyDb:
    """Answer candidates come from here; any write-shaped call fails the test."""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.reads = 0

    def fetch_context_questions(self, subject=None, topic=None, grade=None):
        self.reads += 1
        return list(self.rows)

    def __getattr__(self, name):
        raise AssertionError("an OCR file must not touch db.%s while staging" % name)


def make_pdf(pages=3, width=300, height=400):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(width, height)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


class StageCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.store = StagingStore(os.path.join(self.root, "staging.db"), root=self.root)
        path = os.path.join(self.root, "q.pdf")
        with open(path, "wb") as fh:
            fh.write(make_pdf(3))
        self.rel = "zist/12/ch1/question/q.pdf"
        self.item = Item(source_id_for(self.rel), self.rel, "zist", 12, "ch1",
                         "سوال", "q.pdf", path, os.path.getsize(path),
                         "application/pdf")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class PageSplitTests(StageCase):
    def test_a_pdf_becomes_one_image_per_page(self):
        with open(self.item.file_path, "rb") as fh:
            images = stage.page_images(fh.read(), "application/pdf")
        self.assertEqual([n for n, _ in images], [1, 2, 3])
        self.assertTrue(all(data[:2] == b"\xff\xd8" for _, data in images))

    def test_a_single_image_is_page_one_unchanged(self):
        raw = b"\xff\xd8original-jpeg\xff\xd9"
        self.assertEqual(stage.page_images(raw, "image/jpeg"), [(1, raw)])

    def test_a_broken_pdf_is_a_named_error(self):
        with self.assertRaises(ValueError):
            stage.page_images(b"%PDF-1.4 not really", "application/pdf")


class StageQuestionsTests(StageCase):
    def parsed(self):
        return {"questions": [
            {"question_number": 1, "question_text": r"\frac{1}{2} چند است؟",
             "options": [{"label": "1", "text": "الف"}],
             "diagram_bbox": [100, 100, 400, 800], "corp": "گاج", "year": "۱۴۰۳-۱۴۰۴"},
            {"question_number": 2, "question_text": "بدون شکل"},
        ]}

    def test_rows_use_the_contract_ids_and_keep_the_ocr_json(self):
        out = stage.stage_file(self.store, self.item, self.parsed())
        self.assertEqual(out.question_ids, ["%s-q1" % self.item.source_id,
                                            "%s-q2" % self.item.source_id])
        row = self.store.get("question", "%s-q1" % self.item.source_id)
        self.assertEqual(json.loads(row["ocr_json"])["question_number"], 1)
        self.assertEqual(out.crop_files, 1)

    def test_pages_are_mirrored_with_their_sizes(self):
        stage.stage_file(self.store, self.item, self.parsed())
        source = self.store.get_source(self.item.source_id)
        self.assertEqual(source["page_count"], 3)
        self.assertAlmostEqual(source["page_w"], 300 * 170 / 72, delta=3)  # 170 dpi
        meta = json.loads(source["meta_json"])
        self.assertEqual(len(meta["pages"]), 3)
        self.assertTrue(os.path.exists(self.store.page_path(self.item.source_id, 3)))

    def test_a_bbox_is_cut_under_a_deterministic_name(self):
        stage.stage_file(self.store, self.item, self.parsed())
        row = self.store.get("question", "%s-q1" % self.item.source_id)
        self.assertIn("crops/", row["crop_key"])
        self.assertTrue(row["crop_key"].endswith("q1-0.jpg"))
        data = self.store.read_asset(self.item.source_id,
                                     os.path.basename(row["crop_key"]))
        self.assertTrue(data and data[:2] == b"\xff\xd8")
        self.assertEqual(row["crop_state"], "cut")

    def test_a_figure_less_question_stores_no_crop(self):
        stage.stage_file(self.store, self.item, self.parsed())
        row = self.store.get("question", "%s-q2" % self.item.source_id)
        self.assertIsNone(row["bbox_json"])
        self.assertIsNone(row["crop_key"])

    def test_math_and_zwnj_are_repaired_once(self):
        stage.stage_file(self.store, self.item, self.parsed())
        row = self.store.get("question", "%s-q1" % self.item.source_id)
        self.assertEqual(row["question_text"], r"$\frac{1}{2}$ چند است؟")

    def test_metadata_lands_in_fields_and_keeps_the_raw_year(self):
        stage.stage_file(self.store, self.item, self.parsed())
        fields = json.loads(self.store.get(
            "question", "%s-q1" % self.item.source_id)["fields_json"])
        self.assertEqual(fields["corp"], "گاج")
        self.assertEqual(fields["year"], "۱۴۰۳-۱۴۰۴")
        self.assertEqual(fields["grade"], 12)

    def test_no_postgres_or_storage_write_happens(self):
        db = ReadOnlyDb()
        out = stage.stage_file(self.store, self.item, self.parsed(), db=db)
        self.assertEqual(out.count, 2)
        self.assertEqual(db.reads, 0)      # a question file needs no candidates


class EmptyTests(StageCase):
    def test_an_empty_extraction_is_staged_as_empty_and_reported(self):
        out = stage.stage_file(self.store, self.item, {"questions": []})
        self.assertTrue(out.is_empty)
        self.assertEqual(("source", self.item.source_id), out.empty_records[-1])
        self.assertEqual(self.store.get_source(self.item.source_id)["status"], "empty")

    def test_a_record_without_text_becomes_empty(self):
        out = stage.stage_file(self.store, self.item,
                               {"questions": [{"question_number": 4,
                                               "question_text": ""}]})
        self.assertIn(("question", "%s-q4" % self.item.source_id), out.empty_records)
        self.assertEqual(self.store.get("question",
                                        "%s-q4" % self.item.source_id)["status"],
                         "empty")


class StageAnswersTests(StageCase):
    def answer_item(self):
        rel = "zist/12/ch1/answer/a.pdf"
        return Item(source_id_for(rel), rel, "zist", 12, "ch1", "پاسخ", "a.pdf",
                    self.item.file_path, os.path.getsize(self.item.file_path),
                    "application/pdf")

    def test_an_answer_links_against_a_staged_question(self):
        stage.stage_file(self.store, self.item, {"questions": [
            {"question_number": 1, "question_text": "Q1", "corp": "گاج",
             "year": "1403-1404"}]})
        db = ReadOnlyDb()
        item = self.answer_item()
        candidates = stage.candidate_questions(self.store, db, "zist", "ch1", 12)
        self.assertTrue(candidates)
        self.assertEqual(db.reads, 1)
        out = stage.stage_file(self.store, item, {"answers": [
            {"question_number": 1, "answer_explanation": "پاسخ", "corp": "گاج",
             "year": "۱۴۰۳-۱۴۰۴"}]}, candidates=candidates, db=db)
        row = self.store.get("answer", "%s-a1" % item.source_id)
        self.assertEqual(row["match_state"], "auto_ok")
        self.assertEqual(row["link_id"], "%s-q1" % self.item.source_id)
        self.assertEqual(out.answer_ids, ["%s-a1" % item.source_id])

    def test_the_live_bank_is_merged_into_the_candidates(self):
        db = ReadOnlyDb([{"id": 77, "question_number": 4, "corp": "گاج",
                          "year": "1403", "subject": "zist", "topic": "ch1",
                          "grade": 12, "question_text": "live"}])
        pool = stage.candidate_questions(self.store, db, "zist", "ch1", 12)
        self.assertEqual([c["id"] for c in pool], [77])
        self.assertEqual(pool[0]["origin"], "db")

    def test_a_failing_live_bank_does_not_stop_staging(self):
        class Broken(ReadOnlyDb):
            def fetch_context_questions(self, *a, **k):
                raise RuntimeError("pooler closed")
        pool = stage.candidate_questions(self.store, Broken(), "zist", "ch1", 12)
        self.assertEqual(pool, [])

    def test_an_unmatched_answer_needs_a_human(self):
        item = self.answer_item()
        stage.stage_file(self.store, item, {"answers": [
            {"question_number": 9, "answer_explanation": "پاسخ"}]},
            candidates=[], db=ReadOnlyDb())
        row = self.store.get("answer", "%s-a9" % item.source_id)
        self.assertEqual(row["match_state"], "needs_manual")
        self.assertIsNone(row["link_id"])


class ZeroLiveWriteTests(unittest.TestCase):
    """P3 acceptance: the OCR path may not write the live DB or the bucket.

    A grep-level pin, because the point is the ABSENCE of a call: if someone
    re-adds ``upload_file``/``db.upsert_*`` to the runner or the staging
    bridge, this fails before any live project is touched.
    """

    BANNED = ("upload_file", "upsert_source", "upsert_questions",
              "upsert_answers", "storage.", "supabase")

    def _source(self, name):
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(here, "pipeline", name), encoding="utf-8") as fh:
            return fh.read()

    def test_the_runner_has_no_live_write_call(self):
        import re
        code = self._source("runner.py")
        body = re.sub(r"^\s*#.*$", "", code, flags=re.M)      # comments say why
        body = re.sub(r'""".*?"""', "", body, flags=re.S)     # docstrings too
        for banned in self.BANNED:
            self.assertNotIn(banned, body, "runner.py still calls %s" % banned)

    def test_the_staging_bridge_only_touches_the_local_store(self):
        import re
        body = re.sub(r'""".*?"""', "", self._source("stage.py"), flags=re.S)
        self.assertNotIn("pipeline.db", body)
        self.assertNotIn("upload", body.replace("upload the source", ""))
        self.assertTrue(body.count("store.upsert_") >= 3)     # it DOES stage

    def test_a_run_needs_no_supabase_credential(self):
        from types import SimpleNamespace
        from pipeline import runner
        cfg = SimpleNamespace(supabase_key="", ocr_provider="ninerouter",
                              ninerouter_enabled=True,
                              ocr_base_url="http://127.0.0.1:20128",
                              revision_active=False)
        self.assertEqual(runner.missing_credentials(cfg), [])


if __name__ == "__main__":
    unittest.main()
