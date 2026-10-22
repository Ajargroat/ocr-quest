"""Local-first staging for the Inspect tab's pictures.

The reviewer's replacement crop is a FINISHED picture: either a chosen file
(an image editor, a screenshot tool) or the source page cut at the square the
Inspect editor sends (``cut``). It is cut BEFORE the upload, so the bucket
only ever receives the reviewer's picture and the browser never re-encodes an
image - which is both fragile and, on a no-auth dashboard, an unnecessary
attack surface.

So a chosen picture lands HERE first: ``workspace/<job>/drafts/<key>.jpg``, a
job-local folder on disk, never the bucket and never the database. Nothing is
published until the user approves the batch; ``pipeline.studio.commit`` then
uploads every staged picture as-is and runs one import for the whole set.

This module is pure file I/O plus a PIL re-encode (the bucket's crops are
JPEG, and the upload is exactly the approved bytes).
"""
import io
import os
import re

from pipeline.studio import workspace

SUBDIR = "drafts"
# A crop is one question's figure, not a page scan: 12 MB is generous and keeps
# a stray multi-page PDF upload from filling the disk.
MAX_BYTES = 12 * 1024 * 1024
# The long edge the bucket can serve without a visible quality loss.
MAX_EDGE = 4000
QUALITY = 90


def _safe_key(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "", str(key or ""))[:80]


def draft_dir(job_id: str) -> str:
    path = os.path.join(workspace.job_dir(job_id, create=True), SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def draft_path(job_id: str, key: str) -> str:
    """Where one block's staged picture lives (JPEG, cropped by the user)."""
    safe = _safe_key(key)
    if not safe:
        raise ValueError("invalid block key")
    return os.path.join(draft_dir(job_id), safe + ".jpg")


def staged(job_id: str) -> dict:
    """{block_key: {"bytes", "at"}} for every picture waiting for approval."""
    directory = draft_dir(job_id)
    out = {}
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return out
    for name in names:
        if not name.lower().endswith(".jpg"):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
        except OSError:
            continue
        out[name[:-4]] = {"bytes": stat.st_size, "at": int(stat.st_mtime)}
    return out


def read(job_id: str, key: str) -> bytes:
    """The staged bytes, or b"" when nothing is staged."""
    try:
        with open(draft_path(job_id, key), "rb") as fh:
            return fh.read()
    except (OSError, ValueError):
        return b""


def _stage(job_id: str, key: str, image) -> dict:
    """Re-encode one PIL image to JPEG and stage it. Returns the summary."""
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    if max(image.size) > MAX_EDGE:
        image.thumbnail((MAX_EDGE, MAX_EDGE))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=QUALITY, optimize=True)
    payload = buffer.getvalue()
    path = draft_path(job_id, key)
    with open(path, "wb") as fh:
        fh.write(payload)
    return {"key": key, "bytes": len(payload), "width": image.width,
            "height": image.height, "at": int(os.path.getmtime(path))}

def save(job_id: str, key: str, data: bytes) -> dict:
    """Re-encode a chosen picture to JPEG and stage it. Returns the summary.

    A non-image (or a truncated one) raises ValueError instead of writing a
    file the importer would later trip over.
    """
    if not data:
        raise ValueError("empty picture")
    if len(data) > MAX_BYTES:
        raise ValueError("picture is larger than %d MB" % (MAX_BYTES // 1048576))
    from PIL import Image, UnidentifiedImageError
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError("not a readable image: %s" % exc)
    return _stage(job_id, key, image)

def cut(job_id: str, key: str, bbox) -> dict:
    """Cut a page box into a staged picture (the Inspect editor's square).

    ``bbox`` is the editor's square in the page's own 0-1000 space - the same
    convention the stored crops use - so the picture the reviewer sees under
    the square is exactly the region approval will upload. It only reads the
    job's page and writes ``drafts/<key>.jpg``: the geometry artifact, the
    crop cache, the bucket and the database are untouched.
    """
    from pipeline.studio import geometry
    box = geometry._clean_bbox(bbox)
    if not box:
        raise ValueError("invalid crop box")
    entry = ((workspace.read_artifact(job_id, "geometry") or {}).get("boxes")
             or {}).get(key) or {}
    try:
        page = geometry._page_pil(job_id, entry)
    except Exception as exc:
        raise ValueError("no page to cut from: %s" % exc)
    x0, y0, x1, y1 = geometry._norm_to_pixels(box, page.width, page.height)
    if x1 - x0 < 12 or y1 - y0 < 12:
        raise ValueError("crop box is too small")
    return _stage(job_id, key, page.crop((x0, y0, x1, y1)))


def drop(job_id: str, key: str) -> bool:
    """Forget one staged picture. True when a file was removed."""
    try:
        path = draft_path(job_id, key)
    except ValueError:
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def clear(job_id: str) -> int:
    """Remove every staged picture (after a successful approval)."""
    removed = 0
    for key in list(staged(job_id)):
        if drop(job_id, key):
            removed += 1
    return removed
