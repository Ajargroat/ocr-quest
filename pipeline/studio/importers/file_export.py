"""File target — the rows as one JSON file inside the job directory.

This is the offline path: no network, no database. It exists so a job can be
handed over (or re-imported later) without credentials, and it is what the
Import panel's dry run shows.
"""
import json
import os
import time

from pipeline.studio import workspace
from pipeline.studio.importers.rows import build_rows, diagram_urls

def run(job_id, view, dry_run=False):
    """Write (or count) the export. Returns the run report."""
    rows = build_rows(view, diagram_urls(view))
    dest = os.path.join(workspace.job_dir(job_id), "export.json")
    if not dry_run:
        payload = {
            "version": 1,
            "job_id": job_id,
            "source": view.get("source"),
            "folder_id": view.get("folder_id"),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "questions": rows["questions"],
            "answers": rows["answers"],
            "skipped": rows["skipped"],
        }
        tmp = dest + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, dest)
    return {"target": "file", "dry_run": dry_run,
            "questions": len(rows["questions"]),
            "answers": len(rows["answers"]), "skipped": rows["skipped"],
            "imported": rows["imported"], "uploaded": 0,
            "file": os.path.basename(dest)}
