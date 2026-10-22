"""Offline tests for the in-app PDF upload queue (pipeline.uploads).

P2: bad destinations rejected; good uploads land at <input_root>/<dest>/;
cancel removes pending; queue order is FIFO. No network, no DB.

Run them either way:
    python tests/test_uploads.py
    pytest tests/test_uploads.py
"""
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import uploads


def _cfg(root):
    return SimpleNamespace(input_root=root, min_age_seconds=60)


class UploadQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        uploads.reset_for_tests()

    def tearDown(self):
        self._tmp.cleanup()
        uploads.reset_for_tests()

    def _file(self, name="doc.pdf"):
        return io.BytesIO(b"%PDF-1.4 fake body"), name

    def test_bad_destination_is_rejected(self):
        cfg = _cfg(self._tmp.name)
        for bad in ("", "a/b/c", "a/b/c/d/e", "a/b/c/other",
                    "../x/y/question", "a/b/c/done"):
            body, name = self._file()
            with self.assertRaises(ValueError, msg=bad):
                uploads.enqueue(cfg, body, name, bad)
        self.assertEqual(uploads.list_items(), [])

    def test_good_upload_lands_under_dest(self):
        cfg = _cfg(self._tmp.name)
        body, _ = self._file("report.pdf")
        item = uploads.enqueue(cfg, body, "report.pdf",
                               "physics/12/mech/question")
        self.assertEqual(item["status"], "queued")
        target_dir = os.path.join(self._tmp.name, "physics", "12",
                                  "mech", "question")
        self.assertTrue(os.path.isdir(target_dir))
        stored = os.path.join(target_dir, item["stored"])
        self.assertTrue(os.path.exists(stored))
        self.assertIn(item["id"], [i["id"] for i in uploads.list_items()])

    def test_non_pdf_is_rejected(self):
        cfg = _cfg(self._tmp.name)
        body, _ = self._file("notes.txt")
        with self.assertRaises(ValueError):
            uploads.enqueue(cfg, body, "notes.txt", "a/b/c/question")

    def test_cancel_removes_pending(self):
        cfg = _cfg(self._tmp.name)
        body, _ = self._file()
        item = uploads.enqueue(cfg, body, "x.pdf", "a/b/c/answer")
        got = uploads.cancel(item["id"])
        self.assertIsNotNone(got)
        self.assertEqual(got["status"], "cancelled")
        self.assertEqual(uploads.list_items(), [])
        self.assertIsNone(uploads.cancel("no-such-id"))

    def test_queue_order_is_fifo(self):
        cfg = _cfg(self._tmp.name)
        ids = []
        for n in ("one.pdf", "two.pdf", "three.pdf"):
            body, _ = self._file(n)
            ids.append(uploads.enqueue(
                cfg, body, n, "a/b/c/question")["id"])
        self.assertEqual([i["id"] for i in uploads.list_items()], ids)

    def test_done_or_extracting_cannot_be_cancelled(self):
        cfg = _cfg(self._tmp.name)
        body, _ = self._file()
        item = uploads.enqueue(cfg, body, "x.pdf", "a/b/c/question")
        uploads.mark(item["id"], "extracting")
        self.assertIsNone(uploads.cancel(item["id"]))
        uploads.mark(item["id"], "done")
        self.assertEqual(uploads.list_items(), [])


if __name__ == "__main__":
    unittest.main()
