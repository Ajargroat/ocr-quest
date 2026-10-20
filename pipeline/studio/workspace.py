"""Job workspaces: one directory per studio job, files as the source of truth.

Layout (all under ``<project>/workspace/`` by default, gitignored):

    workspace/<job_id>/
        source_meta.json  the source's tree path + scanner folder id
        profile.json      Stage 0 result (kind, compilation, static fields)
        layer1.json       Stage 1 raw bulk
        layer2.json       Stage 2 enriched bulk  (the reviewed artifact)
        review.json       human edits/approvals over layer2
        crops/            on-demand crop cache
        log.jsonl         append-only, per-stage, cite-able events

The uploaded source does not live here: it is streamed once into the folder
tree (``<INPUT_ROOT>/studio/<job>/`` by default, ``STUDIO_SOURCE_ROOT``
overrides it) and ``source_meta.json`` records its tree-relative path plus the
scanner-derived folder id (Q8/Q11). Nothing here talks to a database or the
network; it is pure file I/O so the stage code and the UI can both read the
same bytes.
"""
import json
import os
import re
import time

from pipeline.utils import sha1_hex

# A job id is a short, sortable, filesystem-safe token. It is NOT a secret and
# NOT a hash of content (re-uploading the same file makes a NEW job on purpose —
# a second pass over a file is a legitimate thing to want).
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# The one directory the studio owns. Created lazily; gitignored.
DEFAULT_WORKSPACE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "workspace")


def workspace_root() -> str:
    """The studio's workspace root (override with STUDIO_WORKSPACE in .env)."""
    return os.getenv("STUDIO_WORKSPACE") or DEFAULT_WORKSPACE_ROOT


def valid_id(job_id: str) -> bool:
    """True when job_id is safe to use as a single path segment.

    Rejects ``..``, separators, empty and over-long ids so a crafted job id can
    never escape the workspace root.
    """
    return bool(job_id) and bool(_ID_RE.match(job_id))


def new_id() -> str:
    """A fresh, monotonic-ish, filesystem-safe job id."""
    return time.strftime("%Y%m%d-%H%M%S") + "-" + os.urandom(2).hex()


def job_dir(job_id: str, create: bool = False) -> str:
    """Absolute path of one job's directory. Raises ValueError on a bad id."""
    if not valid_id(job_id):
        raise ValueError("invalid job id")
    path = os.path.join(workspace_root(), job_id)
    if create:
        os.makedirs(path, exist_ok=True)
        os.makedirs(os.path.join(path, "crops"), exist_ok=True)
    return path


def job_subdir(job_id: str, name: str, create: bool = False) -> str:
    """One named subdirectory of a job ('assets', 'pages', 'crops')."""
    if name not in ("assets", "pages", "crops", "bands"):
        raise ValueError("unknown job subdirectory: " + str(name))
    path = os.path.join(job_dir(job_id, create=create), name)
    if create:
        os.makedirs(path, exist_ok=True)
    return path


def asset_path(job_id: str, asset_id: str) -> str:
    """Where one deduped figure asset lives: assets/<sha-id>.jpg."""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(asset_id or ""))[:64]
    return os.path.join(job_subdir(job_id, "assets", create=True), safe + ".jpg")


def crop_path(job_id: str, key: str) -> str:
    """Where one block's rendered crop lives (cache; on-demand render)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(key or ""))[:80]
    return os.path.join(job_subdir(job_id, "crops", create=True), safe + ".jpg")


def page_image_path(job_id: str, page: int) -> str:
    """The stored page render of an image-lane source page."""
    return os.path.join(job_subdir(job_id, "pages", create=True),
                        "page-%d.jpg" % int(page))

def band_path(job_id: str, key: str) -> str:
    """Where the figure pass keeps one question band (inspection artifact)."""
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(key or ""))[:80]
    return os.path.join(job_subdir(job_id, "bands", create=True), safe + ".jpg")


def exists(job_id: str) -> bool:
    try:
        return os.path.isdir(job_dir(job_id))
    except ValueError:
        return False


# ── JSON read/write (atomic) ────────────────────────────────────────────────

def _read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def _write_json(path: str, value):
    """Atomic write (tmp + rename) so a crash never leaves half a JSON file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def read_artifact(job_id: str, name: str, default=None):
    """Read one named artifact ('profile' | 'layer1' | 'layer2' | 'review')."""
    return _read_json(os.path.join(job_dir(job_id), name + ".json"), default)


def write_artifact(job_id: str, name: str, value):
    """Write one named artifact atomically. Creates the job dir if needed."""
    job_dir(job_id, create=True)
    _write_json(os.path.join(job_dir(job_id), name + ".json"), value)


# ── source file ─────────────────────────────────────────────────────────────

_SOURCE_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp")


def _source_root() -> str:
    """The folder-tree root sources live under (STUDIO_SOURCE_ROOT overrides).

    Defaults to ``<INPUT_ROOT>/studio`` — the scanner skips unknown top-level
    folders (only ``question``/``answer`` are walked), so the tree is inert.
    """
    root = (os.getenv("STUDIO_SOURCE_ROOT") or "").strip()
    if root:
        return root
    from pipeline import config as config_mod
    input_root = os.getenv("INPUT_ROOT") or config_mod.DEFAULT_INPUT_ROOT
    return os.path.join(input_root, "studio")


def source_dir(job_id: str) -> str:
    """The folder-tree directory one job's source lives in."""
    if not valid_id(job_id):
        raise ValueError("invalid job id")
    return os.path.join(_source_root(), job_id)


