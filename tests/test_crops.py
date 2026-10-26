"""Offline tests for pipeline.crops — the bbox contract and crop naming.

No network, no bucket, no database: the vectors are the cross-language ones
(Python writes the box, the dashboard's JS draws and edits it, Supabase reads
``[ymin, xmin, ymax, xmax]``) fixed here so a change in any of them breaks a
test instead of a crop.
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PIL import Image  # noqa: E402

from pipeline import crops  # noqa: E402


def page(width=1000, height=1400):
    image = Image.new("RGB", (width, height), (255, 255, 255))
    return image


class BoxContractTests(unittest.TestCase):
    def test_order_is_ymin_xmin_ymax_xmax(self):
        self.assertEqual(crops.normalize([100, 50, 300, 900]),
                         [100, 50, 300, 900])

    def test_values_are_clamped_to_the_scale(self):
        self.assertEqual(crops.normalize([-20, -5, 1200, 2000]),
                         [0, 0, 1000, 1000])

    def test_a_reversed_box_is_repaired_not_refused(self):
        self.assertEqual(crops.normalize([300, 900, 100, 50]),
                         [100, 50, 300, 900])

    def test_degenerate_shapes_are_none(self):
        for raw in (None, [], [1, 2, 3], [0, 0, 0, 0], [10, 10, 12, 900],
                    ["a", 1, 2, 3], [10, 10, 10, 900], "not json"):
            self.assertIsNone(crops.normalize(raw), raw)
        self.assertFalse(crops.is_valid([10, 10, 10, 900]))

    def test_a_json_string_box_is_accepted(self):
        self.assertEqual(crops.normalize("[10, 20, 30, 40]"), [10, 20, 30, 40])

    def test_union_of_two_boxes(self):
        self.assertEqual(crops.merge([0, 0, 100, 100], [50, 200, 300, 400]),
                         [0, 0, 300, 400])
        self.assertIsNone(crops.merge(None, [0, 0, 0, 0]))

    def test_pixels_and_back_round_trip(self):
        size = (2000, 2800)
        rect = crops.to_pixels([100, 50, 600, 950], *size)
        self.assertEqual(rect, (100, 280, 1900, 1680))
        back = crops.to_norm(rect, *size)
        self.assertEqual(back, [100, 50, 600, 950])

    def test_pixels_are_clipped_to_the_page(self):
        self.assertEqual(crops.to_pixels([0, 0, 1000, 1000], 100, 200),
                         (0, 0, 100, 200))

    def test_a_box_too_small_for_pixels_is_none(self):
        # 8/1000 of a 60 px page is under half a pixel
        self.assertIsNone(crops.to_pixels([10, 10, 18, 18], 60, 60))


class CropNameTests(unittest.TestCase):
    SOURCE = "3a181898d0de49a90248d4563fc14fb5b30f4675"

    def test_the_name_is_deterministic_and_folder_scoped(self):
        key = crops.crop_key(self.SOURCE, "q", 12, 1)
        self.assertEqual(key, "crops/3a181898d0de49a90248d4563fc14fb5b30f4675/q12-1.jpg")
        self.assertEqual(key, crops.crop_key(self.SOURCE, "q", 12, 1))

    def test_question_and_answer_crops_never_collide(self):
        self.assertNotEqual(crops.crop_key(self.SOURCE, "q", 3),
                            crops.crop_key(self.SOURCE, "a", 3))

    def test_a_path_like_source_is_sanitised(self):
        key = crops.crop_key("../../etc/passwd", "q", 1)
        self.assertTrue(key.startswith("crops/"))
        self.assertNotIn("..", key)

    def test_public_name_strips_the_bucket_prefix(self):
        url = ("https://x.supabase.co/storage/v1/object/public/konkour-pages/"
               "crops/abc/q1-0.jpg")
        self.assertEqual(crops.public_name(url), "crops/abc/q1-0.jpg")
        self.assertEqual(crops.public_name("crops/abc/q1-0.jpg"), "crops/abc/q1-0.jpg")


class RenderTests(unittest.TestCase):
    def test_render_cuts_the_box(self):
        data = crops.render(page(1000, 1400), [0, 0, 500, 500])
        image = Image.open(io.BytesIO(data))
        self.assertEqual(image.size, (500, 700))

    def test_render_refuses_a_degenerate_box(self):
        self.assertIsNone(crops.render(page(), [0, 0, 0, 0]))

    def test_inspect_accepts_a_real_crop_and_rejects_junk(self):
        data = crops.render(page(), [10, 10, 200, 200])
        self.assertEqual(crops.inspect(data, [10, 10, 200, 200]), (True, ""))
        ok, reason = crops.inspect(b"not an image", [10, 10, 200, 200])
        self.assertFalse(ok)
        self.assertIn("readable", reason)
        ok, reason = crops.inspect(data, [0, 0, 0, 0])
        self.assertFalse(ok)
        self.assertIn("bbox", reason)


if __name__ == "__main__":
    unittest.main()
