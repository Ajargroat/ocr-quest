"""Re-cut a stored picture from the page it came from (QBank bbox editing).

A QBank picture is a finished crop (`crops/<legacy_id>.jpg`); the page it was
cut out of still lives in the sources bucket as `sources.storage_url`. So a
corrected box means: fetch the page once, cut the new box, and upload it over
the SAME object id - the Q12 invariant that a re-crop replaces a crop instead
of accumulating variants.

Nothing here reads the pipeline's folder tree: the page arrives over HTTP from
the bucket that already holds it, so editing works for rows whose source JPEG
is long gone from local disk.
"""
import io
import threading
import time
from urllib.parse import quote

import requests

from . import storage
from .converter import (PAGE_SIZE_CAP, QUALITY_FLOOR, QUALITY_START,
                        QUALITY_STEP)

PAGE_CACHE_MAX = 24
_MIN_SIDE = 16          # px; a smaller box is a mis-drag, not a crop

_pages = {}
_lock = threading.Lock()


def clear_cache():
    """Drop the memoised pages (tests, and after a source is re-uploaded)."""
    with _lock:
        _pages.clear()


def page_bytes(url):
    """One source page as JPEG bytes, memoised per URL."""
    if not url:
        raise LookupError("no source page for this picture")
    with _lock:
        hit = _pages.get(url)
    if hit:
        return hit
    try:
        resp = requests.get(url, timeout=45)
    except requests.RequestException as exc:
        raise LookupError("page fetch failed: %s" % exc)
    if resp.status_code != 200:
        raise LookupError("page fetch failed (HTTP %s)" % resp.status_code)
    data = resp.content
    if not data:
        raise LookupError("page object is empty")
    with _lock:
        if len(_pages) >= PAGE_CACHE_MAX:
            _pages.pop(next(iter(_pages)))
        _pages[url] = data
    return data


def clean_bbox(raw):
    """`[ymin, xmin, ymax, xmax]`, 0-1000, with a real span - or ValueError."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        raise ValueError("bbox must be 4 numbers between 0 and 1000.")
    try:
        box = [float(v) for v in raw]
    except (TypeError, ValueError):
        raise ValueError("bbox must be 4 numbers between 0 and 1000.")
    if any(v != v or v < 0 or v > 1000 for v in box):
        raise ValueError("bbox must be 4 numbers between 0 and 1000.")
    ymin, xmin, ymax, xmax = box
    if ymax <= ymin or xmax <= xmin:
        raise ValueError("Requires ymin < ymax and xmin < xmax.")
    return [round(v, 2) for v in box]


def pixels(bbox, width, height):
    """A 0-1000 yxxy box as page pixel edges (the crop frame)."""
    ymin, xmin, ymax, xmax = bbox
    return (max(0, int(xmin / 1000.0 * width)),
            max(0, int(ymin / 1000.0 * height)),
            min(width, int(round(xmax / 1000.0 * width))),
            min(height, int(round(ymax / 1000.0 * height))))


def _encode(crop):
    """JPEG bytes under the page-size cap, stepping the quality down."""
    q = max(QUALITY_FLOOR, int(QUALITY_START))
    while True:
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=q)
        data = buf.getvalue()
        if q <= QUALITY_FLOOR or len(data) <= PAGE_SIZE_CAP:
            return data
        q = max(QUALITY_FLOOR, q - QUALITY_STEP)


def cut(data, bbox):
    """`(jpeg bytes, width, height)` for one page image and its box."""
    from PIL import Image
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.load()
            page = im.convert("RGB")
    except Exception as exc:
        raise ValueError("source page is not a readable image: %s" % exc)
    box = pixels(bbox, page.width, page.height)
    if box[2] - box[0] < _MIN_SIDE or box[3] - box[1] < _MIN_SIDE:
        raise ValueError("the box is too small to cut (%dx%d px)"
                         % (box[2] - box[0], box[3] - box[1]))
    crop = page.crop(box)
    return _encode(crop), crop.width, crop.height


def recrop(cfg, db, question_id, index, raw_bbox):
    """Cut one picture's new box from its page; overwrite the same object."""
    row = db.fetch_media_row(question_id)
    if not row:
        raise LookupError("no such question")
    media = row.get("media") or []
    if index < 0 or index >= len(media):
        raise ValueError("index out of range for this picture")
    object_name = str((media[index] or {}).get("object") or "")
    if not object_name.startswith("crops/"):
        raise ValueError("this picture is the page itself - nothing to re-cut")
    bbox = clean_bbox(raw_bbox)
    data, width, height = cut(page_bytes(row.get("source_url")), bbox)
    storage.upload_file(cfg, object_name, data, "image/jpeg")
    db.update_media_bbox(question_id, index, bbox)
    return {"object": object_name, "bbox": bbox, "width": width,
            "height": height, "bytes": len(data),
            "url": "/api/media?object=%s&v=%d"
                   % (quote(object_name, safe=""), int(time.time()))}
