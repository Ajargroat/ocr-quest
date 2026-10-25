"""Tests for the studio importers: the sources row the rows point at.

Offline and DB-free: the import writes through a stubbed database handle that
records every call in order, so the id the importer uses and the order it
writes in are pinned without a real Postgres.

Every literal here is ASCII on purpose: the console this suite runs under is
cp1256 and must never be asked to print Persian text.

Run either way:
    python tests/test_studio_importers.py
    pytest tests/test_studio_importers.py
"""
import io
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.scanner import TYPE_DIRS  # noqa: E402
from pipeline.studio import workspace  # noqa: E402


class _DB:
    """A database handle that records every write, in the order it happened."""

    def __init__(self, cfg=None):
        self.calls = []

    def upsert_source(self, item, storage_url):
        self.calls.append(("upsert_source", item, storage_url))

    def upsert_questions(self, rows):
        self.calls.append(("upsert_questions", rows))

    def upsert_answers(self, rows):
        self.calls.append(("upsert_answers", rows))

    @property
    def names(self):
        """The names of the calls, in order."""
        return [call[0] for call in self.calls]

    def source_item(self):
        """The SourceItem handed to upsert_source (None if never called)."""
        for call in self.calls:
            if call[0] == "upsert_source":
                return call[1]
        return None

    def source_url(self):
        """The storage url handed to upsert_source."""
        for call in self.calls:
            if call[0] == "upsert_source":
                return call[2]
        return None

    def question_rows(self):
        """The rows handed to upsert_questions."""
        for call in self.calls:
            if call[0] == "upsert_questions":
                return call[1]
        return []


class StudioImporterSourceRowTests(unittest.TestCase):
    """The imported rows point at a sources row - it must exist first."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="studio-import-")
        self.src = tempfile.mkdtemp(prefix="studio-import-src-")
        self._old_ws = os.environ.get("STUDIO_WORKSPACE")
        os.environ["STUDIO_WORKSPACE"] = self.root
        self._old_src = os.environ.get("STUDIO_SOURCE_ROOT")
        os.environ["STUDIO_SOURCE_ROOT"] = self.src

    def tearDown(self):
        if self._old_ws is None:
            os.environ.pop("STUDIO_WORKSPACE", None)
        else:
            os.environ["STUDIO_WORKSPACE"] = self._old_ws
        if self._old_src is None:
            os.environ.pop("STUDIO_SOURCE_ROOT", None)
        else:
            os.environ["STUDIO_SOURCE_ROOT"] = self._old_src
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.src, ignore_errors=True)

    def _job(self):
        """A job with a stored source file and a merged view of two blocks."""
        jid = workspace.new_id()
        workspace.save_source(jid, io.BytesIO(b"%PDF-1.4 x"), "exam.pdf",
                              "exam.pdf")
        view = {
            "job_id": jid,
            "source": "exam.pdf",
            "folder_id": "f" * 40,
            "profile": {"static": {"subject": "Chemistry", "grade": "10",
                                   "topic": "Mixtures"}},
            "crops": {},
            "blocks": [
                {"key": "p1-b1", "kind": "question", "status": "ready",
                 "number": 1, "text": "first",
                 "options": ["A) one", "B) two"],
                 "subject": "Chemistry", "topic": "Mixtures"},
                {"key": "p2-k1", "kind": "answer_key_entry", "status": "ready",
                 "number": 1, "label": "B", "subject": "Chemistry",
                 "topic": "Mixtures"},
            ],
        }
        return jid, view

    def _run(self, view, dry_run=False):
        """Import through the recording handle; returns (report, recorder)."""
        from pipeline.studio.importers import supabase
        recorder = _DB()
        imports = {"uploads": {"source": None, "crops": {}}}
        with mock.patch.object(supabase.db_mod, "Database",
                               lambda cfg: recorder):
            report = supabase.run({}, view["job_id"], view, imports,
                                  dry_run=dry_run, emit=lambda *a, **k: None)
        return report, recorder

    def test_the_source_row_is_written_before_the_questions_and_answers(self):
        jid, view = self._job()
        report, recorder = self._run(view)
        self.assertEqual(report["questions"], 1)
        self.assertEqual(report["answers"], 1)
        names = recorder.names
        self.assertEqual(names[0], "upsert_source")
        self.assertLess(names.index("upsert_source"),
                        names.index("upsert_questions"))
        self.assertLess(names.index("upsert_source"),
                        names.index("upsert_answers"))

    def test_the_source_row_carries_the_id_the_rows_point_at(self):
        jid, view = self._job()
        report, recorder = self._run(view)
        item = recorder.source_item()
        rows = recorder.question_rows()
        self.assertIsNotNone(item)
        self.assertTrue(rows)
        self.assertEqual(item.source_id, rows[0]["source_id"])
        self.assertEqual(item.source_id, "studio-" + jid)

    def test_the_source_row_describes_the_job(self):
        jid, view = self._job()
        report, recorder = self._run(view)
        item = recorder.source_item()
        self.assertEqual(item.file_name, "exam.pdf")
        self.assertEqual(item.mime_type, "application/pdf")
        self.assertEqual(item.file_size, 10)
        self.assertTrue(item.file_path.endswith("exam.pdf"))
        self.assertEqual(item.subject, "Chemistry")
        self.assertEqual(item.topic, "Mixtures")
        self.assertEqual(item.grade, 10)
        self.assertEqual(item.type, TYPE_DIRS["question"])
        # the studio keeps its sources in the folder tree, never in the bucket
        self.assertEqual(recorder.source_url(), "")
        # the studio tree's own relative path -> the scanner's sha1 folder id,
        # so the sources row can find its folder later
        meta = workspace.read_artifact(jid, "source_meta", {}) or {}
        self.assertEqual(item.rel_path, meta.get("rel_path"))
        from pipeline import db as db_mod
        self.assertTrue(item.rel_path.endswith("exam.pdf"))
        self.assertTrue(db_mod.folder_id(item.rel_path))

    def test_a_job_without_a_stored_source_keeps_an_empty_folder(self):
        jid = workspace.new_id()
        workspace.job_dir(jid, create=True)
        view = {
            "job_id": jid, "source": "gone.pdf",
            "profile": {"static": {"subject": "Chemistry", "grade": "10"}},
            "crops": {},
            "blocks": [{"key": "p1-b1", "kind": "question", "status": "ready",
                        "number": 1, "text": "first",
                        "options": ["A) one", "B) two"]}],
        }
        report, recorder = self._run(view)
        self.assertEqual(recorder.source_item().rel_path, "")
        from pipeline import db as db_mod
        self.assertIsNone(db_mod.folder_id(""))

    def test_a_grade_that_is_not_a_number_is_left_out(self):
        jid, view = self._job()
        view["profile"]["static"]["grade"] = "tenth"
        report, recorder = self._run(view)
        self.assertIsNone(recorder.source_item().grade)

    def test_an_answer_only_job_is_written_as_an_answer_source(self):
        jid, view = self._job()
        view["blocks"] = [block for block in view["blocks"]
                          if block["kind"] == "answer_key_entry"]
        report, recorder = self._run(view)
        self.assertEqual(report["questions"], 0)
        self.assertEqual(report["answers"], 1)
        self.assertEqual(recorder.source_item().type, TYPE_DIRS["answer"])

    def test_a_dry_run_writes_nothing(self):
        jid, view = self._job()
        report, recorder = self._run(view, dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(recorder.calls, [])


if __name__ == "__main__":
    unittest.main()
