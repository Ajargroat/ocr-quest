"""Supabase target — upload the approved crops, then upsert rows.

Sources never enter the bucket: they stay in the folder tree and the row
references one by its folder id (Q8/Q11). Every approved crop uploads under a
stable object id derived from the source folder + block key, so a re-crop
replaces the same object (delete then upsert) instead of leaving an orphan.
Uploads happen before rows are built, so a failed crop upload leaves its
question queued instead of importing it without a figure.
"""
from pipeline import db as db_mod
from pipeline import storage

from pipeline.studio import geometry, merge, workspace
from pipeline.studio.importers.rows import build_rows, diagram_urls

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
    if rows["questions"]:
        database.upsert_questions(rows["questions"])
    if rows["answers"]:
        database.upsert_answers(rows["answers"])
    return {"target": "supabase", "dry_run": False,
            "questions": len(rows["questions"]),
            "answers": len(rows["answers"]), "skipped": rows["skipped"],
            "imported": rows["imported"], "uploaded": len(blob),
            "errors": errors}
