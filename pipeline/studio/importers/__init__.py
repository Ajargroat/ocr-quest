"""Stage 4 — import: merged rows → a target, continuously and idempotently.

A run takes every row that is ready *now* (healthy, crop approved) and leaves
the rest queued — so uploading can happen at any time, at any batch size, and
re-running only touches what changed: ids are derived from the job and the
block key, and every write is an upsert.
"""
import time

from pipeline.studio import faults as studio_faults
from pipeline.studio import merge, workspace
from pipeline.studio.importers import file_export, supabase
from pipeline.studio.importers.rows import TARGETS, build_rows, marks, targets

def run(cfg, job_id, target="supabase", dry_run=False, emit=None):
    """Import whatever is ready right now. Returns the run report."""
    emit = emit or (lambda *args, **kwargs: None)
    target = target or "supabase"
    if target not in dict(TARGETS):
        raise ValueError("unknown import target: " + str(target))
    view = merge.merged(job_id)
    if not view.get("blocks"):
        raise ValueError("job has nothing to import yet — run enrich first")

    imports = workspace.read_artifact(job_id, "imports") or {}
    imports.setdefault("version", 1)
    imports.setdefault("job_id", job_id)
    imports.setdefault("uploads", {"source": None, "crops": {}})
    imports.setdefault("results", {})
    imports.setdefault("runs", [])

    emit("import", "info", "importing to %s%s"
         % (target, " (dry run)" if dry_run else ""))
    if target == "file":
        report = file_export.run(job_id, view, dry_run=dry_run)
    else:
        try:
            report = supabase.run(cfg, job_id, view, imports, dry_run=dry_run,
                                  emit=emit)
        except Exception as exc:
            # The log keeps the failure with its classified fault: a stage
            # error that only echoed back as a red HTTP body vanished on reload.
            if not dry_run:
                workspace.log_event(
                    job_id, "import", "error", "import failed: %s" % exc,
                    fault=studio_faults.payload(studio_faults.classify(exc)))
            raise

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not dry_run:
        for mark in marks(view, report):
            imports["results"][mark["key"]] = {
                "status": mark["status"], "reason": mark.get("reason"),
                "at": now, "target": target}
    report = {k: v for k, v in report.items() if k != "imported"}
    imports["runs"] = (imports["runs"] + [dict(report, at=now)])[-20:]
    workspace.write_artifact(job_id, "imports", imports)
    workspace.log_event(
        job_id, "import", "success",
        "imported %d question(s), %d answer(s) to %s - %d queued"
        % (report.get("questions", 0), report.get("answers", 0), target,
           len(report.get("skipped") or [])))
    return {"job": workspace.summary(job_id), "report": report}
