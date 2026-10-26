"""Offline tests for the staging store; no Supabase/Postgres/network.

Covers the P2 acceptance list: the identity contract, the empty state, the
local page/draft/crop files, status transitions, re-OCR protection, temp-file
persistence (a write leaves no half file), transactions/rollback, duplicate
ids, a missing source, parallel writers and the batch lifecycle.
"""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import staging  # noqa: E402
from pipeline.staging import StagingStore, source_id_for  # noqa: E402

REL = "zist/10/ch1/question/q.pdf"


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.store = StagingStore(os.path.join(self.root, "staging.db"), root=self.root)
        self.sid = self.store.upsert_source({"rel_path": REL, "subject": "zist",
                                            "grade": 10, "topic": "ch1",
                                            "type": "question"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def question(self, number=1, **extra):
        row = {"id": "%s-q%d" % (self.sid, number), "source_id": self.sid,
               "question_number": number, "question_text": "Q%d" % number,
               "options": [{"label": "1", "text": "A"}], "bbox": [10, 20, 300, 400],
               "page": 1, "ocr_json": {"raw": "Q%d" % number}}
        row.update(extra)
        return row


class IdentityTests(StoreCase):
    def test_source_id_is_sha1_of_the_normalised_rel_path(self):
        import hashlib
        expect = hashlib.sha1(REL.encode()).hexdigest()[:40]
        self.assertEqual(source_id_for(REL), expect)
        self.assertEqual(source_id_for(REL.replace("/", "\\")), expect)
        self.assertEqual(self.sid, expect)

    def test_record_ids_follow_the_contract(self):
        self.store.upsert_question(self.question(3))
        self.assertTrue(self.store.get("question", "%s-q3" % self.sid))

    def test_the_same_rel_path_is_one_source(self):
        again = self.store.upsert_source({"rel_path": REL, "topic": "changed"})
        self.assertEqual(again, self.sid)
        self.assertEqual(len(self.store.list_sources()), 1)

    def test_a_duplicate_id_updates_in_place(self):
        self.store.upsert_question(self.question(1))
        self.store.upsert_question(self.question(1, question_text="new"))
        self.assertEqual(self.store.get("question", "%s-q1" % self.sid)["question_text"],
                         "new")
        self.assertEqual(len(self.store.list_records("question")), 1)


class StatusTests(StoreCase):
    def test_a_new_record_is_staged_and_moves_forward(self):
        self.store.upsert_question(self.question(1))
        rid = "%s-q1" % self.sid
        self.assertEqual(self.store.get("question", rid)["status"], "staged")
        self.assertTrue(self.store.set_status("question", rid, "ready"))
        self.assertEqual(self.store.get("question", rid)["status"], "ready")
        self.assertIn("question", str(self.store.counts()))

    def test_an_invalid_status_is_refused(self):
        self.store.upsert_question(self.question(1))
        with self.assertRaises(ValueError):
            self.store.set_status("question", "%s-q1" % self.sid, "approved")

    def test_the_empty_state_is_available_and_visible(self):
        self.store.upsert_question(self.question(1, question_text=None))
        rid = "%s-q1" % self.sid
        self.store.set_status("question", rid, "empty")
        self.assertEqual(self.store.get("question", rid)["status"], "empty")
        self.assertEqual(self.store.counts()["st_questions"].get("empty"), 1)
        self.store.set_source_status(self.sid, "empty")
        self.assertEqual(self.store.get_source(self.sid)["status"], "empty")

    def test_an_answers_default_is_needs_manual(self):
        self.store.upsert_answer({"id": "%s-a1" % self.sid, "source_id": self.sid,
                                  "question_number": 1, "answer_explanation": "x"})
        saved = self.store.get("answer", "%s-a1" % self.sid)
        self.assertEqual(saved["match_state"], "needs_manual")
        self.assertEqual(saved["link_kind"], "staged")

    def test_match_states_are_constrained(self):
        with self.assertRaises(Exception):
            self.store.upsert_answer({"id": "%s-a2" % self.sid, "source_id": self.sid,
                                      "match_state": "guessed"})


class ReOcrProtectionTests(StoreCase):
    def test_re_ocr_never_overwrites_a_human_or_final_state(self):
        rid = "%s-q1" % self.sid
        for status in ("edited", "ready", "imported", "skipped"):
            self.store._set_status("st_questions", rid, "staged", staging.RECORD_STATUSES)
            self.store.upsert_question(self.question(1, question_text="Q1"))
            self.store.set_status("question", rid, status)
            written = self.store.upsert_question(self.question(1, question_text="OCR again"))
            self.assertFalse(written, status)
            row = self.store.get("question", rid)
            self.assertEqual(row["question_text"], "Q1", status)
            self.assertEqual(row["status"], status, status)

    def test_re_ocr_does_update_a_plain_staged_row(self):
        rid = "%s-q1" % self.sid
        self.store.upsert_question(self.question(1))
        self.assertTrue(self.store.upsert_question(self.question(1, question_text="v2")))
        self.assertEqual(self.store.get("question", rid)["question_text"], "v2")

    def test_a_protected_source_keeps_its_status(self):
        self.store.set_source_status(self.sid, "ready")
        self.store.upsert_source({"rel_path": REL, "status": "staged"})
        self.assertEqual(self.store.get_source(self.sid)["status"], "ready")


class EditTests(StoreCase):
    def test_an_edit_writes_the_value_and_marks_the_row_edited(self):
        self.store.upsert_question(self.question(1))
        row = self.store.update_record("question", "%s-q1" % self.sid,
                                       {"question_text": "edited", "tags": ["شیمی"]})
        self.assertEqual(row["question_text"], "edited")
        self.assertEqual(row["status"], "edited")
        self.assertEqual(json.loads(row["tags_json"]), ["شیمی"])

    def test_a_ready_row_stays_ready_through_an_edit(self):
        rid = "%s-q1" % self.sid
        self.store.upsert_question(self.question(1))
        self.store.set_status("question", rid, "ready")
        row = self.store.update_record("question", rid, {"caption": "cap"})
        self.assertEqual(row["status"], "ready")

    def test_metadata_edits_land_in_the_fields_json(self):
        self.store.upsert_question(self.question(1))
        row = self.store.update_record("question", "%s-q1" % self.sid,
                                       {"corp": "qalam", "year": "1403-1404"})
        fields = json.loads(row["fields_json"])
        self.assertEqual(fields["corp"], "qalam")
        self.assertEqual(fields["year"], "1403-1404")

    def test_an_unknown_field_is_refused(self):
        self.store.upsert_question(self.question(1))
        with self.assertRaises(ValueError):
            self.store.update_record("question", "%s-q1" % self.sid, {"colour": "red"})

    def test_an_imported_row_is_frozen(self):
        rid = "%s-q1" % self.sid
        self.store.upsert_question(self.question(1))
        self.store.set_status("question", rid, "imported")
        with self.assertRaises(ValueError):
            self.store.update_record("question", rid, {"question_text": "x"})


class FileLifecycleTests(StoreCase):
    def test_a_page_and_a_crop_are_written_and_read_back(self):
        self.store.save_page(self.sid, 1, b"page-bytes")
        self.store.save_crop(self.sid, "q1-0.jpg", b"crop-bytes")
        self.assertEqual(self.store.read_asset(self.sid, "p1.jpg"), b"page-bytes")
        self.assertEqual(self.store.read_asset(self.sid, "q1-0.jpg"), b"crop-bytes")
        self.assertIsNone(self.store.read_asset(self.sid, "missing.jpg"))

    def test_a_crop_name_cannot_escape_the_asset_dir(self):
        self.store.save_crop(self.sid, "../../evil.jpg", b"x")
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "assets", self.sid, "evil.jpg")))
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil.jpg")))

    def test_a_write_leaves_no_temp_file(self):
        self.store.save_page(self.sid, 2, b"abc")
        names = os.listdir(os.path.join(self.root, "assets", self.sid))
        self.assertIn("p2.jpg", names)
        self.assertFalse([n for n in names if n.endswith(".tmp")])

    def test_an_existing_page_is_replaced_not_appended(self):
        self.store.save_page(self.sid, 1, b"first")
        self.store.save_page(self.sid, 1, b"second")
        self.assertEqual(self.store.read_asset(self.sid, "p1.jpg"), b"second")

    def test_the_source_file_is_mirrored_once(self):
        path = os.path.join(self.root, "q.pdf")
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4")
        first = self.store.mirror_source(self.sid, path)
        self.assertTrue(os.path.exists(first))
        self.assertEqual(self.store.mirror_source(self.sid, path), first)
        self.assertIsNone(self.store.mirror_source(self.sid, ""))


