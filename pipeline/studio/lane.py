"""Per-job lane: which provider/model one source file runs on, plus its prompt.

The Studio Models tab keeps the STUDIO-LOCAL provider list and a default pick
per stage; this module is the per-job override beside those defaults:

    workspace/<job>/studio.json   {"provider": "<label>" | "", "prompt": ""}

An empty ``provider`` means "the Models tab's stage pick" (and once the job has
run once, the lane it actually used is remembered there), so a file keeps the
same model on its next stage unless the reviewer changes it — one file can run
on a different model than the next without touching the global list.

``lane_cfg(cfg, job_id, stage)`` is the single place that folds the override
into a config copy for one stage. On the Gemini pool it also carries the
job's ``prompt`` (SPARE) into ``studio_gemini_model_prompt``; a 9router lane
has no per-model prompt map, so that half is pool-only.

``PREVIEW_SRC`` is the seam tests set, so no unit test renders a page.
"""
import copy
import os

from . import workspace

DEFAULTS = ("provider", "prompt")

# Preview long edge for the Inspect editor's page (the crop overlay reads it
# through a plain canvas, so a page-sized JPEG is plenty and stays small).
PREVIEW_LONG_EDGE = 1600
PREVIEW_QUALITY = 80
PREVIEW_DIR = "previews"

PREVIEW_SRC = None


def read(job_id):
    """This job's saved overrides, defaults filled in (never raises)."""
    try:
        stored = workspace.read_artifact(job_id, "studio", {}) or {}
    except ValueError:
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    out = {}
    for field in DEFAULTS:
        value = stored.get(field)
        out[field] = value.strip() if isinstance(value, str) else ""
    return out


def save(job_id, patch):
    """Merge one patch over the saved overrides. Returns the saved dict."""
    data = read(job_id)
    for field in DEFAULTS:
        if field in (patch or {}) and patch[field] is not None:
            value = str(patch[field])
            data[field] = value.strip() if field == "provider" else value
    # A save is also an explicit confirmation: drop any preview rendered from
    # the previous state so the editor never shows a stale box.
    drop_preview(job_id)
    workspace.write_artifact(job_id, "studio", data)
    return data


def lane_cfg(cfg, job_id, stage):
    """A config copy for ONE stage of ONE job (defaults when nothing is set)."""
    try:
        data = read(job_id)
    except ValueError:
        data = {"provider": "", "prompt": ""}
    label = data.get("provider") or ""
    prompt = data.get("prompt") or ""
    if not label and not prompt:
        return cfg
    lane_cfg = copy.copy(cfg)
    if label:
        lane_cfg = _with_stage(lane_cfg, stage, label)
    if prompt:
        lane_cfg.studio_gemini_model_prompt = dict(
            getattr(cfg, "studio_gemini_model_prompt", None) or {})
        lane_cfg.studio_gemini_model_prompt[stage] = prompt
    return lane_cfg


def _with_stage(cfg, stage, label):
    key = {"extract": "studio_extract_active", "enrich": "studio_enrich_active",
           "vision": "studio_vision_active"}.get(stage)
    if key:
        setattr(cfg, key, label)
    return cfg


def used(cfg, job_id, stage):
    """The provider label one stage would run on right now ('' = Gemini pool)."""
    try:
        stored = read(job_id).get("provider") or ""
    except ValueError:
        stored = ""
    if stored:
        return stored
    return (getattr(cfg, {"extract": "studio_extract_active",
                          "enrich": "studio_enrich_active",
                          "vision": "studio_vision_active"}.get(stage, ""), "")
            or "").strip()


def preview_dir(job_id):
    path = os.path.join(workspace.job_dir(job_id), PREVIEW_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def preview_path(job_id, page):
    """Where the editor's page preview for one page lives."""
    return os.path.join(preview_dir(job_id), "page-%s.jpg" % int(page))


def drop_preview(job_id):
    """Forget every rendered preview of a job (best effort)."""
    try:
        folder = os.path.join(workspace.job_dir(job_id), PREVIEW_DIR)
    except ValueError:
        return
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        try:
            os.remove(os.path.join(folder, name))
        except OSError:
            pass


def render_preview(job_id, page):
    """One page as a capped JPEG (the crop cut it overlays). Returns the path."""
    if PREVIEW_SRC is not None:
        return PREVIEW_SRC(job_id, page)
    dest = preview_path(job_id, page)
    if os.path.isfile(dest):
        return dest
    from pipeline import converter
    from . import geometry

    geometry_mod = geometry
    entry = {"crop_page": int(page), "crop_image": _page_image_rel(job_id, page)}
    pil = geometry_mod._page_pil(job_id, entry)
    if pil.width > PREVIEW_LONG_EDGE:
        ratio = PREVIEW_LONG_EDGE / float(pil.width)
        pil = pil.resize((PREVIEW_LONG_EDGE, max(1, int(pil.height * ratio))))
    converter._save_capped(pil, dest, PREVIEW_QUALITY)
    return dest


def _page_image_rel(job_id, page):
    """The stored page image for one page, when layer2 kept one."""
    layer2 = workspace.read_artifact(job_id, "layer2") or {}
    for item in layer2.get("pages") or []:
        if isinstance(item, dict) and item.get("page") == int(page):
            rel = item.get("image") or ""
            if rel and os.path.isfile(os.path.join(workspace.job_dir(job_id), rel)):
                return rel
    return ""
