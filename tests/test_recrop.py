"""QBank picture editing: re-cut a stored crop from its source page.

The Q12 rule under test: a corrected box replaces the crop's OWN object id and
lands in the DB in the same call, so no variants accumulate. No network, no
database, no bucket - the page fetch and the upload are stubbed.
"""
import io
import os
import sys
import types
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pipeline import recrop


def _page_jpeg(width=400, height=600):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(buf, "JPEG")
    return buf.getvalue()


class CleanBboxTest(unittest.TestCase):
    def test_accepts_four_numbers_in_range(self):
        self.assertEqual(recrop.clean_bbox([1, 2, 300, 400]), [1, 2, 300, 400])
        self.assertEqual(recrop.clean_bbox(["10", "20", "30.5", "40.25"]),
                         [10, 20, 30.5, 40.25])

    def test_rejects_a_wrong_shape(self):
        for bad in ([1, 2, 3], [1, 2, 3, 4, 5], None, "1234", ["a", 1, 2, 3]):
            with self.assertRaises(ValueError):
                recrop.clean_bbox(bad)

    def test_rejects_out_of_range_and_nan(self):
        for bad in ([-1, 0, 100, 100], [0, 0, 1001, 100],
                    [float("nan"), 0, 10, 10]):
            with self.assertRaises(ValueError):
                recrop.clean_bbox(bad)

    def test_rejects_an_inverted_or_empty_box(self):
        for bad in ([500, 0, 500, 100], [0, 500, 100, 500]):
            with self.assertRaises(ValueError):
                recrop.clean_bbox(bad)


class PixelsTest(unittest.TestCase):
    def test_maps_the_0_1000_frame_onto_the_page(self):
        self.assertEqual(recrop.pixels([100, 200, 500, 800], 400, 600),
                         (80, 60, 320, 300))

    def test_clamps_to_the_page_edges(self):
        x0, y0, x1, y1 = recrop.pixels([0, 0, 1000, 1000], 400, 600)
        self.assertEqual((x0, y0, x1, y1), (0, 0, 400, 600))


class CutTest(unittest.TestCase):
    def test_cuts_the_box_out_of_the_page(self):
        data, width, height = recrop.cut(_page_jpeg(), [100, 200, 500, 800])
        self.assertTrue(data.startswith(b"\xff\xd8"))   # JPEG SOI
        self.assertEqual((width, height), (240, 240))

    def test_refuses_a_box_that_is_too_small(self):
        # 5/1000 of 400 px = 2 px wide
        with self.assertRaises(ValueError):
            recrop.cut(_page_jpeg(), [100, 200, 105, 800])

    def test_refuses_a_page_that_is_not_an_image(self):
        with self.assertRaises(ValueError):
            recrop.cut(b"not a jpeg", [0, 0, 500, 500])


class PageBytesTest(unittest.TestCase):
    def setUp(self):
        recrop.clear_cache()
        self.addCleanup(recrop.clear_cache)

    def test_fetches_once_and_memoises(self):
        resp = types.SimpleNamespace(status_code=200, content=_page_jpeg())
        with mock.patch.object(recrop.requests, "get", return_value=resp) as get:
            first = recrop.page_bytes("https://x/page.jpg")
            second = recrop.page_bytes("https://x/page.jpg")
        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 1)

    def test_a_missing_page_is_a_lookup_error(self):
        resp = types.SimpleNamespace(status_code=404, content=b"")
        with mock.patch.object(recrop.requests, "get", return_value=resp):
            with self.assertRaises(LookupError):
                recrop.page_bytes("https://x/gone.jpg")

    def test_no_url_is_a_lookup_error(self):
        with self.assertRaises(LookupError):
            recrop.page_bytes("")


class _FakeDb:
    def __init__(self, row):
        self.row = row
        self.bbox_calls = []

    def fetch_media_row(self, question_id):
        return self.row

    def update_media_bbox(self, question_id, index, bbox):
        self.bbox_calls.append((question_id, index, bbox))


