"""Supabase target — upload the approved crops, then upsert rows.

Sources never enter the bucket: they stay in the folder tree and the row
references one by its folder id (Q8/Q11). Every approved crop uploads under a
stable object id derived from the source folder + block key, so a re-crop
replaces the same object (delete then upsert) instead of leaving an orphan.
Uploads happen before rows are built, so a failed crop upload leaves its
question queued instead of importing it without a figure.

The questions/answers name their source by id ("studio-<job_id>") and that
column is a foreign key onto sources(id), so the sources row is written
before them - otherwise the insert is rejected.
"""
import hashlib
import os

from pipeline import db as db_mod
from pipeline import storage
from pipeline.scanner import ALLOWED_EXTENSIONS, TYPE_DIRS, SourceItem

from pipeline.studio import geometry, merge, workspace
from pipeline.studio.importers.rows import (build_rows, diagram_urls,
                                            job_source_id)
from pipeline.utils import parse_grade

def _external_crop(job_id, block, cfg, folder, cache, emit):
    """Upload the reviewer's finished picture — as-is, never re-cut.

    Inspect's staged crop is already the picture the question needs, so there
    is no page box to render from and no browser-side cropping to replay: the
    bytes in the crop cache go straight to the bucket."""
    key = block.get("key")
    path = workspace.crop_path(job_id, key)
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None, {"key": key, "error": "approved picture is missing"}
    spec = "external:" + hashlib.sha1(data).hexdigest()
    obj = geometry.crop_object(cfg, folder, key)
    have = cache.get(key) or {}
    page = (block.get("crop") or {}).get("page") or 0
    if have.get("spec") == spec and have.get("object") == obj:
        return {"object": obj, "bbox": None, "page": page,
                "url": have.get("url")}, None
    if have.get("object") == obj:
        try:
            storage.delete_file(cfg, obj)
        except Exception as exc:      # non-fatal: the upload still replaces it
            emit("import", "warn", "crop %s delete: %s" % (key, exc))
    try:
        url = storage.upload_file(cfg, obj, data, "image/jpeg")
    except Exception as exc:
        emit("import", "error", "crop %s: %s" % (key, exc))
        return None, {"key": key, "error": str(exc)}
    cache[key] = {"url": url, "object": obj, "spec": spec}
    emit("import", "info", "uploaded approved picture %s (%d KB)"
         % (key, len(data) // 1024))
    return {"object": obj, "bbox": None, "page": page, "url": url}, None


def _upload_crops(cfg, job_id, view, imports, emit):
    """Upload every approved crop under its stable object id (replace in place)."""
    cache = imports["uploads"].setdefault("crops", {})
    blob, errors = {}, []
    folder = view.get("folder_id") or ("studio-%s" % job_id)
    for block in view.get("blocks") or []:
        key = block.get("key")
        ok, _ = merge.importable(block)
        if not ok or not (block.get("crop") or {}).get("needed"):
            continue
        if (block.get("crop") or {}).get("external"):
            entry, error = _external_crop(job_id, block, cfg, folder,
                                          cache, emit)
            if error:
                errors.append(error)
            elif entry:
                blob[key] = entry
            continue
        entry = geometry.render_crop(job_id, key,
                                     (block.get("crop") or {}).get("bbox"))
        if not entry:
            errors.append({"key": key, "error": "crop could not be rendered"})
            continue
        spec = entry.get("spec")
        have = cache.get(key) or {}
        obj = geometry.crop_object(cfg, folder, key)
        page = entry.get("crop_page") or (block.get("crop") or {}).get("page")
        if have.get("spec") == spec and have.get("object") == obj:
            blob[key] = {"object": obj, "bbox": entry.get("crop_bbox"),
                         "page": page, "url": have.get("url")}
            continue
        if have.get("object") == obj:
            try:
                storage.delete_file(cfg, obj)
            except Exception as exc:      # non-fatal: the upload still replaces it
                emit("import", "warn", "crop %s delete: %s" % (key, exc))
        with open(workspace.crop_path(job_id, key), "rb") as fh:
            data = fh.read()
        try:
            url = storage.upload_file(cfg, obj, data, "image/jpeg")
        except Exception as exc:
            errors.append({"key": key, "error": str(exc)})
            emit("import", "error", "crop %s: %s" % (key, exc))
            continue
        cache[key] = {"url": url, "object": obj, "spec": spec}
        blob[key] = {"object": obj, "bbox": entry.get("crop_bbox"),
                     "page": page, "url": url}
        emit("import", "info", "uploaded crop %s (%d KB)" % (key, len(data) // 1024))
    return blob, errors

def _job_meta(job_id):
    """The job's source_meta artifact ({} when the job has none)."""
    try:
        return workspace.read_artifact(job_id, "source_meta", {}) or {}
    except ValueError:
        return {}

def _grade(value):
    """sources.grade is a SMALLINT: a Persian grade name becomes its digit."""
    return parse_grade(value)

def _source_item(job_id, view, rows):
    """The `sources` row the imported rows point at.

    The studio keeps its sources in its own folder system
    (``STUDIO_SOURCE_ROOT``, ``<INPUT_ROOT>/studio`` by default) - a tree the
    old scanner shape never walks - and the row references its folder by the
    same sha1 id the scanner derives, so the source can be found and backed up
    later.
    """
    meta = _job_meta(job_id)
    static = (view.get("profile") or {}).get("static") or {}
    questions = rows.get("questions") or []
    answers = rows.get("answers") or []
    first = (questions or answers or [{}])[0]
    name = view.get("source") or meta.get("name") or ""
    ext = os.path.splitext(name)[1].lower()
    return SourceItem(
        source_id=job_source_id(view),
        rel_path=meta.get("rel_path") or "",
        subject=static.get("subject") or first.get("subject"),
        grade=_grade(static.get("grade")),
        topic=static.get("topic") or first.get("topic"),
        type=TYPE_DIRS["question"] if questions else TYPE_DIRS["answer"],
        file_name=name,
        file_path=workspace.source_path(job_id) or "",
        file_size=meta.get("size") or 0,
        mime_type=ALLOWED_EXTENSIONS.get(ext, ""),
    )

def _ensure_source(database, job_id, view, rows, emit):
    """Write the sources row the imported rows point at.

    `db.upsert_source` is the table's only writer (no raw SQL, no schema
    change). A handle that does not implement it cannot write the row at
    all: that is reported through ``emit`` instead of failing the import.
    """
    item = _source_item(job_id, view, rows)
    write = getattr(database, "upsert_source", None)
    if write is None:
        emit("import", "warn", "the database handle cannot write a sources "
                               "row - the imported rows may be rejected")
        return None
    write(item, "")
    return item

def run(cfg, job_id, view, imports, dry_run=False, emit=None):
    """The supabase import. Updates ``imports['uploads']`` in place."""
    emit = emit or (lambda *args, **kwargs: None)
    if dry_run:
        urls = {key: (state.get("file") or "crops/%s.jpg" % key)
                for key, state in (view.get("crops") or {}).items()
                if state.get("approved")}
        rows = build_rows(view, urls)
        return {"target": "supabase", "dry_run": True,
                "questions": len(rows["questions"]),
                "answers": len(rows["answers"]),
                "skipped": rows["skipped"], "imported": rows["imported"],
                "uploaded": 0, "errors": []}

    blob, errors = _upload_crops(cfg, job_id, view, imports, emit)
    rows = build_rows(view, diagram_urls(view, blob))
    emit("import", "info", "writing %d question(s) + %d answer(s) to the database"
         % (len(rows["questions"]), len(rows["answers"])))
    database = db_mod.Database(cfg)
    if rows["questions"] or rows["answers"]:
        # the rows point at this sources row by id - it has to exist first
        _ensure_source(database, job_id, view, rows, emit)
    if rows["questions"]:
        database.upsert_questions(rows["questions"])
    if rows["answers"]:
        database.upsert_answers(rows["answers"])
    return {"target": "supabase", "dry_run": False,
            "questions": len(rows["questions"]),
            "answers": len(rows["answers"]), "skipped": rows["skipped"],
            "imported": rows["imported"], "uploaded": len(blob),
            "errors": errors}
