"""The merged view: layer2 + geometry + review → what the UI and import read.

Stage artifacts stay immutable on disk; this module is the single place that
folds them into one row set, so the bulk table, the crop panel and the
importer can never disagree about what a block says or whether its crop is
approved.
"""
import copy
import os

from pipeline.studio import enrich, profile as profile_mod, workspace

# Fields a human may override in the review panel.
EDITABLE = ("kind", "number", "label", "text", "options", "subject", "corp",
            "year", "grade", "topic", "difficulty", "answer")
# Issues that are relations between blocks — an edit cannot recompute them.
KEEP_ISSUES = ("duplicate number in the answer key",
               "no question with this number",
               "sorting call returned nothing")


def _crop_state(entry, job_id, key):
    needed = bool(entry.get("crop_bbox"))
    rel = entry.get("crop")
    state = {"key": key, "needed": needed, "approved": entry.get("crop_approved"),
             "bbox": entry.get("crop_bbox"), "page": entry.get("crop_page"),
             "file": rel, "bytes": entry.get("crop_bytes"),
             "exists": bool(rel) and os.path.exists(
                 os.path.join(workspace.job_dir(job_id), rel))}
    return state


def _figure_marker(block):
    """True when the block owns a picture the text lane cannot spell out."""
    return bool(block.get("has_figure") or block.get("images")) \
        and (block.get("source") or {}).get("method") == "text"


def merged(job_id):
    """One flat, review-overlaid view of a job. Returns a dict."""
    layer2 = workspace.read_artifact(job_id, "layer2") or {}
    geometry = workspace.read_artifact(job_id, "geometry") or {}
    review = workspace.read_artifact(job_id, "review") or {}
    profile = layer2.get("profile") or profile_mod.blank()
    req = enrich._req(profile)
    boxes = geometry.get("boxes") or {}
    edits = review.get("edits") or {}
    crop_edits = review.get("crops") or {}

    blocks, crops = [], {}
    for raw in layer2.get("blocks") or []:
        key = raw.get("key")
        block = copy.deepcopy(raw)
        patch = edits.get(key) if isinstance(edits.get(key), dict) else None
        entry = dict(boxes.get(key) or {})
        cov = crop_edits.get(key) if isinstance(crop_edits.get(key), dict) else {}
        if "bbox" in cov:
            # A null box is the reviewer saying "this question has no picture"
            # (the vision pass flagged one that is not there).
            entry["crop_bbox"] = list(cov["bbox"]) if cov.get("bbox") else None
        if "approved" in cov:
            entry["crop_approved"] = bool(cov["approved"]) or None
        if cov.get("split_ok"):
            # the review desk confirmed the join; the half may import
            block["split"] = None
            block["split_joined"] = True
        if patch:
            for field in EDITABLE:
                if field in patch and patch[field] is not None:
                    block[field] = patch[field]
            block["issues"] = [i for i in block.get("issues") or []
                               if i in KEEP_ISSUES]
            block["missing"] = []
            status, issues = enrich._health(block, req, profile)
            block["status"], block["issues"] = status, issues
            block["needs_human"] = status != "ready"
        block["geo"] = {"bbox": entry.get("bbox"),
                        "source": entry.get("source"),
                        "crop_bbox": entry.get("crop_bbox"),
                        "approved": entry.get("crop_approved")}
        if entry.get("bbox"):
            block["bbox"] = entry["bbox"]
            block["bbox_source"] = entry.get("source") or block.get("bbox_source")
        state = _crop_state(entry, job_id, key)
        block["crop"] = state
        if _figure_marker(block):
            block["figure_marker"] = True
        if state["needed"]:
            crops[key] = state
        blocks.append(block)

    return {
        "job_id": job_id,
        "source": layer2.get("source"),
        "folder_id": (workspace.read_artifact(job_id, "source_meta")
                      or {}).get("folder_id"),
        "kind": layer2.get("kind"),
        "profile": profile,
        "pages": layer2.get("pages") or [],
        "assets": layer2.get("assets") or [],
        "blocks": blocks,
        "crops": crops,
        "stats": layer2.get("stats") or {},
    }


def importable(block):
    """(ok, reason) — may this block be written to a target right now?"""
    if block.get("kind") not in ("question", "answer_key_entry"):
        return False, "not a question"
    if block.get("status") == "error":
        return False, "flagged: " + "; ".join(block.get("missing")
                                              or block.get("issues") or [])
    if block.get("split"):
        return False, "split across a page boundary"
    crop = block.get("crop") or {}
    if crop.get("needed") and not crop.get("approved"):
        return False, "crop awaiting approval"
    return True, None

def render_crop(job_id, key):
    """Render one merged block's crop — the reviewed box is what renders."""
    from pipeline.studio import geometry
    view = merged(job_id)
    block = next((b for b in view["blocks"] if b.get("key") == key), None)
    if not block or not (block.get("crop") or {}).get("needed"):
        return None
    return geometry.render_crop(job_id, key, block["crop"].get("bbox"))
