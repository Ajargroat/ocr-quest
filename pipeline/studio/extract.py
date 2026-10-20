"""Stage 1 — extraction: source → layer1.json (the raw bulk).

Text lane (deterministic, no model): pdfium's text layer + per-character
boxes → ``layout.split_text`` → one block per question with an EXACT
text-layer bbox (the prototype from the plan), and every embedded figure
saved once under a content id — deduped across the whole job, referenced by
blocks, so storage holds each picture exactly one time.

Image lane: each page is rendered through the shared 200 KB compressor and
sent to the stage-1 model with ``OCR_PAGE_PROMPT`` (verbatim output — a
low-thinking task any model can serve); the reply runs through the SAME
splitter, so both lanes produce one bulk schema.

Mixed PDFs use both lanes page by page. A vision page that fails is logged
and left empty — the rest of the job still extracts.
"""
import base64
import hashlib
import os
import re
import time

from pipeline import converter
from pipeline.studio import chunker, layout, llm, workspace
from pipeline.studio import probe as probe_mod

# Render resolution for figures, page images and (later) crops.
RENDER_DPI = int(os.getenv("STUDIO_RENDER_DPI", "170"))
# Vision pages are capped by pixels, not bytes: the old 200 KB byte cap forced
# a 4968 px page down to JPEG quality 20, which is bigger *and* blurrier than
# a 3000 px page at quality 88.
VISION_LONG_EDGE = int(os.getenv("STUDIO_VISION_LONG_EDGE", "3000"))
VISION_QUALITY = int(os.getenv("STUDIO_VISION_QUALITY", "88"))
# Hard bounds so one pathological page cannot fill the disk.
FIGURES_PER_PAGE = 12
# A figure this many normalized units below a block still belongs to it.
FIGURE_GAP = 250
ASSET_LIMIT = int(os.getenv("STUDIO_ASSET_LIMIT", "300"))

# The stage-1 prompt for the image lane: raw text out, nothing else.
OCR_PAGE_PROMPT = """You are a precise OCR engine for Iranian exam pages.

Return the text of every page image in this request, verbatim, in natural
reading order - nothing else.

Rules:
1. Reproduce Persian text exactly, keeping half-spaces (ZWNJ) and punctuation.
2. Keep question numbers and option labels exactly as printed (۱، ۲، 1) ، الف) …).
3. Keep the line structure: a question stem, each option and each metadata line stay their own line, in the printed order.
4. If formulas, physics, chemistry or math notation appear, write them in clean LaTeX.
5. Do not translate, summarise, explain, renumber or add commentary.
6. If a word is genuinely unreadable, give your best single reading — never a placeholder like [?].
7. When the request carries more than one page image, start each page with one line exactly "=== PAGE <n> ===", n counting from 1 in the order the images were sent.
8. Never cut a question: one question stays one entry. If the last question on this page continues onto the next page, end this page with one line exactly "SPLIT: tail".
9. If the first question on this page continues a question that started on an earlier page, print one line exactly "SPLIT: head" right before that question's text. A question that both continues and continues onwards gets both marker lines.
Output only the page text."""

# The vision lane's per-page section marker (rule 7 of the prompt).
PAGE_MARK_RE = re.compile(r"^\s*=+\s*PAGE\s+(\d+)\s*=+\s*$", re.IGNORECASE)


# ── pdfium helpers ──────────────────────────────────────────────────────────

def _page_text(page):
    try:
        return page.get_textpage().get_text_range() or ""
    except Exception:
        return ""


def _char_boxes(page):
    """Per-character (left, bottom, right, top) PDF-point boxes, y-up.

    Index i matches character i of ``get_text_range()``, which is exactly
    what ``layout`` block ranges index into.
    """
    try:
        textpage = page.get_textpage()
        count = textpage.count_chars()
    except Exception:
        return []
    boxes = []
    for i in range(count):
        try:
            boxes.append(tuple(textpage.get_charbox(i)))
        except Exception:
            boxes.append(None)
    return boxes


def _figure_bounds(page):
    """(left, bottom, right, top) of every image object on the page."""
    out = []
    try:
        for obj in page.get_objects():
            if obj.type != 3:               # 3 = image (see probe._count_figures)
                continue
            try:
                bounds = tuple(obj.get_bounds())
            except Exception:
                continue
            if len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                out.append(bounds)
    except Exception:
        return []
    return out[:FIGURES_PER_PAGE]


