"""Scanner layout tests.

Run them either way:
    python tests/test_scanner.py
    pytest tests/test_scanner.py        (if pytest is installed)
"""
import json
import os
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.config import DEFAULT_INPUT_ROOT         # noqa: E402
from pipeline.scanner import scan                        # noqa: E402
from pipeline.utils import sha1_hex                      # noqa: E402


def make_cfg(root, min_age_seconds=0):
    # scan() only reads these two settings.
    return SimpleNamespace(input_root=root, min_age_seconds=min_age_seconds)


class ScanTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.warnings = []

    def put(self, rel_path, age_seconds=600, meta=None):
        """Create an input file, optionally with a meta.json next to it."""
        path = os.path.join(self.root, *rel_path.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"fake page bytes")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))
        if meta is not None:
            meta_path = os.path.join(os.path.dirname(path), "meta.json")
            with open(meta_path, "w", encoding="utf-8") as fh:
                fh.write(meta if isinstance(meta, str) else json.dumps(meta))
        return path

    def run_scan(self, min_age_seconds=0):
        return scan(make_cfg(self.root, min_age_seconds), warn=self.warnings.append)


class TestAcceptedLayouts(ScanTestCase):
    def test_grade_level_layout(self):
        self.put("physic/10/chapter1/question/q1.jpg",
                 meta={"subject": "فیزیک", "type": "سوال"})
        item = self.run_scan()[0]
        # Folder names stay authoritative even though meta.json carries labels.
        self.assertEqual((item.subject, item.grade, item.topic, item.type),
                         ("physic", 10, "chapter1", "سوال"))
        self.assertEqual(item.source_id,
                         sha1_hex("physic/10/chapter1/question/q1.jpg"))

    def test_layout_without_grade_level_is_scanned(self):
        """Regression: the riazi/zamin trees used to raise and abort the run."""
        self.put("riazi/amar/question/q1.jpg", meta={"type": "سوال"})
        item = self.run_scan()[0]
        self.assertEqual((item.subject, item.grade, item.topic), ("riazi", None, "amar"))
        self.assertEqual(self.warnings, [])

    def test_non_numeric_grade_is_kept_as_text(self):
        self.put("physic/10th/chapter1/question/q1.jpg")
        self.assertEqual(self.run_scan()[0].grade, "10th")

    def test_answer_folder_maps_to_persian_type(self):
        self.put("zamin/chapter1/answer/a1.png")
        self.assertEqual(self.run_scan()[0].type, "پاسخ")


class TestMetaJsonIsOptional(ScanTestCase):
    def test_folder_without_meta_json_is_still_scanned(self):
        """Regression: meta.json used to be mandatory, so scaffolded folders
        without it were silently invisible to the pipeline."""
        self.put("shimi/12/chapter3/question/q1.pdf")
        items = self.run_scan()
        self.assertEqual([item.type for item in items], ["سوال"])
        self.assertEqual(self.warnings, [])

    def test_meta_json_with_bom_is_parsed(self):
        """Real data check: PowerShell writes every meta.json with a UTF-8 BOM."""
        self.put("shimi/11/chapter2/question/q1.jpg",
                 meta="\ufeff" + json.dumps({"type": "پاسخ"}))
        items = self.run_scan()
        self.assertEqual([item.type for item in items], ["پاسخ"])
        self.assertEqual(self.warnings, [])

    def test_broken_meta_json_warns_without_losing_the_file(self):
        self.put("shimi/12/chapter3/question/q1.pdf", meta="{not json")
        items = self.run_scan()
        self.assertEqual([item.type for item in items], ["سوال"])
        self.assertEqual(len(self.warnings), 1)
        self.assertIn("meta.json", self.warnings[0])


class TestBadFoldersAreReportedNotFatal(ScanTestCase):
    def test_unsupported_depths_are_skipped_with_a_warning(self):
        self.put("physic/10/chapter1/question/good.jpg")
        self.put("physic/10/chapter1/extra/question/too_deep.jpg")
        self.put("question/too_shallow.jpg")

        items = self.run_scan()

        self.assertEqual([item.file_name for item in items], ["good.jpg"])
        self.assertEqual(len(self.warnings), 2)
        self.assertTrue(any("too_deep.jpg" in w for w in self.warnings))
        self.assertTrue(any("too_shallow.jpg" in w for w in self.warnings))

    def test_run_continues_after_a_bad_folder(self):
        """A malformed folder must never cost the rest of the batch."""
        self.put("physic/10/a/b/question/bad.jpg")
        self.put("zist/11/chapter2/question/good.jpg")
        self.assertEqual([i.file_name for i in self.run_scan()], ["good.jpg"])

    def test_files_nested_below_a_type_folder_are_ignored(self):
        self.put("riazi/amar/question/q1.jpg")
        self.put("riazi/amar/answer/sub/key1.jpg")
        self.assertEqual([i.file_name for i in self.run_scan()], ["q1.jpg"])


class TestIgnoredInput(ScanTestCase):
    def test_done_and_failed_archives_are_skipped(self):
        self.put("physic/10/chapter1/question/live.jpg")
        self.put("physic/10/chapter1/question/done/old.pdf")
        self.put("physic/10/chapter1/question/failed/broken.pdf")
        self.assertEqual([i.file_name for i in self.run_scan()], ["live.jpg"])
        self.assertEqual(self.warnings, [])

    def test_unsupported_extension_is_ignored(self):
        self.put("physic/10/chapter1/question/notes.txt")
        self.assertEqual(self.run_scan(), [])

    def test_empty_type_folder_produces_no_noise(self):
        os.makedirs(os.path.join(self.root, "physic", "10", "chapter1", "question"))
        self.assertEqual(self.run_scan(), [])
        self.assertEqual(self.warnings, [])

    def test_recently_written_file_is_ignored(self):
        self.put("physic/10/chapter1/question/writing.jpg", age_seconds=0)
        self.assertEqual(self.run_scan(min_age_seconds=3600), [])
        self.assertEqual(len(self.run_scan(min_age_seconds=0)), 1)


class TestOrdering(ScanTestCase):
    def test_questions_come_first_so_answers_can_link(self):
        self.put("physic/10/chapter1/answer/a1.jpg")
        self.put("physic/10/chapter2/question/q1.jpg")
        self.assertEqual([i.type for i in self.run_scan()], ["سوال", "پاسخ"])


class TestRealProjectTree(ScanTestCase):
    """Smoke check the repository's own konkour-ocr/ tree."""

    def test_shipped_tree_scans_without_warnings(self):
        if not os.path.isdir(DEFAULT_INPUT_ROOT):
            self.skipTest(f"{DEFAULT_INPUT_ROOT} is not present")

        items = scan(make_cfg(DEFAULT_INPUT_ROOT, 0), warn=self.warnings.append)

        self.assertEqual(self.warnings, [], "shipped tree has unusable folder shapes")
        for item in items:
            parts = item.rel_path.split("/")
            self.assertIn(parts[0], ("physic", "riazi", "shimi", "zamin", "zist"))
            self.assertIn(parts[-2], ("question", "answer"))
            self.assertIn(len(parts), (4, 5))          # with or without a grade level
            self.assertTrue(os.path.isfile(item.file_path))
            self.assertNotIn("/done/", item.rel_path)
            self.assertNotIn("/failed/", item.rel_path)
            self.assertEqual(item.source_id, sha1_hex(item.rel_path))
            expected = "سوال" if parts[-2] == "question" else "پاسخ"
            self.assertEqual(item.type, expected)


if __name__ == "__main__":
    unittest.main(verbosity=2)
