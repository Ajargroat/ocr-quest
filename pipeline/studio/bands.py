"""Question-band cropping and band-to-page coordinate mapping.

The figure pass cuts a horizontal slice of the page around each pictured
question and sends several of those slices as separate image parts in one
request. The model answers in 0-1000 coordinates *relative to each band*, so
this module owns the only two things that can go wrong there: the cut and the
mapping back to the page frame.

A band is deliberately not a composite sheet. Each band keeps its own scale
and its own retry, and a bad cut can be re-cut without touching its neighbours.
"""
import io
from dataclasses import dataclass

MIN_NORM = 8

# Padding around a question box, in page-normalized units (0-1000). Vertical
# padding gives the figure room when the question box ends just above it;
# horizontal padding keeps a full-width figure from being clipped.
PAD_Y = 25
PAD_X = 40

# A question wider than this already spans most of the page; widening the band
# to the full page costs nothing and avoids cutting a wide figure.
WIDE_QUESTION = 560


def clamp(value):
    return max(0, min(1000, int(value)))


def clean_box(raw):
    """A model box -> four clamped ints, or None when degenerate."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        y0, x0, y1, x1 = (int(round(float(v))) for v in raw)
    except (TypeError, ValueError):
        return None
    y0, x0, y1, x1 = clamp(y0), clamp(x0), clamp(y1), clamp(x1)
    if y1 - y0 < MIN_NORM or x1 - x0 < MIN_NORM:
        return None
    return [y0, x0, y1, x1]


@dataclass
class Band:
    """One question slice, in page pixels, plus the page it came from."""

    key: str
    page: int
    rect: tuple          # (x0, y0, x1, y1) in page pixels
    page_w: int
    page_h: int

    @property
    def width(self):
        return self.rect[2] - self.rect[0]

    @property
    def height(self):
        return self.rect[3] - self.rect[1]

    def crop(self, page_image):
        return page_image.crop(self.rect)

    def to_band(self, box):
        """Page-relative 0-1000 box -> band-relative 0-1000 box.

        The inverse of ``to_page``; used to run the band QA on a lane that
        answered in page coordinates instead of band coordinates.
        """
        clean = clean_box(box)
        if not clean:
            return None
        y0, x0, y1, x1 = clean
        rx0, ry0, rx1, ry1 = self.rect
        if ry1 <= ry0 or rx1 <= rx0:
            return None

        def by(value):
            px = value / 1000.0 * self.page_h
            return clamp(round((px - ry0) / float(ry1 - ry0) * 1000.0))

        def bx(value):
            px = value / 1000.0 * self.page_w
            return clamp(round((px - rx0) / float(rx1 - rx0) * 1000.0))

        return [by(y0), bx(x0), by(y1), bx(x1)]

    def to_page(self, box):
        """Band-relative 0-1000 box -> page-relative 0-1000 box."""
        clean = clean_box(box)
        if not clean:
            return None
        y0, x0, y1, x1 = clean
        rx0, ry0, rx1, ry1 = self.rect

        def py(value):
            px = ry0 + (value / 1000.0) * (ry1 - ry0)
            return clamp(round(px / float(self.page_h) * 1000.0))

        def px(value):
            p = rx0 + (value / 1000.0) * (rx1 - rx0)
            return clamp(round(p / float(self.page_w) * 1000.0))

        return [py(y0), px(x0), py(y1), px(x1)]


def cut(page_image, bbox, key, page_no, pad_y=PAD_Y, pad_x=PAD_X):
    """Cut one question band off a page render, or None when it cannot exist."""
    box = clean_box(bbox)
    if not box:
        return None
    y0, x0, y1, x1 = box
    if x1 - x0 >= WIDE_QUESTION:
        x0, x1 = 0, 1000
    width, height = page_image.size
    if width < 40 or height < 40:
        return None
    px0 = max(0, int(x0 / 1000.0 * width) - int(width * pad_x / 1000.0))
    px1 = min(width, int(x1 / 1000.0 * width) + int(width * pad_x / 1000.0))
    py0 = max(0, int(y0 / 1000.0 * height) - int(height * pad_y / 1000.0))
    py1 = min(height, int(y1 / 1000.0 * height) + int(height * pad_y / 1000.0))
    if px1 - px0 < 24 or py1 - py0 < 24:
        return None
    return Band(key, page_no, (px0, py0, px1, py1), width, height)


def qa(box, band):
    """(ok, reason) for a band-relative figure box.

    These are the failures worth one re-cut with more padding: a box that
    touches a cut edge was probably clipped, and a box that fills the band is
    the question itself rather than its picture.
    """
    clean = clean_box(box)
    if not clean:
        return False, "degenerate"
    y0, x0, y1, x1 = clean
    w, h = x1 - x0, y1 - y0
    area = (w / 1000.0) * (h / 1000.0)
    if area < 0.002:
        return False, "tiny"
    if area > 0.92:
        return False, "whole-band"
    if h <= 0 or w / float(h) < 0.08 or w / float(h) > 12:
        return False, "aspect"
    if y0 <= 1 or y1 >= 999:
        return False, "vertical-edge"
    full_width = band.width >= band.page_w - 4
    if not full_width and (x0 <= 1 or x1 >= 999):
        return False, "side-edge"
    return True, ""


def jpeg_bytes(page_image, band, quality=88):
    """The exact JPEG bytes sent for one band."""
    buf = io.BytesIO()
    band.crop(page_image).save(buf, "JPEG", quality=int(quality), optimize=True)
    return buf.getvalue()


def write_jpeg(page_image, band, dest, quality=88):
    """Save a band for inspection; the bytes match what the model receives."""
    data = jpeg_bytes(page_image, band, quality)
    with open(dest, "wb") as fh:
        fh.write(data)
    return data