class RollbackTests(StoreCase):
    def test_a_failed_transaction_leaves_no_partial_row(self):
        with self.assertRaises(RuntimeError):
            with self.store._transaction() as db:
                db.execute("INSERT INTO st_sources(source_id,rel_path) "
                           "VALUES ('x','x')")
                raise RuntimeError("boom")
        self.assertIsNone(self.store.get_source("x"))

    def test_a_pre_existing_v1_store_gains_the_new_columns(self):
        import sqlite3
        path = os.path.join(self.root, "v1.db")
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE st_sources (source_id TEXT PRIMARY KEY, rel_path TEXT NOT NULL,
              local_path TEXT, subject TEXT, grade TEXT, topic TEXT, type TEXT,
              page_w INTEGER, page_h INTEGER, ocr_at TEXT,
              status TEXT NOT NULL DEFAULT 'staged');
            CREATE TABLE st_questions (id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              question_number INTEGER, question_text TEXT, options_json TEXT,
              fields_json TEXT NOT NULL DEFAULT '{}', bbox_json TEXT,
              crop_state TEXT NOT NULL DEFAULT 'pending',
              status TEXT NOT NULL DEFAULT 'staged', ocr_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE st_answers (id TEXT PRIMARY KEY, source_id TEXT NOT NULL,
              question_number INTEGER, answer_explanation TEXT, assets_json TEXT,
              fields_json TEXT NOT NULL DEFAULT '{}', bbox_json TEXT,
              link_kind TEXT NOT NULL DEFAULT 'staged', link_id TEXT, match_method TEXT,
              match_state TEXT NOT NULL DEFAULT 'needs_manual',
              status TEXT NOT NULL DEFAULT 'staged', ocr_json TEXT NOT NULL DEFAULT '{}',
              updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
            INSERT INTO st_sources(source_id, rel_path) VALUES ('old','a/b.pdf');
        """)
        old.commit()
        old.close()
        store = StagingStore(path, root=self.root)
        try:
            self.assertEqual(store.get_source("old")["rel_path"], "a/b.pdf")
            self.assertTrue(store.upsert_question({"id": "old-q1", "source_id": "old",
                                                   "question_number": 1,
                                                   "caption": "cap"}))
            self.assertEqual(store.get("question", "old-q1")["caption"], "cap")
        finally:
            store.close()

    def test_a_missing_source_is_created_as_a_placeholder(self):
        self.store.upsert_question({"id": "solo-q1", "source_id": "solo",
                                    "rel_path": "nobody/here/q.pdf"})
        self.assertEqual(self.store.get_source("solo")["rel_path"], "nobody/here/q.pdf")

    def test_a_closed_store_refuses_writes(self):
        self.store.close()
        with self.assertRaises(ValueError):
            self.store.upsert_question({"id": "x-q1", "source_id": "x"})


class ConcurrencyTests(StoreCase):
    def test_parallel_writers_are_serialised(self):
        errors = []

        def worker(n):
            try:
                self.store.upsert_question(self.question(n))
            except Exception as exc:            # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(1, 41)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        rows = self.store.list_records("question", limit=100)
        self.assertEqual(len(rows), 40)

    def test_two_stores_on_one_file_see_each_other(self):
        other = StagingStore(self.store.path, root=self.root)
        try:
            other.upsert_question(self.question(9))
            self.assertTrue(self.store.get("question", "%s-q9" % self.sid))
        finally:
            other.close()


class BatchTests(StoreCase):
    def test_a_batch_lists_its_rows_and_moves_through_its_states(self):
        self.store.upsert_question(self.question(1))
        rid = "%s-q1" % self.sid
        batch = self.store.create_batch("test", scope={"source_id": self.sid},
                                        rows=[("question", rid)], total=1)
        self.assertEqual(batch["status"], "staged")
        self.assertEqual([r["record_id"] for r in batch["rows"]], [rid])
        self.store.set_batch_status(batch["batch_id"], "importing")
        self.store.set_batch_row(batch["batch_id"], "question", rid, "imported")
        done = self.store.set_batch_status(batch["batch_id"], "imported", imported=1)
        self.assertEqual(done["status"], "imported")
        self.assertEqual(done["imported"], 1)
        self.assertEqual(self.store.batch_rows(batch["batch_id"])[0]["status"], "imported")
        self.assertEqual(self.store.list_batches()[0]["batch_id"], batch["batch_id"])

    def test_an_invalid_batch_status_is_refused(self):
        batch = self.store.create_batch("x")
        with self.assertRaises(ValueError):
            self.store.set_batch_status(batch["batch_id"], "done")

    def test_the_same_batch_id_is_idempotent(self):
        rows = [("question", "abc-q1")]
        self.store.create_batch("x", batch_id="fixed", rows=rows, total=1)
        self.store.create_batch("x", batch_id="fixed", rows=rows, total=1)
        self.assertEqual(len(self.store.get_batch("fixed")["rows"]), 1)


class SourceTallyTests(unittest.TestCase):
    """The Inspect source list carries its own row counts."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.store = StagingStore(os.path.join(self.root, "staging.db"), root=self.root)
        self.sid = self.store.upsert_source({"rel_path": "a/q.pdf", "subject": "zist",
                                             "grade": 12, "topic": "ch1"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_the_list_counts_the_rows_of_each_source(self):
        for n in (1, 2):
            self.store.upsert_question({"id": "%s-q%d" % (self.sid, n),
                                        "source_id": self.sid, "question_number": n,
                                        "fields": {}})
        self.store.upsert_answer({"id": "%s-a1" % self.sid, "source_id": self.sid,
                                  "question_number": 1, "fields": {}})
        row = [s for s in self.store.list_sources() if s["source_id"] == self.sid][0]
        self.assertEqual(row["question_count"], 2)
        self.assertEqual(row["answer_count"], 1)

    def test_an_empty_source_reports_zero_not_null(self):
        row = [s for s in self.store.list_sources() if s["source_id"] == self.sid][0]
        self.assertEqual(row["question_count"], 0)
        self.assertEqual(row["answer_count"], 0)


if __name__ == "__main__":
    unittest.main()
