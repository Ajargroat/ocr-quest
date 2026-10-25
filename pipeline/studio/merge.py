"""The merged view: layer2 + geometry + review → what the UI and import read.

Stage artifacts stay immutable on disk; this module is the single place that
folds them into one row set, so the bulk table, the crop panel and the
importer can never disagree about what a block says or whether its crop is
approved.
"""
import copy
import os

from pipeline.studio import enrich, profile as profile_mod, tags, workspace

# Fields a human may override in the review panel.
EDITABLE = ("kind", "number", "label", "text", "options", "subject", "corp",
            "year", "grade", "topic", "difficulty", "answer")
# Issues that are relations between blocks — an edit cannot recompute them.
KEEP_ISSUES = ("duplicate number in the answer key",
               "no question with this number",
               "sorting call returned nothing")


def _crop_state(entry, job_id, key, block_page=None):
    # An EXTERNAL crop is a finished picture the reviewer chose (Inspect's
    # local-first draft). It has no page box: the bytes in the crop cache are
    # the picture, and the importer uploads them as-is instead of re-cutting.
    external = bool(entry.get("external"))
    needed = bool(entry.get("crop_bbox")) or external
    rel = entry.get("crop")
    cached = workspace.crop_path(job_id, key) if key else ""
    exists = bool(rel) and os.path.exists(
        os.path.join(workspace.job_dir(job_id), rel))
    if external and cached and os.path.exists(cached):
        exists = True
    # A planned crop names crop_page; a REVIEWED box in review.json names no
    # page, so the block's own page is what the crop belongs to.
    state = {"key": key, "needed": needed, "approved": entry.get("crop_approved"),
             "bbox": entry.get("crop_bbox"),
             "page": entry.get("crop_page") or block_page,
             "file": rel, "bytes": entry.get("crop_bytes"),
             "external": external, "exists": exists,
             "source": entry.get("crop_source"),
             "figure": entry.get("figure_source")}
    return state


def _soft_for(layer_blocks, edits):
    """Job-wide absent metadata fields, review edits included."""
    probe = []
    for raw in layer_blocks or []:
        if raw.get("kind") != "question":
            continue
        block = {"kind": "question", "corp": raw.get("corp"),
                 "year": raw.get("year")}
        patch = edits.get(raw.get("key"))
        if not isinstance(patch, dict):
            patch = {}
        for field in enrich.SOFT_METADATA:
            if patch.get(field) is not None:
                block[field] = patch[field]
        probe.append(block)
    return enrich._job_soft(probe)


def _tag_set(profile):
    ref = (profile or {}).get("tag_list_ref")
    data = tags.load(ref) if ref else tags.load()
    return {tags.fold(t) for t in tags.known_topics(data) if str(t).strip()}


def _landed(options):
    return [str(o).strip() for o in (options or []) if str(o).strip()]


def scorecard(view, soft=None):
    """Pure counts over a merged view - the job's own quality card.

    No model, no extra I/O: every number is a property of what the job already
    holds, so the card can never claim more than the rows do.
    """
    blocks = view.get("blocks") or []
    questions = [b for b in blocks if b.get("kind") == "question"]
    prose = [b for b in blocks if b.get("kind") == "prose"]
    four = sum(1 for q in questions if len(_landed(q.get("options"))) == 4)
    topics = [q.get("topic") for q in questions if q.get("topic")]
    known = _tag_set(view.get("profile"))
    matched = sum(1 for t in topics if tags.fold(t) in known)
    crops = [b for b in blocks if (b.get("crop") or {}).get("needed")]
    approved = sum(1 for b in crops if (b.get("crop") or {}).get("approved"))
    confidence = []
    for page in view.get("pages") or []:
        try:
            confidence.append(float((page.get("layout") or {}).get("confidence")))
        except (TypeError, ValueError):
            continue
    return {
        "blocks": len(blocks), "questions": len(questions),
        "prose": len(prose),
        "ready": sum(1 for b in blocks if b.get("status") == "ready"),
        "warn": sum(1 for b in blocks if b.get("status") == "warn"),
        "error": sum(1 for b in blocks if b.get("status") == "error"),
        "four_options": four,
        "fewer_options": len(questions) - four,
        "topics": len(topics), "topics_known": matched,
        "topics_unknown": len(topics) - matched,
        "crops": len(crops), "crops_approved": approved,
        "crops_awaiting": len(crops) - approved,
        "layout_confidence": min(confidence) if confidence else None,
        "organized": sum(1 for b in blocks if b.get("organized")),
        "figures": sum(1 for b in blocks
                       if b.get("has_figure") or b.get("figure_bbox")),
        "metadata_missing": sorted(soft if soft is not None
                                   else view.get("metadata_missing") or []),
    }


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
    soft = _soft_for(layer2.get("blocks") or [], edits)

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
        if cov.get("external"):
            # the reviewer supplied a FINISHED picture: it is needed, it
            # needs no page box, and the importer must not re-cut it.
            entry["external"] = True
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
            status, issues = enrich._health(block, req, profile, soft)
            block["status"], block["issues"] = status, issues
            block["needs_human"] = status != "ready"
        block["geo"] = {"bbox": entry.get("bbox"),
                        "source": entry.get("source"),
                        "crop_bbox": entry.get("crop_bbox"),
                        "approved": entry.get("crop_approved")}
        if entry.get("bbox"):
            block["bbox"] = entry["bbox"]
            block["bbox_source"] = entry.get("source") or block.get("bbox_source")
        state = _crop_state(entry, job_id, key, block.get("page"))
        block["crop"] = state
        if _figure_marker(block):
            block["figure_marker"] = True
        if state["needed"]:
            crops[key] = state
        blocks.append(block)

    view = {
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
        # one job-level note instead of a warning per row: the source printed
        # no metadata line for these fields, so no row can carry them.
        "metadata_missing": sorted(soft),
    }
    view["scorecard"] = scorecard(view, soft)
    return view


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
