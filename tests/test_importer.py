"""Offline tests for pipeline.importer — the only live writer (P6).

A fake live database records every call; a fake upload records every bucket
object. The two lists are compared for ORDER, because the promise is
crop-first: a live row must never point at a picture that failed to land.
"""
import json
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import importer  # noqa: E402
from pipeline.staging import StagingStore, source_id_for  # noqa: E402

REL = "zist/12/ch1/question/q.pdf"


class FakeLive:
    def __init__(self, dead=()):
        self.dead = set(dead)
        self.calls = []

    def _do(self, name, rows):
        self.calls.append((name, [r["id"] for r in rows]))
        if name in self.dead:
            raise RuntimeError("postgres said no")

    def upsert_questions(self, rows):
        self._do("upsert_questions", rows)

    def upsert_answers(self, rows):
        self._do("upsert_answers", rows)

    def ops(self):
        return [name for name, _ in self.calls]


class Uploader:
    def __init__(self, fail_on=None):
        self.keys = []
        self.fail_on = fail_on

    def __call__(self, cfg, key, data, *a, **k):
        if self.fail_on and self.fail_on in key:
            raise RuntimeError("502 from the storage edge")
        self.keys.append(key)
        return "https://example/" + key


CFG = SimpleNamespace(supabase_url="https://proj.supabase.co",
                      supabase_bucket="konkour-pages", supabase_key="k")


class ImportCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.store = StagingStore(os.path.join(self.root, "staging.db"), root=self.root)
        self.sid = self.store.upsert_source({"rel_path": REL, "subject": "zist",
                                            "grade": 12, "topic": "ch1"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def question(self, number=1, status="ready", crop="q1-0.jpg", **extra):
        rid = "%s-q%d" % (self.sid, number)
        row = {"id": rid, "source_id": self.sid, "question_number": number,
               "question_text": "Q%d" % number,
               "options": [{"label": "1", "text": "A"}],
               "bbox": [10, 20, 300, 400], "page": 1,
               "crop_key": "crops/%s/%s" % (self.sid, crop) if crop else None,
               "crop_state": "cut" if crop else "pending",
               "fields": {"subject": "zist", "topic": "ch1", "grade": 12,
                          "corp": "qalam", "year": "1403-1404"},
               "ocr_json": {"raw": 1}}
        row.update(extra)
        self.store.upsert_question(row)
        if status != "staged":
            self.store.set_status("question", rid, status)
        if crop:
            self.store.save_crop(self.sid, crop, b"\xff\xd8crop\xff\xd9")
        return rid

    def answer(self, number=1, status="ready", link=None):
        rid = "%s-a%d" % (self.sid, number)
        self.store.upsert_answer({"id": rid, "source_id": self.sid,
                                  "question_number": number,
                                  "answer_explanation": "A%d" % number,
                                  "link_kind": "staged", "link_id": link,
                                  "fields": {"subject": "zist", "topic": "ch1",
                                             "grade": 12}})
        if status != "staged":
            self.store.set_status("answer", rid, status)
        return rid


class OrderingTests(ImportCase):
    def test_only_ready_rows_are_imported(self):
        ready = self.question(1, "ready")
        self.question(2, "staged")
        self.question(3, "edited")
        self.question(4, "skipped")
        live, up = FakeLive(), Uploader()
        report = importer.run(self.store, live=live, cfg=CFG, upload=up)
        self.assertEqual(report["imported"], 1)
        self.assertEqual([i for _, ids in live.calls for i in ids], [ready])

    def test_the_crop_lands_before_the_row(self):
        self.question(1, "ready")
        order = []
        live = FakeLive()
        live.calls = order

        class Watching(FakeLive):
            def _do(self, name, rows):
                order.append(name)
                super()._do(name, rows)

        up = Uploader()
        importer.run(self.store, live=Watching(), cfg=CFG,
                     upload=lambda c, k, d, *a, **kw: (order.append("upload"),
                                                       up(c, k, d))[1])
        self.assertEqual(order[0], "upload")
        self.assertEqual(order[1], "upsert_questions")

    def test_a_failed_crop_upload_stops_that_row_and_leaves_it_ready(self):
        rid = self.question(1, "ready")
        report = importer.run(self.store, live=FakeLive(), cfg=CFG,
                              upload=Uploader(fail_on=self.sid))
        self.assertEqual(report["imported"], 0)
        self.assertEqual(report["failed"], 1)
        self.assertIn("crop upload failed", report["errors"][0]["error"])
        self.assertEqual(self.store.get("question", rid)["status"], "ready")

    def test_a_dead_row_write_leaves_the_row_ready_and_reports_it(self):
        rid = self.question(1, "ready")
        report = importer.run(self.store, live=FakeLive(dead={"upsert_questions"}),
                              cfg=CFG, upload=Uploader())
        self.assertEqual((report["imported"], report["failed"]), (0, 1))
        self.assertEqual(self.store.get("question", rid)["status"], "ready")

    def test_an_imported_row_is_frozen(self):
        rid = self.question(1, "ready")
        importer.run(self.store, live=FakeLive(), cfg=CFG, upload=Uploader())
        self.assertEqual(self.store.get("question", rid)["status"], "imported")
        with self.assertRaises(ValueError):
            self.store.update_record("question", rid, {"question_text": "late edit"})

    def test_a_second_run_imports_nothing_and_reuses_the_crop(self):
        self.question(1, "ready")
        up = Uploader()
        importer.run(self.store, live=FakeLive(), cfg=CFG, upload=up)
        live = FakeLive()
        report = importer.run(self.store, live=live, cfg=CFG, upload=up)
        self.assertEqual(report["imported"], 0)
        self.assertEqual(live.calls, [])
        self.assertEqual(len(up.keys), 1)          # the crop was uploaded once


class BatchTests(ImportCase):
    def test_the_batch_records_each_row_and_its_final_state(self):
        self.question(1, "ready")
        self.question(2, "ready")
        report = importer.run(self.store, live=FakeLive(), cfg=CFG,
                              upload=Uploader())
        batch = self.store.get_batch(report["batch_id"])
        self.assertEqual(batch["status"], "imported")
        self.assertEqual(batch["imported"], 2)
        self.assertEqual([r["status"] for r in batch["rows"]], ["imported", "imported"])

    def test_a_partial_failure_marks_the_batch_failed(self):
        self.question(1, "ready", crop=None)     # no crop: imports fine
        self.question(2, "ready")                # the row write dies
        # only the SECOND write fails, so the batch mixes imported and failed
        class SecondDies(FakeLive):
            def _do(self, name, rows):
                if self.calls:
                    self.calls.append((name, [r["id"] for r in rows]))
                    raise RuntimeError("postgres said no")
                super()._do(name, rows)
        report = importer.run(self.store, live=SecondDies(), cfg=CFG,
                              upload=Uploader())
        self.assertEqual(report["status"], "failed")
        batch = self.store.get_batch(report["batch_id"])
        self.assertEqual({r["status"] for r in batch["rows"]}, {"imported", "failed"})


class LiveRowTests(unittest.TestCase):
    def test_a_ready_question_becomes_an_approved_live_row(self):
        record = {"id": "sid-q1", "source_id": "sid", "question_number": 1,
                  "question_text": "Q", "options_json": '[{"label":"1"}]',
                  "bbox_json": "[10,20,30,40]",
                  "fields_json": json.dumps({"grade": "12", "corp": "qalam"}),
                  "crop_key": "crops/sid/q1-0.jpg", "ocr_json": "{}"}
        row = importer.to_live("question", record, CFG)
        self.assertEqual(row["review_status"], "approved")
        self.assertEqual(row["grade"], 12)                 # smallint, not "12"
        self.assertEqual(row["options"], [{"label": "1"}])
        self.assertTrue(row["diagram_url"].endswith("/konkour-pages/crops/sid/q1-0.jpg"))
        self.assertEqual(row["diagram_bbox"], [10, 20, 30, 40])

    def test_a_crop_less_row_points_at_no_picture(self):
        record = {"id": "sid-q2", "source_id": "sid", "question_number": 2,
                  "question_text": "Q", "fields_json": "{}"}
        row = importer.to_live("question", record, CFG)
        self.assertIsNone(row["diagram_url"])
        self.assertIsNone(row["diagram_bbox"])

    def test_an_answer_carries_its_link_and_keeps_a_missing_grade_null(self):
        record = {"id": "sid-a1", "source_id": "sid", "question_number": 1,
                  "answer_explanation": "A", "link_id": "sid-q1",
                  "fields_json": "{}"}
        row = importer.to_live("answer", record, CFG)
        self.assertEqual(row["question_id"], "sid-q1")
        self.assertIsNone(row["grade"])
        self.assertEqual(row["review_status"], "approved")


if __name__ == "__main__":
    unittest.main()
