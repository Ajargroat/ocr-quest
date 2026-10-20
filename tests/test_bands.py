import unittest

from PIL import Image

from pipeline.studio import bands


class BandCutTests(unittest.TestCase):
    def test_cut_pads_the_question_and_clamps_to_the_page(self):
        page = Image.new("RGB", (1000, 2000))
        band = bands.cut(page, [100, 200, 500, 700], "p1-b1", 1)
        self.assertIsNotNone(band)
        self.assertEqual(band.rect, (160, 150, 740, 1050))
        self.assertEqual(band.page, 1)
        self.assertEqual(band.width, 580)
        self.assertEqual(band.height, 900)

    def test_a_wide_question_gets_the_full_page_width(self):
        page = Image.new("RGB", (1000, 2000))
        band = bands.cut(page, [100, 200, 500, 900], "p1-b1", 1)
        self.assertEqual(band.rect[0], 0)
        self.assertEqual(band.rect[2], 1000)

    def test_a_degenerate_box_gets_no_band(self):
        page = Image.new("RGB", (1000, 2000))
        self.assertIsNone(bands.cut(page, [0, 0, 4, 4], "p1-b1", 1))
        self.assertIsNone(bands.cut(page, None, "p1-b1", 1))

    def test_band_coordinates_map_back_exactly(self):
        band = bands.Band("p1-b1", 1, (200, 400, 800, 1600), 1000, 2000)
        self.assertEqual(band.to_page([0, 0, 1000, 1000]), [200, 200, 800, 800])
        self.assertEqual(band.to_page([500, 500, 600, 600]), [500, 500, 560, 560])
        self.assertIsNone(band.to_page([0, 0, 2, 2]))

    def test_jpeg_bytes_are_a_jpeg(self):
        page = Image.new("RGB", (1000, 2000), (250, 250, 250))
        band = bands.cut(page, [100, 200, 500, 700], "p1-b1", 1)
        data = bands.jpeg_bytes(page, band, 88)
        self.assertTrue(data.startswith(b"\xff\xd8"))
        self.assertGreater(len(data), 100)


class BandQaTests(unittest.TestCase):
    def setUp(self):
        self.band = bands.Band("p1-b1", 1, (0, 400, 1000, 1600), 1000, 2000)

    def test_a_centred_figure_passes(self):
        ok, reason = bands.qa([100, 200, 800, 700], self.band)
        self.assertTrue(ok, reason)

    def test_a_vertical_edge_means_the_cut_clipped_it(self):
        ok, reason = bands.qa([0, 200, 500, 700], self.band)
        self.assertFalse(ok)
        self.assertEqual(reason, "vertical-edge")

    def test_a_whole_band_is_the_question_not_the_picture(self):
        ok, reason = bands.qa([0, 0, 999, 999], self.band)
        self.assertFalse(ok)
        self.assertIn(reason, ("whole-band", "vertical-edge"))

    def test_a_tiny_box_is_noise(self):
        ok, reason = bands.qa([100, 100, 104, 104], self.band)
        self.assertFalse(ok)
        self.assertEqual(reason, "degenerate")

    def test_a_full_width_band_may_touch_both_sides(self):
        wide = bands.Band("p1-b1", 1, (0, 400, 1000, 1600), 1000, 2000)
        ok, reason = bands.qa([100, 0, 500, 990], wide)
        self.assertTrue(ok, reason)


if __name__ == "__main__":
    unittest.main(verbosity=2)
