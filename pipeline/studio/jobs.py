"""Stage orchestration + job state for the studio.

This is the thin conductor the UI talks to: create job → store source → probe
→ save profile → extract → enrich → geometry → import, with the stage
functions below sharing the same job state machine.

State is process-local (like ``revision/runner.py``): the authoritative job
record is the workspace directory on disk, and this module only mirrors the
*currently running* operation so the dashboard can show live progress.
"""
import threading

from . import enrich as enrich_mod
from . import extract as extract_mod
from . import geometry as geometry_mod
from . import importers
from . import probe as probe_mod
from . import profile as profile_mod
from . import workspace

_lock = threading.Lock()
_state = {"running": False, "job": None, "stage": None, "message": "",
          "error": None, "last_result": None}


def _set(**kw):
    with _lock:
        _state.update(kw)


def get_state() -> dict:
    with _lock:
        return dict(_state)


def _emit(hub, job_id, stage, level, message, **extra):
    """Log to the job file and (if a hub is attached) the live dashboard."""
    workspace.log_event(job_id, stage, level, message, **extra)
    if hub is not None:
        try:
            hub.emit({"type": "studio", "job": job_id, "stage": stage,
                      "level": level, "message": message, **extra})
        except Exception:
            pass


# ── job lifecycle ───────────────────────────────────────────────────────────

def create_job(hub, fileobj, filename: str, original_name: str = None) -> dict:
    """Create a job, store the source, and probe it. Returns the job summary.

    This is the whole of Stage 0's non-AI half: after it returns, the job has
    a source file, a probe report and a suggested (editable) profile on disk.
    """
    job_id = workspace.new_id()
    _set(running=True, job=job_id, stage="intake", message="storing source",
         error=None, last_result=None)
    try:
        workspace.save_source(job_id, fileobj, filename, original_name)
        _emit(hub, job_id, "intake", "info",
              f"source stored: {original_name or filename}")
        return run_probe(hub, job_id)
    except Exception as exc:
        _set(error=str(exc), running=False)
        _emit(hub, job_id, "intake", "error", str(exc))
        raise
    finally:
        _set(running=False, stage=None)


def run_probe(hub, job_id: str) -> dict:
    """Probe a job's source and persist a suggested profile. Returns the summary."""
    _set(running=True, job=job_id, stage="probe", message="probing", error=None)
    try:
        src = workspace.source_path(job_id)
        if not src:
            raise ValueError("job has no source file")
        _emit(hub, job_id, "probe", "info", "probing source…")
        report = probe_mod.probe(src)
        if not report.get("ok"):
            raise ValueError(report.get("error") or "probe failed")
        workspace.write_artifact(job_id, "probe", report)

        # Keep an existing, user-edited profile; only seed a new one.
        existing = workspace.read_artifact(job_id, "profile")
        if existing:
            suggestion = profile_mod.normalize(existing)
            _emit(hub, job_id, "probe", "info",
                  "existing profile kept (re-probe does not overwrite edits)")
        else:
            suggestion = profile_mod.normalize(probe_mod.suggest_profile(report))
            workspace.write_artifact(job_id, "profile", suggestion)

        _emit(hub, job_id, "probe", "success",
              f"{report.get('source_kind')} · {report.get('page_count')} page(s) · "
              f"text pages {report.get('text_pages')} · "
              f"answer-key pages {len(report.get('answer_key_pages') or [])}")
        result = {"job": workspace.summary(job_id), "probe": report,
                  "profile": suggestion}
        _set(last_result=result)
        return result
    except Exception as exc:
        _set(error=str(exc))
        _emit(hub, job_id, "probe", "error", str(exc))
        raise
    finally:
        _set(running=False, stage=None)


def save_profile(hub, job_id: str, overrides: dict) -> dict:
    """Apply UI overrides to a job's profile and persist it."""
    current = workspace.read_artifact(job_id, "profile") or profile_mod.blank()
    merged = profile_mod.apply_overrides(current, overrides or {})
    workspace.write_artifact(job_id, "profile", merged)
    _emit(hub, job_id, "profile", "info", "profile updated",
          static=merged.get("static"), compilation=merged.get("compilation"))
    return {"job": workspace.summary(job_id), "profile": merged,
            "required_missing": profile_mod.required_missing(merged)}


# ── the stage chain (extract → enrich → geometry → import) ──────────────

def _run_stage(hub, job_id: str, stage: str, func, message: str) -> dict:
    """Shared shell: state → emit → call → last_result → clear."""
    _set(running=True, job=job_id, stage=stage, message=message, error=None)
    try:
        def emit(step, level, text, **extra):
            _emit(hub, job_id, step, level, text, **extra)
        result = func(job_id, emit)
        _set(last_result=result)
        return result
    except Exception as exc:
        _set(error=str(exc))
        _emit(hub, job_id, stage, "error", str(exc))
        raise
    finally:
        _set(running=False, stage=None)

def run_extract(hub, job_id: str, cfg) -> dict:
    """Stage 1 — source → layer1 (text lane deterministic, image lane OCR)."""
    return _run_stage(hub, job_id, "extract",
                      lambda jid, emit: extract_mod.run(jid, cfg, emit),
                      "extracting")

def run_enrich(hub, job_id: str, cfg) -> dict:
    """Stage 2 — layer1 → layer2 (fields, flags, tags, answer links)."""
    return _run_stage(hub, job_id, "enrich",
                      lambda jid, emit: enrich_mod.run(jid, cfg, emit),
                      "sorting")

def run_geometry(hub, job_id: str, cfg) -> dict:
    """Stage 3 — locate every block, plan + cache its crop."""
    return _run_stage(hub, job_id, "geometry",
                      lambda jid, emit: geometry_mod.run(jid, cfg, emit),
                      "locating")

def run_import(hub, job_id: str, cfg, target: str = "supabase",
               dry_run: bool = False) -> dict:
    """Stage 4 — write whatever is ready now to the chosen target."""
    return _run_stage(
        hub, job_id, "import",
        lambda jid, emit: importers.run(cfg, jid, target=target,
                                        dry_run=dry_run, emit=emit),
        "importing")

def delete_job(job_id: str) -> bool:
    """Remove a job directory (and all its artifacts). Returns True if gone."""
    import shutil
    try:
        path = workspace.job_dir(job_id)
    except ValueError:
        return False
    if not workspace.exists(job_id):
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not workspace.exists(job_id)
