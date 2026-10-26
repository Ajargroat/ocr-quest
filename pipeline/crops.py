"""Crop geometry and deterministic crop naming â€” the one bbox contract.

Every crop in this repository is a slice of ONE page image, addressed by a
normalised box on a 0-1000 scale (``bbox = [ymin, xmin, ymax, xmax]``, the same
order the Supabase dashboard and Sapienstech read).  Two things have historically
gone wrong there, and this module owns both:

* **the box** â€” an empty, reversed or sub-pixel box is not a crop. ``normalize``
  returns ``None`` for it so a caller can refuse the row instead of uploading a
  1x1 JPEG that looks like a success;
* **the name** â€” a crop object is a finished picture referenced by rows, so its
  object name must be reproducible from (source, question, index) alone.
  ``crop_key`` builds that name; re-cutting a row re-uses it, which is what makes
  a re-crop a replace instead of an orphan variant.

Coordinates are converted with ``to_pixels`` (box -> page pixels) and back with
``to_norm`` (page pixels -> box).  Both round the same way, so a box that goes
through a render and back is stable.
"""
import io
import re

MIN_NORM = 8           # a box thinner than this (of 1000) is not a picture
DEFAULT_QUALITY = 88
CROP_PREFIX = "crops"

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _int(value):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def clean_box(raw):
    """A raw box -> four clamped ints ``[ymin, xmin, ymax, xmax]`` or ``None``.

    ``None`` means "this is not a picture": wrong length, non-numeric,
    degenerate (thinner than ``MIN_NORM`` on either axis).
    """
    if isinstance(raw, str):
        import json
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    values = [_int(v) for v in raw]
    if any(v is None for v in values):
        return None
    ymin, xmin, ymax, xmax = (max(0, min(1000, v)) for v in values)
    # A model sometimes answers [y1,x1,y0,x0]: order first, then judge.
    ymin, ymax = min(ymin, ymax), max(ymin, ymax)
    xmin, xmax = min(xmin, xmax), max(xmin, xmax)
    if ymax - ymin < MIN_NORM or xmax - xmin < MIN_NORM:
        return None
    return [ymin, xmin, ymax, xmax]


def normalize(raw):
    """The canonical box for a raw answer, or ``None`` when it is not one.

    A reversed box is the same picture, so it is put back in order rather than
    refused (see ``clean_box``).
    """
    return clean_box(raw)


def is_valid(raw):
    return normalize(raw) is not None


def merge(*boxes):
    """The union box of several boxes (a picture split across two crops)."""
    good = [b for b in (normalize(x) for x in boxes) if b]
    if not good:
        return None
    return [min(b[0] for b in good), min(b[1] for b in good),
            max(b[2] for b in good), max(b[3] for b in good)]


def to_pixels(box, width, height):
    """Box -> ``(x0, y0, x1, y1)`` in page pixels, clipped to the page."""
    clean = normalize(box)
    if not clean or width <= 0 or height <= 0:
        return None
    ymin, xmin, ymax, xmax = clean
    x0 = max(0, min(width, int(round(xmin / 1000.0 * width))))
    x1 = max(0, min(width, int(round(xmax / 1000.0 * width))))
    y0 = max(0, min(height, int(round(ymin / 1000.0 * height))))
    y1 = max(0, min(height, int(round(ymax / 1000.0 * height))))
    if x1 - x0 < 4 or y1 - y0 < 4:
        return None
    return (x0, y0, x1, y1)


def to_norm(rect, width, height):
    """Page-pixel ``(x0, y0, x1, y1)`` -> a clamped box, or ``None``."""
    if not rect or len(rect) != 4 or width <= 0 or height <= 0:
        return None

    def unit(value, scale):
        return max(0, min(1000, int(round(float(value) / scale * 1000.0))))

    x0, y0, x1, y1 = rect
    return normalize([unit(y0, height), unit(x0, width),
                      unit(y1, height), unit(x1, width)])


def crop_key(source_id, kind, number, index=0):
    """The deterministic object name of one crop.

    ``crops/<sha1 of the source dir>/<kind>-<number>-<index>.jpg`` â€” identity
    comes from the source and the row, never from a timestamp, so re-cutting
    the same picture writes over the same object.
    """
    source = str(source_id or "")
    folder = source.rsplit("-", 1)[0] if re.fullmatch(r"[0-9a-f]{40}(-\d+)?", source) \
        else source
    folder = _SAFE.sub("", folder).replace(".", "") or "unknown"
    kind = _SAFE.sub("", str(kind or "q")).lower() or "q"
    number = _int(number) or 0
    index = _int(index) or 0
    return "%s/%s/%s%d-%d.jpg" % (CROP_PREFIX, folder, kind, number, index)


def public_name(crop_object):
    """The bucket-relative name of an object URL the DB may already store."""
    text = str(crop_object or "")
    if "/storage/v1/object/public/" in text:
        text = text.split("/storage/v1/object/public/", 1)[1]
        text = text.split("/", 1)[1] if "/" in text else text
    return text.lstrip("/")


def render(page_image, box, quality=DEFAULT_QUALITY):
    """Cut ``box`` out of a PIL page image; returns JPEG bytes or ``None``."""
    rect = to_pixels(box, *page_image.size)
    if rect is None:
        return None
    cut = page_image.crop(rect)
    if cut.width < 4 or cut.height < 4:
        return None
    buf = io.BytesIO()
    cut.save(buf, "JPEG", quality=int(quality), optimize=True)
    return buf.getvalue()


def open_image(data):
    """Bytes -> a PIL image (RGB), or a ValueError naming the bad file."""
    from PIL import Image
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:              # PIL raises its own error family
        raise ValueError("not a readable image: %s" % exc) from exc
    return image.convert("RGB") if image.mode != "RGB" else image


def inspect(data, box):
    """(ok, reason) for a stored crop: decodable, and the box fits the page."""
    try:
        image = open_image(data)
    except ValueError as exc:
        return False, str(exc)
    clean = normalize(box)
    if clean is None:
        return False, "bbox is empty or degenerate"
    rect = to_pixels(clean, *image.size)
    if rect is None:
        return False, "bbox is outside the page"
    return True, ""
