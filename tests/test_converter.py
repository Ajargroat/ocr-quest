"""Offline tests for the embedded pure-Python converter (pipeline.converter).

P1: mature PDFs render to JPEGs and move to done/; fresh PDFs (< min_age)
are untouched; render errors move to failed/; done/failed subtrees are
never scanned.

Render is stubbed (_render_pdf) — these tests pin the sweep/archive
behaviour, not pypdfium2 itself.

Run them either way:
    python tests/test_converter.py
    pytest tests/test_converter.py
"""
import os
import random
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import converter


def _cfg(root, min_age=60):
    return SimpleNamespace(input_root=root, min_age_seconds=min_age)


class ConverterSweepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._real_render = converter._render_pdf
        converter.reset_for_tests()

    def tearDown(self):
        converter._render_pdf = self._real_render
        self._tmp.cleanup()

    def _pdf(self, name, old=True):
        path = os.path.join(self._tmp.name, name)
        with open(path, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        stamp = time.time() - 3600 if old else time.time()
        os.utime(path, (stamp, stamp))
        return path

    def test_mature_pdf_moves_to_done(self):
        """A mature PDF renders to JPEGs and is archived under done/."""
        made = []

        def fake_render(pdf_path, out_base, dpi, quality):
            made.append((pdf_path, out_base))
            with open(out_base + "-1.jpg", "wb") as fh:
                fh.write(b"\xff\xd8fake")

        converter._render_pdf = fake_render
        pdf = self._pdf("a.pdf")
        converted, failed = converter._sweep_once(_cfg(self._tmp.name))
        self.assertEqual((converted, failed), (1, 0))
        self.assertTrue(os.path.exists(
            os.path.join(self._tmp.name, "done", "a.pdf")))
        self.assertFalse(os.path.exists(pdf))
        self.assertTrue(os.path.exists(os.path.join(self._tmp.name, "a-1.jpg")))

    def test_fresh_pdf_is_untouched(self):
        """A PDF younger than min_age is never picked up (MIN_AGE gate)."""
        calls = []
        converter._render_pdf = lambda *a: calls.append(a)
        pdf = self._pdf("fresh.pdf", old=False)
        converted, failed = converter._sweep_once(_cfg(self._tmp.name))
        self.assertEqual((converted, failed), (0, 0))
        self.assertEqual(calls, [])
        self.assertTrue(os.path.exists(pdf))

    def test_render_error_moves_to_failed(self):
        """A PDF that fails rendering lands in failed/, not done/."""
        def boom(*a):
            raise ValueError("not a pdf")

        converter._render_pdf = boom
        self._pdf("bad.pdf")
        converted, failed = converter._sweep_once(_cfg(self._tmp.name))
        self.assertEqual((converted, failed), (0, 1))
        self.assertTrue(os.path.exists(
            os.path.join(self._tmp.name, "failed", "bad.pdf")))

    def test_done_and_failed_subtrees_are_never_scanned(self):
        """Archived PDFs are terminal — the sweep prunes done/failed."""
        calls = []
        converter._render_pdf = lambda *a: calls.append(a)
        for sub in ("done", "failed"):
            d = os.path.join(self._tmp.name, sub)
            os.makedirs(d)
            with open(os.path.join(d, "old.pdf"), "wb") as fh:
                fh.write(b"%PDF-1.4 fake")
            old = time.time() - 3600
            os.utime(os.path.join(d, "old.pdf"), (old, old))
        converted, failed = converter._sweep_once(_cfg(self._tmp.name))
        self.assertEqual((converted, failed), (0, 0))
        self.assertEqual(calls, [])

    def test_raster_quality_key_is_the_only_quality_source(self):
        """CONVERTER_JPEG_QUALITY is gone; CONVERTER_RASTER_QUALITY rules."""
        calls = []

        def fake_render(pdf_path, out_base, dpi, quality):
            calls.append(quality)
            with open(out_base + "-1.jpg", "wb") as fh:
                fh.write(b"\xff\xd8fake")

        converter._render_pdf = fake_render
        os.environ["CONVERTER_JPEG_QUALITY"] = "10"
        os.environ["CONVERTER_RASTER_QUALITY"] = "78"
        try:
            self._pdf("q.pdf")
            converter._sweep_once(_cfg(self._tmp.name))
        finally:
            os.environ.pop("CONVERTER_JPEG_QUALITY", None)
            os.environ.pop("CONVERTER_RASTER_QUALITY", None)
        self.assertEqual(calls, [78])

    def test_mark_upload_with_a_twelve_char_uid_does_not_raise(self):
        """Upload-tracked uids used to hit a NameError (scope bug)."""
        converter._mark_upload("abcdefghijkl", "done", "ok")


class PageCapTests(unittest.TestCase):
    def _noise(self, w, h, seed=7):
        """Deterministic mid-grey noise — JPEG-incompressible filler."""
        rnd = random.Random(seed)
        data = bytearray(rnd.randbytes(w * h))
        for i, v in enumerate(data):
            data[i] = 96 + (v >> 2)
        return Image.frombytes("L", (w, h), bytes(data)).convert("RGB")

    def test_page_is_capped_at_200kb(self):
        """_save_capped walks the quality ladder down until the file fits."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p-1.jpg")
            q = converter._save_capped(self._noise(1000, 1400), path, 78)
            self.assertLessEqual(os.path.getsize(path), converter.PAGE_SIZE_CAP)
            self.assertLess(q, 78)
            self.assertGreaterEqual(q, converter.QUALITY_FLOOR)

    def test_small_page_keeps_the_start_quality(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p-1.jpg")
            q = converter._save_capped(Image.new("RGB", (64, 64), "white"), path, 78)
            self.assertEqual(q, 78)

    def test_ladder_stops_at_the_floor(self):
        """A pathological page bottoms out at QUALITY_FLOOR, never below."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p-1.jpg")
            q = converter._save_capped(self._noise(2000, 2800, seed=11), path, 78)
            self.assertEqual(q, converter.QUALITY_FLOOR)

    def test_multi_page_pdf_renders_all_pages(self):
        """The threaded path (RENDER_WORKERS > 1) renders every page."""
        import pypdfium2 as pdfium
        src = pdfium.PdfDocument.new()
        src.new_page(595, 842)
        src.new_page(595, 842)
        with tempfile.TemporaryDirectory() as d:
            pdf = os.path.join(d, "multi.pdf")
            src.save(pdf)
            src.close()
            out_base = os.path.join(d, "multi")
            count = converter._render_pdf(pdf, out_base, 170, 78)
            self.assertEqual(count, 2)
            for i in (1, 2):
                path = f"{out_base}-{i}.jpg"
                self.assertTrue(os.path.exists(path))
                self.assertLessEqual(os.path.getsize(path), converter.PAGE_SIZE_CAP)


if __name__ == "__main__":
    unittest.main()