def _row(object_name="crops/abc-q41.jpg", url="https://x/page.jpg"):
    return {"id": "41", "legacy_id": "abc-q41", "source_id": "abc",
            "media": [{"object": object_name, "bbox": [10, 10, 20, 20],
                       "page": 1}],
            "source_url": url}


class RecropTest(unittest.TestCase):
    def setUp(self):
        recrop.clear_cache()
        self.addCleanup(recrop.clear_cache)

    def test_replaces_the_same_object_and_writes_the_box(self):
        db = _FakeDb(_row())
        upload = mock.Mock(return_value="https://cdn/crops/abc-q41.jpg")
        with mock.patch.object(recrop, "page_bytes", return_value=_page_jpeg()), \
                mock.patch.object(recrop.storage, "upload_file", upload):
            out = recrop.recrop(types.SimpleNamespace(), db, "41", 0,
                                [100, 200, 500, 800])
        self.assertEqual(out["object"], "crops/abc-q41.jpg")
        self.assertEqual(out["bbox"], [100, 200, 500, 800])
        self.assertEqual((out["width"], out["height"]), (240, 240))
        self.assertIn("object=crops%2Fabc-q41.jpg", out["url"])
        self.assertIn("&v=", out["url"])
        # the SAME object id - never a variant
        self.assertEqual(upload.call_args[0][1], "crops/abc-q41.jpg")
        self.assertEqual(upload.call_args[0][3], "image/jpeg")
        self.assertEqual(db.bbox_calls, [("41", 0, [100, 200, 500, 800])])

    def test_a_page_object_is_not_re_cut(self):
        db = _FakeDb(_row(object_name="https://x/page.jpg"))
        with mock.patch.object(recrop.storage, "upload_file") as upload:
            with self.assertRaises(ValueError):
                recrop.recrop(types.SimpleNamespace(), db, "41", 0,
                              [0, 0, 500, 500])
        upload.assert_not_called()
        self.assertEqual(db.bbox_calls, [])

    def test_an_out_of_range_index_is_a_value_error(self):
        db = _FakeDb({"media": [], "source_url": "https://x/page.jpg"})
        with self.assertRaises(ValueError):
            recrop.recrop(types.SimpleNamespace(), db, "41", 3,
                          [0, 0, 500, 500])

    def test_a_missing_row_is_a_lookup_error(self):
        with self.assertRaises(LookupError):
            recrop.recrop(types.SimpleNamespace(), _FakeDb(None), "41", 0,
                          [0, 0, 500, 500])

    def test_a_page_without_a_source_url_is_a_lookup_error(self):
        db = _FakeDb(_row(url=None))
        with self.assertRaises(LookupError):
            recrop.recrop(types.SimpleNamespace(), db, "41", 0,
                          [0, 0, 500, 500])
        self.assertEqual(db.bbox_calls, [])

    def test_a_bad_box_never_reaches_the_bucket(self):
        db = _FakeDb(_row())
        with mock.patch.object(recrop, "page_bytes",
                               return_value=_page_jpeg()) as page, \
                mock.patch.object(recrop.storage, "upload_file") as upload:
            with self.assertRaises(ValueError):
                recrop.recrop(types.SimpleNamespace(), db, "41", 0, [5, 5, 5, 5])
        page.assert_not_called()
        upload.assert_not_called()


class RouteTest(unittest.TestCase):
    def test_the_two_qbank_routes_exist(self):
        with io.open(os.path.join(ROOT, "main.py"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertIn('@app.get("/api/qbank/page")', src)
        self.assertIn('@app.post("/api/qbank/recrop")', src)
        # the DB write and the upload stay inside the recrop module
        self.assertIn("recrop.recrop, cfg, db, body.id", src)


if __name__ == "__main__":
    unittest.main()
