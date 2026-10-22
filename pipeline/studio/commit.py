"""Approve the Inspect batch: review edits + staged pictures → ONE import.

LOCAL first, IMPORT second. While the reviewer works, nothing touches the
bucket or the database:

* text/field edits live in the browser's localStorage (non-secret UI state);
* replacement pictures live in ``workspace/<job>/drafts/`` (``drafts.py``).

Pressing Approve calls :func:`apply` once for the whole set. It folds the
edits into ``review.json`` exactly like the Crops tab does, copies each staged
picture into the job's crop cache, and hands the merged view to the normal
importer — which uploads the pictures AS THEY ARE (no browser-side cropping,
no re-cut from a bbox) and upserts every ready row in a single batch.

Everything remains re-runnable: ids are derived from the job + block key and
every write is an upsert, so approving twice only rewrites what changed.
"""
import time

from pipeline.studio import drafts, merge, workspace


def _merge_patch(current, incoming):
    """Overlay ``incoming`` patches onto ``current`` (both {key: {field…}})."""
    out = {key: dict(value) for key, value in (current or {}).items()
           if isinstance(value, dict)}
    for key, patch in (incoming or {}).items():
        if not isinstance(patch, dict):
            continue
        out[key] = {**(out.get(key) or {}), **patch}
    return out


def apply(cfg, job_id, edits=None, crops=None, target="supabase",
          dry_run=False, emit=None):
    """Apply the Inspect drafts and import everything ready, in one pass.

    Returns {"review", "staged", "uploads", "report", "job"} — ``report`` is
    the importer's own report (questions/answers/skipped/errors).
    """
    emit = emit or (lambda *args, **kwargs: None)
    view = merge.merged(job_id)
    known = {block.get("key") for block in view.get("blocks") or []}
    if not known:
        raise ValueError("job has nothing to import yet — run enrich first")

    edits = {key: value for key, value in (edits or {}).items()
             if key in known and isinstance(value, dict)}
    crops = {key: value for key, value in (crops or {}).items()
             if key in known and isinstance(value, dict)}
    staged = {key: value for key, value in drafts.staged(job_id).items()
              if key in known}

    # A staged picture IS an approved, finished crop: it has no page box, so
    # it is marked external (the importer uploads it as-is, never re-cuts it).
    for key in staged:
        patch = crops.setdefault(key, {})
        patch["approved"] = True
        patch["external"] = True
        patch["bbox"] = None

    current = workspace.read_artifact(job_id, "review") or {}
    payload = {
        "version": 1,
        "job_id": job_id,
        "edits": _merge_patch(current.get("edits"), edits),
        "crops": _merge_patch(current.get("crops"), crops),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if dry_run:
        # A preview is read-only: it names what WOULD be approved and
        # leaves review.json and the drafts untouched.
        return {"review": payload, "staged": sorted(staged), "uploads": 0,
                "report": None, "job": workspace.summary(job_id)}

    workspace.write_artifact(job_id, "review", payload)
    emit("inspect", "info", "approved %d edit(s) · %d picture(s)"
         % (len(edits), len(staged)))

    # The crop cache is what the importer reads for an external crop; putting
    # the approved bytes there keeps one source of truth on disk.
    for key in staged:
        data = drafts.read(job_id, key)
        if not data:
            continue
        path = workspace.crop_path(job_id, key)
        with open(path, "wb") as fh:
            fh.write(data)

    from pipeline.studio import importers
    result = importers.run(cfg, job_id, target=target or "supabase",
                           dry_run=False, emit=emit)
    drafts.clear(job_id)
    return {"review": workspace.read_artifact(job_id, "review"),
            "staged": [], "uploads": len(staged),
            "report": result.get("report"), "job": result.get("job")}