def folder_id(rel_path: str) -> str:
    """The scanner's own id derivation, applied to a tree-relative path."""
    return sha1_hex(rel_path.replace(os.sep, "/"))


def _safe_name(name: str) -> str:
    """A single path segment safe for the source tree (never '..', keeps Persian)."""
    base = os.path.basename(str(name or "").replace("\\", "/"))
    safe = re.sub(r"[^\w.\- ]", "-", base, flags=re.UNICODE).strip(" .")
    return safe[:80] or "source"


def source_path(job_id: str):
    """The source file's path: the tree copy first, the legacy job copy second."""
    try:
        meta = read_artifact(job_id, "source_meta", {}) or {}
    except ValueError:
        return None
    rel = meta.get("rel_path")
    if rel:
        path = os.path.join(_source_root(), rel.replace("/", os.sep))
        return path if os.path.isfile(path) else None
    try:
        directory = job_dir(job_id)
    except ValueError:
        return None
    if not os.path.isdir(directory):
        return None
    for name in sorted(os.listdir(directory)):
        if name.startswith("source.") and name.lower().endswith(_SOURCE_EXTS):
            return os.path.join(directory, name)
    return None


def source_name(job_id: str):
    """The original file name recorded when the source was stored."""
    meta = read_artifact(job_id, "source_meta", {}) or {}
    return meta.get("name")


def save_source(job_id: str, fileobj, filename: str, original_name: str = None):
    """Stream one uploaded file into the folder tree (one copy, no workspace copy).

    ``fileobj`` is any binary file-like (an UploadFile's ``.file``); it is
    copied in chunks so a large PDF is never held whole in memory. Returns the
    stored path. ``source_meta.json`` records the tree-relative path, the
    scanner-derived folder id and the original name for display.
    """
    job_dir(job_id, create=True)          # artifacts only; the source lives in the tree
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in _SOURCE_EXTS:
        raise ValueError("unsupported file type: " + (ext or "(none)"))
    name = _safe_name(filename)
    directory = source_dir(job_id)
    os.makedirs(directory, exist_ok=True)
    dest = os.path.join(directory, name)
    with open(dest, "wb") as out:
        while True:
            chunk = fileobj.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
    rel = os.path.relpath(dest, _source_root()).replace(os.sep, "/")
    write_artifact(job_id, "source_meta", {
        "name": original_name or filename,
        "stored": name,
        "rel_path": rel,
        "folder_id": folder_id(rel),
        "size": os.path.getsize(dest),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return dest


# ── append-only log ─────────────────────────────────────────────────────────

def log_event(job_id: str, stage: str, level: str, message: str, **extra):
    """Append one line to ``log.jsonl``. Best-effort; never raises."""
    try:
        job_dir(job_id, create=True)
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "stage": stage, "level": level, "message": message}
        row.update(extra)
        with open(os.path.join(job_dir(job_id), "log.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_log(job_id: str, limit: int = 200):
    """The last ``limit`` log lines, newest last."""
    path = os.path.join(job_dir(job_id), "log.jsonl")
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


# ── listing ─────────────────────────────────────────────────────────────────

# Which artifacts a job "has" is derived from disk, not a stored flag, so the
# UI can never disagree with reality.
ARTIFACTS = ("profile", "layer1", "layer2", "geometry", "review", "imports")


def summary(job_id: str) -> dict:
    """A compact, disk-derived description of one job for the Jobs list."""
    directory = job_dir(job_id)
    src = source_path(job_id)
    meta = read_artifact(job_id, "source_meta", {}) or {}
    profile = read_artifact(job_id, "profile", {}) or {}
    have = {name: os.path.isfile(os.path.join(directory, name + ".json"))
            for name in ARTIFACTS}
    layer2 = read_artifact(job_id, "layer2", {}) or {}
    layer1 = layer2 or (read_artifact(job_id, "layer1", {}) or {})
    blocks = layer1.get("blocks") or []
    figures = [b for b in blocks if b.get("has_figure")]
    imports = read_artifact(job_id, "imports", {}) or {}
    imported = sum(1 for row in (imports.get("results") or {}).values()
                   if isinstance(row, dict) and row.get("status") == "ok")
    geometry = read_artifact(job_id, "geometry", {}) or {}
    return {
        "id": job_id,
        "name": meta.get("name") or (os.path.basename(src) if src else job_id),
        "kind": profile.get("source_kind"),
        "compilation": profile.get("compilation"),
        "subject": (profile.get("static") or {}).get("subject"),
        "has_source": bool(src),
        "artifacts": have,
        "blocks": len(blocks) if (have["layer2"] or have["layer1"]) else None,
        "figures": len(figures) if blocks else None,
        "geometry": bool(have["geometry"]),
        "geometry_missing": (geometry.get("stats") or {}).get("missing"),
        "imported": imported,
        "created_at": meta.get("saved_at"),
        "mtime": os.path.getmtime(directory) if os.path.isdir(directory) else None,
    }


def list_jobs() -> list:
    """Every job on disk, newest first. Corrupt/oddly-named dirs are skipped."""
    root = workspace_root()
    if not os.path.isdir(root):
        return []
    jobs = []
    for name in os.listdir(root):
        if not valid_id(name) or not os.path.isdir(os.path.join(root, name)):
            continue
        try:
            jobs.append(summary(name))
        except Exception:
            continue
    jobs.sort(key=lambda j: j.get("mtime") or 0, reverse=True)
    return jobs
