"""In-app PDF upload queue (workstream B).

Uploaded PDFs land in the konkour-ocr/ tree under a validated
destination, then the embedded converter watcher extracts their pages
and the normal scanner/runner pipeline processes them — sequential by
construction, no parallel push (PREFERENCE §2). Queue events reuse the
existing Hub broadcast the dashboard already subscribes to.

Q9: the queue is manageable — pending files can be removed and
queued/failed ones cancelled.
"""
import os
import shutil
import threading
import time
import uuid
from collections import deque

from .scanner import EXPECTED_LAYOUTS, TYPE_DIRS

_lock = threading.Lock()
_queue = deque()          # [{id, filename, dest_rel, status, detail, ts}]
_by_id = {}

# Statuses: queued → extracting → done | failed | cancelled.
FINAL = {"done", "failed", "cancelled"}


def _upload_root(cfg):
    return os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "konkouploads")


def _dest_ok(dest):
    """Validate the destination against the watched-tree layout.

    Accepts `subject/grade/topic/question|answer` (the EXPECTED_LAYOUTS
    prefix without the filename) — the filename comes from the upload."""
    parts = [p for p in (dest or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if len(parts) != 4:
        return False
    if any(p in ("..",) or "/" in p or p in ("done", "failed") for p in parts):
        return False
    if parts[3] not in TYPE_DIRS:
        return False
    return True


def _dest_hint():
    return ("destination must be "
            "'subject/grade/topic/question|answer' under the watched tree")


def reset_for_tests():
    with _lock:
        _queue.clear()
        _by_id.clear()


def enqueue(cfg, fileobj, filename, dest):
    """Validate + stream one upload to disk. Returns the queue item dict."""
    if not _dest_ok(dest):
        raise ValueError(_dest_hint())
    safe_name = os.path.basename(filename or "").strip()
    if not safe_name.lower().endswith(".pdf") or not safe_name:
        raise ValueError("only .pdf files can be uploaded")
    parts = [p for p in dest.replace("\\", "/").split("/") if p not in ("", ".")]
    rel_dir = "/".join(parts)
    target_dir = os.path.join(cfg.input_root, *parts)
    os.makedirs(target_dir, exist_ok=True)
    uid = uuid.uuid4().hex[:12]
    stamped = f"{uid}-{safe_name}"
    target = os.path.join(target_dir, stamped)
    with open(target, "wb") as out:
        shutil.copyfileobj(fileobj, out, length=1024 * 256)
    # Fresh uploads must not trip the converter's maturity gate on arrival:
    # backdate the mtime so the next sweep picks them up immediately.
    try:
        min_age = int(getattr(cfg, "min_age_seconds", 60) or 60)
        mature = time.time() - (min_age + 1)
        os.utime(target, (mature, mature))
    except OSError:
        pass
    item = {"id": uid, "filename": safe_name, "stored": stamped,
            "dest_rel": rel_dir, "status": "queued", "detail": "Queued",
            "ts": time.time()}
    with _lock:
        _queue.append(item)
        _by_id[uid] = item
    return dict(item)


def list_items():
    with _lock:
        return [dict(i) for i in _queue]


def cancel(uid):
    """Remove a pending item / cancel a queued-or-failed one (Q9).

    Physical PDFs already landed in the tree stay — they are normal
    watched files now; cancellation only drops the tracking row.
    Returns the item, or None when unknown."""
    with _lock:
        item = _by_id.get(uid)
        if item is None:
            return None
        if item["status"] in ("extracting", "done"):
            return None  # in-flight or finished: nothing manageable left
        item["status"] = "cancelled"
        item["detail"] = "Cancelled by user"
        try:
            _queue.remove(item)
        except ValueError:
            pass
        return dict(item)


def mark(uid, status, detail=""):
    """Advance a tracking row (called by the converter sweep)."""
    with _lock:
        item = _by_id.get(uid)
        if item is None:
            return None
        item["status"] = status
        if detail:
            item["detail"] = detail
        if status in FINAL:
            try:
                _queue.remove(item)
            except ValueError:
                pass
        return dict(item)
