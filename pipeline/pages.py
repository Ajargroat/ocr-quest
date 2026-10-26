"""Page rendering and the shared "what is a figure" rule.

Staging needs both, and needs them deterministic — no model, no network:

* ``render`` turns ONE pdfium page into a PIL image. A bbox is meaningless
  without the page frame it was measured in, so the render is at a fixed DPI
  and the page's own size is stored beside every crop;
* ``figure_bounds`` is the single decorative-image rule the probe and the
  extractor used to share. It is kept here so "this picture is a real figure"
  has one definition: page furniture (a full-page scan, a header strip), a
  sliver (a corner mark, a bullet) and an image drawn on top of a page-filling
  picture (a banner over a cover) are not figures.
"""
import os
import threading

# 170 dpi: dense Persian text stays readable for a model and for a human
# reviewing a crop, at ~1.4 MB per page (measured).
RENDER_DPI = int(os.getenv("STAGING_RENDER_DPI", "170"))

_PDFIUM_LOCK = threading.Lock()

# A page whose extracted text has at least this many non-space characters is
# text-selectable. Real exam pages carry hundreds; a scan carries a handful.
TEXT_PAGE_MIN_CHARS = 40

FIGURE_MAX_AREA_RATIO = 0.35     # at or above this share: page furniture
FIGURE_MIN_AREA_RATIO = 0.005    # below this share: a tiny decoration
FIGURE_FILL_AREA_RATIO = 0.9     # at or above this share: the page itself


def open_pdf(path):
    """A pdfium document for ``path``; raises ValueError naming a bad file."""
    import pypdfium2 as pdfium
    try:
        return pdfium.PdfDocument(path)
    except Exception as exc:
        raise ValueError("cannot open the PDF: %s" % exc) from exc


def page_count(path):
    doc = open_pdf(path)
    try:
        return len(doc)
    finally:
        close(doc)


def close(doc):
    try:
        doc.close()
    except Exception:
        pass


def render(page, dpi=None):
    """One pdfium page as an independent RGB PIL image."""
    from PIL import Image
    scale = (dpi or RENDER_DPI) / 72.0
    with _PDFIUM_LOCK:
        bitmap = page.render(scale=scale)
        try:
            pil = bitmap.to_pil().copy()      # detach from the bitmap buffer
        finally:
            del bitmap
    if pil.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", pil.size, (255, 255, 255))
        mask = pil.split()[-1] if pil.mode in ("RGBA", "LA") else None
        background.paste(pil, mask=mask)
        pil = background
    elif pil.mode != "RGB":
        pil = pil.convert("RGB")
    return pil


def fit_long_edge(pil, edge):
    """Downscale so the longest side is at most ``edge`` (never upscale)."""
    edge = int(edge or 0)
    if edge <= 0 or max(pil.size) <= edge:
        return pil
    ratio = edge / float(max(pil.size))
    return pil.resize((max(1, int(pil.width * ratio)),
                       max(1, int(pil.height * ratio))))


def page_text(page):
    """The page's text layer ("" when the page has none)."""
    try:
        return page.get_textpage().get_text_range() or ""
    except Exception:
        return ""


def has_text_layer(page):
    text = page_text(page)
    return len(text.strip()) >= TEXT_PAGE_MIN_CHARS


def _page_area(page):
    """The page area in PDF points, or 0.0 when the size is unknown."""
    try:
        width, height = page.get_size()
    except Exception:
        return 0.0
    if not width or not height:
        return 0.0
    return float(width) * float(height)


def _area_share(bounds, area):
    return ((bounds[2] - bounds[0]) * (bounds[3] - bounds[1])) / area


def _inside(inner, outer, tol=1.0):
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol
            and inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def figure_bounds(page):
    """``(left, bottom, right, top)`` of every genuine figure image on a page.

    pdfium object types: 1 = text, 2 = path, 3 = image. Images are the reliable
    "this page has a picture" signal; vector paths flood-count rules and text
    on every page, so only 3 counts here. When the page size is unknown every
    image is kept — an unmeasurable page must not lose a real figure.
    """
    boxes = []
    try:
        for obj in page.get_objects():
            if obj.type != 3:               # 3 = image
                continue
            try:
                bounds = tuple(obj.get_bounds())
            except Exception:
                continue
            if len(bounds) != 4 or bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
                continue
            boxes.append(bounds)
    except Exception:
        return []
    area = _page_area(page)
    if not area:
        return boxes
    filling = [b for b in boxes if _area_share(b, area) >= FIGURE_FILL_AREA_RATIO]
    out = []
    for bounds in boxes:
        share = _area_share(bounds, area)
        if share >= FIGURE_MAX_AREA_RATIO or share < FIGURE_MIN_AREA_RATIO:
            continue
        if any(_inside(bounds, big) for big in filling):
            continue
        out.append(bounds)
    return out