def _render_pil(page, dpi=None):
    """One full-page render as an independent RGB PIL image."""
    from PIL import Image
    scale = (dpi or RENDER_DPI) / 72.0
    with converter._PDFIUM_LOCK:
        bitmap = page.render(scale=scale)
        pil = bitmap.to_pil()
        pil = pil.copy()                    # detach from the bitmap buffer
    del bitmap
    if pil.mode in ("RGBA", "LA", "P"):
        background = Image.new("RGB", pil.size, (255, 255, 255))
        mask = pil.split()[-1] if pil.mode in ("RGBA", "LA") else None
        background.paste(pil, mask=mask)
        pil = background
    elif pil.mode != "RGB":
        pil = pil.convert("RGB")
    return pil


def _fit_long_edge(pil, edge):
    """Downscale so the longest side is at most ``edge`` (never upscale)."""
    from PIL import Image
    edge = int(edge or 0)
    if edge <= 0 or max(pil.size) <= edge:
        return pil
    ratio = edge / float(max(pil.size))
    return pil.resize((max(1, int(pil.width * ratio)),
                       max(1, int(pil.height * ratio))), Image.LANCZOS)


def _save_page(job_id, pil, page_no, cfg=None):
    """Save a vision page at the pixel/quality budget, never the byte cap."""
    edge = int(getattr(cfg, "studio_vision_long_edge", VISION_LONG_EDGE)
               or VISION_LONG_EDGE)
    quality = int(getattr(cfg, "studio_vision_quality", VISION_QUALITY)
                  or VISION_QUALITY)
    dest = workspace.page_image_path(job_id, page_no)
    _fit_long_edge(pil, edge).save(dest, "JPEG", quality=quality,
                                   optimize=True)
    return dest


# ── figure assets (saved once, referenced by id) ────────────────────────────

def _overlaps(a, b):
    """Two normalized yxxy boxes share area."""
    if not a or not b:
        return False
    return (min(a[2], b[2]) > max(a[0], b[0])
            and min(a[3], b[3]) > max(a[1], b[1]))


