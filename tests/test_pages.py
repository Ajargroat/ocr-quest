"""Offline tests for pipeline.pages — page rendering and the figure rule.

Pages are built with pypdfium2 in memory (no fixture file, no network): the
figure rule is exercised through a real pdfium page so the object types and
coordinates are the ones the OCR actually meets.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import pages  # noqa: E402


def _page(width=600, height=800, image_rect=None):
    import io

    import pypdfium2 as pdfium
    from PIL import Image

    doc = pdfium.PdfDocument.new()
    page = doc.new_page(width, height)
    if image_rect:
        buf = io.BytesIO()
        Image.new("RGB", (4, 4), (255, 255, 255)).save(buf, "JPEG")
        image = pdfium.PdfImage.new(doc)
        image.load_jpeg(io.BytesIO(buf.getvalue()))
        left, bottom, right, top = image_rect
        matrix = (pdfium.PdfMatrix()
                  .translate(left, bottom)
                  .scale(right - left, top - bottom))
        image.set_matrix(matrix)
        page.insert_obj(image)
        page.gen_content()
    return doc, page

class FigureRuleTests(unittest.TestCase):
    def tearDown(self):
        doc = getattr(self, "doc", None)
        if doc is not None:
            doc.close()

    def test_a_page_without_images_has_no_figures(self):
        self.doc, page = _page()
        self.assertEqual(pages.figure_bounds(page), [])

    def test_a_real_picture_counts_as_a_figure(self):
        # ~12% x 12% of the page: a real picture, not a banner or a sliver
        self.doc, page = _page(image_rect=(100, 500, 180, 580))
        self.assertEqual(len(pages.figure_bounds(page)), 1)

    def test_a_tiny_mark_is_not_a_figure(self):
        self.doc, page = _page(image_rect=(10, 10, 18, 18))
        self.assertEqual(pages.figure_bounds(page), [])

    def test_a_page_filling_scan_is_not_a_figure(self):
        self.doc, page = _page(image_rect=(0, 0, 600, 800))
        self.assertEqual(pages.figure_bounds(page), [])


class RenderTests(unittest.TestCase):
    def test_render_is_rgb_and_follows_the_dpi(self):
        doc, page = _page(600, 800)
        try:
            image = pages.render(page, dpi=72)
            self.assertEqual(image.mode, "RGB")
            self.assertEqual(image.size, (600, 800))
            big = pages.render(page, dpi=144)
            self.assertEqual(big.size, (1200, 1600))
        finally:
            doc.close()

    def test_fit_long_edge_never_upscales(self):
        doc, page = _page(600, 800)
        try:
            image = pages.render(page, dpi=72)
            self.assertEqual(pages.fit_long_edge(image, 4000).size, (600, 800))
            self.assertEqual(pages.fit_long_edge(image, 400).size, (300, 400))
        finally:
            doc.close()

    def test_a_bad_file_is_a_named_error(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(b"not a pdf")
            path = fh.name
        try:
            with self.assertRaises(ValueError):
                pages.open_pdf(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
