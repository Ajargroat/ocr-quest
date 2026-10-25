"""Offline tests for SQLite staging; no Supabase/Postgres/network access."""
import os
import tempfile
import threading
import unittest

from pipeline.staging import StagingStore

class StagingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = StagingStore(os.path.join(self.tmp.name, "staging.db"))
        self.store.upsert_source({"source_id":"s1","rel_path":"zist/10/ch1/q.pdf",
                                  "subject":"zist","grade":10,"topic":"ch1","type":"question"})

    def tearDown(self):
        self.tmp.cleanup()

    def test_question_upsert_round_trips_and_is_idempotent(self):
        row = {"id":"s1-q1","source_id":"s1","question_number":1,
               "question_text":"first","options":[{"label":"1","text":"A"}],
               "bbox":[10,20,30,40],"ocr_json":{"raw":"first"}}
        self.store.upsert_question(row)
        self.store.upsert_question(row)
        saved = self.store.get("question","s1-q1")
        self.assertEqual(saved["question_text"],"first")
        self.assertIn('"label":"1"',saved["options_json"])

    def test_reocr_does_not_overwrite_edited_or_ready_rows(self):
        row = {"id":"s1-q1","source_id":"s1","question_number":1,"question_text":"OCR"}
        self.store.upsert_question(row)
        self.store.set_status("question","s1-q1","edited")
        self.store.upsert_question({**row,"question_text":"new OCR"})
        self.assertEqual(self.store.get("question","s1-q1")["question_text"],"OCR")

    def test_answer_defaults_to_manual_match(self):
        self.store.upsert_answer({"id":"s1-a1","source_id":"s1","question_number":1,
                                  "answer_explanation":"answer","assets":[]})
        saved=self.store.get("answer","s1-a1")
        self.assertEqual(saved["match_state"],"needs_manual")
        self.assertEqual(saved["link_kind"],"staged")

    def test_parallel_upserts_are_serialized(self):
        errors=[]
        def worker(n):
            try:
                self.store.upsert_question({"id":f"s1-q{n}","source_id":"s1",
                                            "question_number":n,"question_text":str(n)})
            except Exception as exc:
                errors.append(exc)
        threads=[threading.Thread(target=worker,args=(n,)) for n in range(1,21)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(errors,[])
        self.assertEqual(sum(self.store.get("question",f"s1-q{n}") is not None
                             for n in range(1,21)),20)

if ( __name__ == "__main__" ):
    unittest.main()