def _save_asset(job_id, pil, bounds, page_no, width, height, scale, assets):
    """Crop one figure off a render, cap at 200 KB, dedupe by content id."""
    left, bottom, right, top = bounds
    box = (max(0, int(left * scale)), max(0, int((height - top) * scale)),
           min(pil.width, int(right * scale) + 1),
           min(pil.height, int((height - bottom) * scale) + 1))
    if box[2] - box[0] < 12 or box[3] - box[1] < 12:
        return None
    tmp = workspace.asset_path(job_id, "tmp-%d-%d" % (page_no, len(assets)))
    try:
        converter._save_capped(pil.crop(box), tmp, converter.QUALITY_START)
        with open(tmp, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    asset_id = "img-" + hashlib.sha1(data).hexdigest()[:12]
    dest = workspace.asset_path(job_id, asset_id)
    if not os.path.exists(dest):
        with open(dest, "wb") as fh:
            fh.write(data)
    record = {"id": asset_id, "page": page_no, "size": len(data),
              "w": box[2] - box[0], "h": box[3] - box[1],
              "bbox": layout.pdf_rect_to_norm(bounds, width, height)}
    return record


# ── block assembly ──────────────────────────────────────────────────────────

def _option_texts(options):
    """Layout's {label, text} options → strings that keep the printed label.

    One canonical form downstream: ``"ب) متن گزینه"`` — Stage 2 repairs it,
    the review panel shows one option per line, and the importer re-parses
    the label back out with ``questions.normalize_options``.
    """
    out = []
    for option in options or []:
        if isinstance(option, dict):
            label = str(option.get("label") or "").strip()
            text = str(option.get("text") or "").strip()
            out.append(("%s) %s" % (label, text)).strip() if label else text)
        elif option is not None:
            out.append(str(option).strip())
    return [o for o in out if o]

def _block(key, kind, page_no, order, number=None, label=None, text="",
           options=None, metadata_line=None, bbox=None, bbox_source=None,
           images=None, raw_ocr=None, source=None, split=None):
    metadata = layout.metadata_fields(metadata_line)
    return {
        "key": key, "kind": kind, "page": page_no, "order": order,
        "number": number, "label": label,
        "text": text, "options": _option_texts(options),
        "metadata_line": metadata_line,
        "corp": metadata["corp"], "year": metadata["year"],
        "topic": None, "difficulty": metadata["difficulty"],
        "has_figure": bool(images),
        "images": images or [],
        "bbox": bbox, "bbox_source": bbox_source,
        "split": split, "split_joined": False,
        "raw_ocr_text": raw_ocr,
        "source": source or {},
        "missing": [], "fixes": [], "stall": None, "needs_human": False,
    }


def _key_entry_blocks(text, page_no, source):
    return [
        _block("p%d-k%d" % (page_no, index), "answer_key_entry", page_no,
               index, number=entry["number"], label=entry["label"],
               text="", source=source)
        for index, entry in enumerate(layout.key_entries(text), start=1)
    ]


def _text_page(job_id, page, page_no, is_key_page, assets):
    """Deterministic lane for one page with a real text layer."""
    text = _page_text(page)
    source = {"method": "text", "page": page_no}
    if is_key_page:
        return ({"page": page_no, "method": "text", "text": text, "images": []},
                _key_entry_blocks(text, page_no, source))

    try:
        width, height = page.get_size()
    except Exception:
        width = height = 0
    blocks = layout.split_text(text)
    boxes = _char_boxes(page)

    figures = []
    if width and height:
        bounds = _figure_bounds(page)
        if bounds:
            pil = _render_pil(page)
            scale = pil.width / width
            for one in bounds:
                if len(assets) >= ASSET_LIMIT:
                    break
                record = _save_asset(job_id, pil, one, page_no, width, height,
                                     scale, assets)
                if record:
                    figures.append(record)
                    assets.append(record)

    records, claimed = [], set()
    for index, raw in enumerate(blocks, start=1):
        rects = []
        for start, end in raw["ranges"]:
            for ci in range(max(0, start), min(end, len(boxes))):
                if boxes[ci]:
                    rects.append(boxes[ci])
        bbox = layout.pdf_rect_to_norm(layout.union_rects(rects), width, height)
        images = [f["id"] for f in figures if _overlaps(bbox, f["bbox"])]
        for f in figures:
            if f["id"] in images:
                bbox = layout.norm_union(bbox, f["bbox"])
        claimed.update(images)
        raw_ocr = "\n".join(text[s:e] for s, e in raw["ranges"])
        records.append(_block(
            "p%d-b%d" % (page_no, index), raw["kind"], page_no, index,
            number=raw["number"], text=raw["text"], options=raw["options"],
            metadata_line=raw["metadata_line"], bbox=bbox,
            bbox_source="text_layer" if bbox else None, images=images,
            raw_ocr=raw_ocr, source=source))

    # A figure that overlaps no block still belongs to the block it follows
    # (the classic "figure straight under the stem" placement).
    for fig in figures:
        if fig["id"] in claimed:
            continue
        owner = None
        for rec in records:
            bbox = rec.get("bbox")
            if not bbox or bbox[0] > fig["bbox"][0]:
                continue
            if owner is None or bbox[0] > owner["bbox"][0]:
                owner = rec
        if (owner is None
                or fig["bbox"][0] - owner["bbox"][2] > FIGURE_GAP):
            continue
        owner["images"] = owner["images"] + [fig["id"]]
        owner["has_figure"] = True
        owner["bbox"] = layout.norm_union(owner["bbox"], fig["bbox"])
        claimed.add(fig["id"])

    page_images = [f["id"] for f in figures if f["id"] not in claimed]
    return ({"page": page_no, "method": "text", "text": text,
             "images": page_images}, records)


def _vision_text(job_id, cfg, image_path, page_no):
    """One OCR call over a stored page image; returns (text, route)."""
    texts, route = _vision_texts(cfg, [image_path], [page_no])
    return texts.get(page_no) or "", route


def _vision_texts(cfg, image_paths, page_nos):
    """OCR a chunk's pages in batches of ``studio_pages_per_call``.

    Both lanes accept several images per request; the batch size is a setting
    so a slow tunnel can be traded for fewer, larger requests. Page markers
    keep the reply attributable to its page.
    """
    payloads = []
    for path in image_paths:
        with open(path, "rb") as fh:
            payloads.append(base64.b64encode(fh.read()).decode("ascii"))
    per_call = max(1, int(getattr(cfg, "studio_pages_per_call", 3) or 3))
    texts, route = {}, None
    for start in range(0, len(payloads), per_call):
        batch = payloads[start:start + per_call]
        batch_pages = page_nos[start:start + per_call]
        blob = batch[0] if len(batch) == 1 else batch
        result = llm.call_stage(cfg, "extract", OCR_PAGE_PROMPT, blob,
                                "image/jpeg", raw=True)
        route = result.get("route") or route
        text = (result.get("text") or "").strip()
        texts.update(_split_page_markers(text, batch_pages))
    return texts, route


def _split_page_markers(text, page_nos):
    """Reply -> {page_no: text} on the prompt's request-local page markers.

    An unmarked reply stays whole on the first page, so a model that ignores
    rule 7 costs page precision, never text.
    """
    found, index = {}, 0
    for line in (text or "").splitlines():
        match = PAGE_MARK_RE.match(line)
        if match:
            index = int(match.group(1))
            continue
        found.setdefault(index, []).append(line)
    if len(page_nos) == 1 or not [i for i in found if i > 1]:
        whole = "\n".join(found.get(0, []) + found.get(1, [])).strip()
        return {page_nos[0]: whole or (text or "").strip()}
    out = {}
    for offset, page_no in enumerate(page_nos, start=1):
        lines = found.get(offset, [])
        if offset == 1:
            lines = found.get(0, []) + lines
        out[page_no] = "\n".join(lines).strip()
    return out


def _merge_split(current, flag):
    """Combine two split flags for one block ("both" is the widest)."""
    if not current:
        return flag
    if current == flag:
        return current
    return "both"


def _apply_split(blocks, flag):
    """Attach a page's SPLIT marker to the block it belongs to."""
    if not blocks or not flag:
        return blocks
    if flag in ("head", "both"):
        blocks[0]["split"] = _merge_split(blocks[0].get("split"), "head")
    if flag in ("tail", "both"):
        blocks[-1]["split"] = _merge_split(blocks[-1].get("split"), "tail")
    return blocks


def _vision_blocks(text, page_no, source):
    clean, split = layout.parse_split(text)
    if probe_mod._looks_like_answer_key(clean):
        return _key_entry_blocks(clean, page_no, source)
    blocks = [
        _block("p%d-b%d" % (page_no, index), raw["kind"], page_no, index,
               number=raw["number"], text=raw["text"], options=raw["options"],
               metadata_line=raw["metadata_line"], bbox=None,
               bbox_source=None, images=[], raw_ocr=clean, source=source)
        for index, raw in enumerate(layout.split_text(clean), start=1)
    ]
    return _apply_split(blocks, split)


def _chunk_bounds(cfg):
    """(max_chars, max_pages) from the config; a bare dict means defaults."""
    chars = int(getattr(cfg, "studio_chunk_chars", 24000) or 24000)
    images = int(getattr(cfg, "studio_chunk_images", 3) or 3)
    return chars, images


def _stamp_window(blocks, window):
    """Every block names the request window it came from."""
    for block in blocks:
        block["page_start"] = window["page_start"]
        block["page_end"] = window["page_end"]
    return blocks


def _image_source_page(job_id, cfg, src):
    """The whole source is one loose image — a single vision page."""
    from PIL import Image
    pil = Image.open(src)
    pil.load()
    if pil.mode != "RGB":
        pil = pil.convert("RGB")
    dest = _save_page(job_id, pil, 1, cfg)
    source = {"method": "vision", "page": 1,
              "image": os.path.relpath(dest, workspace.job_dir(job_id))}
    text, route = _vision_text(job_id, cfg, dest, 1)
    record = {"page": 1, "method": "vision", "text": text,
              "image": source["image"], "route": route}
    return record, _vision_blocks(text, 1, source)


# ── the stage entry point ───────────────────────────────────────────────────

def run(job_id, cfg, emit=None):
    """Extract a job's source into layer1.json. Returns the job summary."""
    emit = emit or (lambda *args, **kwargs: None)
    src = workspace.source_path(job_id)
    if not src:
        raise ValueError("job has no source file")
    profile = workspace.read_artifact(job_id, "profile") or {}
    report = workspace.read_artifact(job_id, "probe") or {}
    key_pages = set(report.get("answer_key_pages") or [])

    assets, pages, blocks = [], [], []
    stats = {"text_pages": 0, "vision_pages": 0, "assets": 0, "calls": 0,
             "blocks": 0, "joined": 0, "errors": []}

    if os.path.splitext(src)[1].lower() == ".pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(src)
        try:
            total = len(doc)
            emit("extract", "info", "reading %d page(s)" % total)
            counts = [len(re.sub(r"\s", "", _page_text(doc[index])))
                      for index in range(total)]
            max_chars, max_pages = _chunk_bounds(cfg)
            windows = chunker.chunk_pages(counts, max_chars=max_chars,
                                          max_pages=max_pages)
            for window in windows:
                emit("extract", "info",
                     "chunk %d/%d - pages %d-%d"
                     % (window["index"], window["chunks"],
                        window["page_start"], window["page_end"]),
                     chunk=window["index"], chunks=window["chunks"],
                     page_start=window["page_start"],
                     page_end=window["page_end"])
                results, vision = {}, []
                for page_no in window["pages"]:
                    page = doc[page_no - 1]
                    if counts[page_no - 1] >= probe_mod.TEXT_PAGE_MIN_CHARS:
                        results[page_no] = _text_page(
                            job_id, page, page_no, page_no in key_pages, assets)
                        stats["text_pages"] += 1
                    else:
                        vision.append(page_no)
                if vision:
                    paths = []
                    for page_no in vision:
                        pil = _render_pil(doc[page_no - 1])
                        paths.append(_save_page(job_id, pil, page_no, cfg))
                    per_call = max(1, int(getattr(cfg, "studio_pages_per_call",
                                                  3) or 3))
                    stats["calls"] += ((len(vision) + per_call - 1)
                                       // per_call)
                    try:
                        texts, route = _vision_texts(cfg, paths, vision)
                        failures = {}
                    except Exception as exc:
                        texts, route = {}, None
                        failures = {page_no: str(exc) for page_no in vision}
                        for page_no in vision:
                            stats["errors"].append({"page": page_no,
                                                    "error": str(exc)})
                        emit("extract", "error",
                             "pages %d-%d: %s"
                             % (window["page_start"], window["page_end"],
                                exc))
                    stats["vision_pages"] += len(vision)
                    for page_no in vision:
                        text = texts.get(page_no) or ""
                        source = {"method": "vision", "page": page_no,
                                  "image": os.path.relpath(
                                      workspace.page_image_path(job_id,
                                                                page_no),
                                      workspace.job_dir(job_id))}
                        record = {"page": page_no, "method": "vision",
                                  "text": text, "image": source["image"],
                                  "route": route}
                        if page_no in failures:
                            record["error"] = failures[page_no]
                        results[page_no] = (record,
                                            _vision_blocks(text, page_no,
                                                           source))
                for page_no in window["pages"]:
                    record, page_blocks = results[page_no]
                    _stamp_window(page_blocks, window)
                    pages.append(record)
                    blocks.extend(page_blocks)
        finally:
            try:
                doc.close()
            except Exception:
                pass
    else:
        emit("extract", "info", "one image via vision")
        record, page_blocks = _image_source_page(job_id, cfg, src)
        stats["vision_pages"] = 1
        stats["calls"] = 1
        pages.append(record)
        blocks.extend(page_blocks)

    layout.mark_continuations(blocks)
    blocks, joined = layout.join_continuations(blocks)
    stats["joined"] = joined
    for order, block in enumerate(blocks, start=1):
        block["order"] = order
    stats["blocks"] = len(blocks)
    stats["assets"] = len(assets)

    layer1 = {
        "version": 1,
        "job_id": job_id,
        "source": workspace.source_name(job_id) or os.path.basename(src),
        "kind": profile.get("source_kind") or report.get("source_kind"),
        "profile": profile,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pages": pages,
        "blocks": blocks,
        "assets": assets,
        "stats": stats,
    }
    workspace.write_artifact(job_id, "layer1", layer1)
    workspace.log_event(
        job_id, "extract", "success",
        "extracted %d block(s) - text pages %d, vision pages %d, "
        "assets %d, joined %d"
        % (len(blocks), stats["text_pages"], stats["vision_pages"],
           stats["assets"], stats["joined"]))
    return {"job": workspace.summary(job_id), "stats": stats}
