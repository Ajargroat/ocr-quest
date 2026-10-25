"""Stage 1 — extraction: source → layer1.json (the raw bulk).

Text lane (deterministic, no model): pdfium's text layer + per-character
boxes → ``layout.split_text`` → one block per question with an EXACT
text-layer bbox (the prototype from the plan), and every embedded figure
saved once under a content id — deduped across the whole job, referenced by
blocks, so storage holds each picture exactly one time.

Image lane: each page is rendered through the shared 200 KB compressor and
sent to the stage-1 model with ``OCR_JSON_PROMPT`` (a JSON body carrying the
verbatim transcription — a low-thinking task any model can serve, and the
container keeps the model from re-ordering a right-to-left line); the reply
runs through the SAME splitter, so both lanes produce one bulk schema.

A text page whose layer carries no TeX character is read by the image lane
instead: the formulas on such a page are picture art, and the text lane would
import the prose with the maths silently missing.

Mixed PDFs use both lanes page by page. A vision page that fails is logged
and left empty — the rest of the job still extracts.
"""
import base64
import hashlib
import json
import os
import re
import time

from pipeline import converter, prompts
from pipeline.utils import to_english_digits
from pipeline.studio import (chunker, faults as studio_faults, layout, llm,
                             persian, textnorm, workspace)
from pipeline.studio import probe as probe_mod

# Render resolution for figures, page images and (later) crops.
RENDER_DPI = int(os.getenv("STUDIO_RENDER_DPI", "170"))

# A text page whose TeX characters (``\``, ``^``, ``_``) pdfium exposes is a
# text page; one that draws its formulas as vector art exposes none, and the
# text lane would import the prose with the maths silently missing. That page
# is read by the model instead. The signal is the PAGE, not the block: a page
# that printed real TeX may still have a prose-only block (a header), and
# "the block has no TeX" must not demote that block on its own.
TEXT_MATH_MARKS = ("\\", "^", "_")


def page_has_text_math(text):
    """True when a text layer carries TeX characters a reader can reason on."""
    return any(mark in (text or "") for mark in TEXT_MATH_MARKS)

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
10. An option's label goes on the LEFT of its text exactly as printed: an option printed "(1 ... " starts its line "(1 ... " and one printed "1) ... " starts "1) ... ". Never move a label to the other end of its line, and never swap two options.
Output only the page text."""

# Rule 10 exists because of a measured miss: on a right-to-left page the model
# put the label on the wrong side of the value ("(4 25/3 - 440" for a printed
# "(4 440 - 25/3"), which turns every option into a different answer.

# The same transcription asked for as a JSON body. The container was tried as
# a lever on option ORDER and measured over the whole 67-page booklet: it did
# not settle it (free-form lane 120 flipped option lines, JSON lane 189), so
# the JSON body is kept for a different reason - it returns exactly one field
# per page and the page-marker parse can never split a page wrongly. Option
# order still needs the organize pass (or a human) before an import.
OCR_JSON_PROMPT = """You are a precise OCR engine for Iranian exam pages.

Transcribe the text of every page image in this request, verbatim, in natural
reading order.

Rules:
1. Reproduce Persian text exactly, keeping half-spaces (ZWNJ) and punctuation.
2. Keep question numbers and option labels exactly as printed, on the side of the line the print puts them (۱، ۲، 1) ، الف) …).
3. Keep the line structure: a question stem, each option and each metadata line stay their own line, in the printed order.
4. If formulas, physics, chemistry or math notation appear, write them in clean LaTeX.
5. Do not translate, summarise, explain, renumber or add commentary.
6. If a word is genuinely unreadable, give your best single reading — never a placeholder like [?].
7. When the request carries more than one page image, start each page with one line exactly "=== PAGE <n> ===", n counting from 1 in the order the images were sent.
8. Never cut a question: one question stays one entry. If the last question on this page continues onto the next page, end this page with one line exactly "SPLIT: tail".
9. If the first question on this page continues a question that started on an earlier page, print one line exactly "SPLIT: head" right before that question's text. A question that both continues and continues onwards gets both marker lines.
10. An option's label goes on the LEFT of its text exactly as printed: an option printed "(1 ... " starts its line "(1 ... " and one printed "1) ... " starts "1) ... ". Never move a label to the other end of its line, and never swap two options.
Output ONE JSON object: {"text": "<the whole transcription, newlines as \\n>"} - the transcription and nothing else, no page commentary, no markdown fences."""

# The organize pass: a text page whose deterministic anchor pass is unsure is
# re-read as ONE structure. The image is the source of truth; the text layer is
# only a spelling reference because its reading order is sometimes scrambled.
ORGANIZE_PAGE_PROMPT = """You re-read one exam page and return its questions.

The page image is the source of truth. The text below is the PDF's own text
layer; its reading order is sometimes scrambled and some question numbers are
glued to a formula, so use it only as a spelling reference.

Return ONE JSON object:
{"questions": [{"number": 55, "stem": "...", "options": ["...", "..."],
                "figure": [y0, x0, y1, x1] | null}]}

Rules:
- One entry per question whose printed number is visible on this page, in
  printed order. Never invent a number and never renumber.
- Read the page image in its printed visual order, not the text layer's
  order. Re-join every piece the print splits across lines: a stem, an
  option and a formula each read as one continuous unit again.
- "stem": the full stem as ONE flowing paragraph - no newline character
  anywhere inside. Verbatim, LaTeX kept, option texts excluded. A question
  that continues from the previous page has no printed number here: leave
  it out (the code joins the halves).
- "options": the printed options in printed order, label stripped ("1)" and
  "1(" are labels, not text). Each option is likewise one flowing string
  with no newline inside. Keep the printed count; an unreadable option is
  "" but still occupies its place.
- "figure": the box of the picture that belongs to this question, normalized
  [y0, x0, y1, x1] on a 0-1000 scale over the page, or null when the question
  has no picture. A formula printed as text is not a figure.
- Never merge two questions and never move an option to another question.
- Never let the text layer's scrambled or duplicated lines create words
  the printed page does not carry: trust the image, copy its words.
- Output no commentary and no markdown fences.
"""

# The vision lane's per-page section marker (rule 7 of the prompt).
PAGE_MARK_RE = re.compile(r"^\s*=+\s*PAGE\s+(\d+)\s*=+\s*$", re.IGNORECASE)


def _reply_text(result):
    """The transcription out of a lane reply, JSON container or plain text.

    The JSON body is preferred (see ``OCR_JSON_PROMPT``); a lane that answered
    with prose anyway still carries the page, so its ``text`` is used. An
    ``"text"`` key that is not a string is ignored rather than trusted.
    """
    parsed = (result or {}).get("parsed")
    if isinstance(parsed, str):
        return parsed.strip()
    if isinstance(parsed, dict):
        for key in ("text", "pages"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, list) and value:
                joined = [v if isinstance(v, str) else str(v) for v in value]
                return "\n".join(joined).strip()
    return ((result or {}).get("text") or "").strip()


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
    """(left, bottom, right, top) of every genuine figure image on the page.

    Page furniture (a repeated header/footer strip, a full-page scan) and
    tiny decorations (a corner mark) are skipped by the same rule the
    probe uses, so the two never disagree about what a figure is.
    """
    return probe_mod.figure_bounds(page)[:FIGURES_PER_PAGE]


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
    # The text layer of a scanned-but-textual PDF carries no half-spaces
    # (see pipeline.studio.persian), so every block is repaired here - the one
    # place every lane and every source shape passes through. Math is folded to
    # its canonical delimiters AFTER that repair (a half-space inside a formula
    # is not a word joiner), so a stored block already carries the form the
    # renderer shows.
    text = textnorm.canonical(persian.restore(text or ""))
    options = _option_texts(options)
    return {
        "key": key, "kind": kind, "page": page_no, "order": order,
        "number": number, "label": label,
        "text": text,
        "options": [textnorm.canonical(persian.restore(o)) for o in options],
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


def _text_page(job_id, page, page_no, is_key_page, assets, start=None):
    """Deterministic lane for one page with a real text layer."""
    text = _page_text(page)
    boxes = _char_boxes(page)
    source = {"method": "text", "page": page_no}
    if is_key_page:
        return ({"page": page_no, "method": "text", "text": text,
                 "images": [], "answer_key": True},
                _key_entry_blocks(text, page_no, source))

    try:
        width, height = page.get_size()
    except Exception:
        width = height = 0
    # The organizer cuts the page at its question anchors first; a page it
    # cannot anchor falls back to the plain line splitter.
    organized = layout.split_page(text, boxes=boxes,
                                  page_size=(width, height), start=start)
    blocks = organized["blocks"]

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
             "images": page_images,
             "layout": {"confidence": organized["confidence"],
                        "anchors": organized["anchors"],
                        "numbers": organized.get("numbers") or [],
                        "questions": organized["questions"],
                        "last": organized["last"]}}, records)


def _as_number(raw):
    """A question number from a model/JSON value, or None."""
    try:
        text = to_english_digits(str(raw)).strip()
    except Exception:
        return None
    if not text.isdigit():
        return None
    value = int(text)
    return value if 1 <= value <= 999 else None


def _organize_figure(raw):
    """A validated [y0, x0, y1, x1] figure box, or None."""
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return None
    try:
        box = [int(round(float(value))) for value in raw]
    except (TypeError, ValueError):
        return None
    if min(box) < 0 or max(box) > 1000:
        return None
    if box[2] - box[0] < 15 or box[3] - box[1] < 15:
        return None
    return box


def _organize_entries(parsed):
    """Validated questions from one organize reply. Never raises."""
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("questions")
    if not isinstance(raw, list):
        return []
    entries, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        number = _as_number(item.get("number"))
        if number is None or number in seen:
            continue
        stem = str(item.get("stem") or "").strip()
        options = [str(option).strip() for option in item.get("options") or []
                   if str(option or "").strip()][:4]
        figure = _organize_figure(item.get("figure"))
        if not stem and not options:
            continue
        seen.add(number)
        entries.append({"number": number, "stem": stem, "options": options,
                        "figure": figure})
    return entries


def _sanitize_entry(entry, stats):
    """One flowing piece per stem/option; count what needed flattening."""
    raw = (entry.get("stem") or "")
    breaks = "\n" in raw or "\r" in raw
    entry["stem"] = layout.one_line(raw)
    options = []
    for option in entry.get("options") or []:
        breaks = breaks or "\n" in (option or "") \
            or "\r" in (option or "")
        options.append(layout.one_line(option))
    entry["options"] = options
    if breaks:
        stats["organize_sanitized"] += 1


def _organize_enabled(cfg, record, blocks=None, mode="auto"):
    """Should this page be re-read by the extract model?

    ``mode`` is this run's own choice: ``auto`` follows the env default and
    the adaptive trigger, ``off`` never organizes, ``all`` re-reads every
    text page. An explicit per-run choice wins over the env default; the env
    only decides what ``auto`` means.
    """
    mode = (mode or "auto").strip().lower()
    if mode not in ("auto", "off", "all"):
        mode = "auto"
    if not record or record.get("method") != "text":
        return False
    if record.get("answer_key"):
        return False
    if mode == "off":
        return False
    if mode == "all":
        return True
    if not getattr(cfg, "studio_organize", 0):
        return False
    try:
        confidence = float((record.get("layout") or {}).get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 1.0:
        return True
    # A fully-anchored page can still be incoherent: a print that wraps
    # sentences mid-line never passes. Those pages always get re-read.
    for block in blocks or []:
        if layout.mid_sentence_breaks(block.get("text") or ""):
            return True
    return False


def _organize_page(cfg, job_id, page, page_no, record, emit, stats):
    """One organize call over a low-confidence text page. [] on failure."""
    text = record.get("text") or ""
    try:
        pil = _render_pil(page)
        dest = _save_page(job_id, pil, page_no, cfg)
        record["image"] = os.path.relpath(dest, workspace.job_dir(job_id))
        with open(dest, "rb") as fh:
            blob = base64.b64encode(fh.read()).decode("ascii")
        prompt = (prompts.get("studio.organize", ORGANIZE_PAGE_PROMPT)
                  + "\n\npage text:\n" + text)
        stats["organize_calls"] += 1
        result = llm.call_stage(cfg, "extract", prompt, blob, "image/jpeg",
                                raw=False, json_mode=True, temperature=0.1,
                                thinking=0)
    except Exception as exc:
        stats["organize_errors"].append({"page": page_no, "error": str(exc)})
        emit("extract", "error", "organize page %s: %s" % (page_no, exc),
             fault=studio_faults.payload(studio_faults.classify(exc)))
        return []
    parsed = (result or {}).get("parsed")
    if not isinstance(parsed, dict):
        try:
            parsed = json.loads((result or {}).get("text") or "")
        except (TypeError, ValueError):
            parsed = None
    entries = _organize_entries(parsed)
    for entry in entries:
        _sanitize_entry(entry, stats)
    if not entries:
        stats["organize_errors"].append({"page": page_no,
                                         "error": "no usable questions"})
        emit("extract", "warn",
             "organize page %s returned no usable question" % page_no)
    return entries


def _apply_organize(blocks, entries, page_no, span=None):
    """Overlay validated model questions on one page's deterministic blocks.

    A deterministic block for the same number keeps its text-layer bbox (the
    exact geometry), gets the model's clean stem/options, and is marked
    ``organized``. A question the deterministic pass missed becomes a new
    block; no deterministic-only block is ever dropped. ``span`` is the
    page's anchored number range: a model number outside it is a question
    that continues from another page (or a misread), and it is skipped
    instead of becoming a bbox-less row.
    """
    replaced = added = skipped = 0
    by_number = {}
    for block in blocks:
        if block.get("kind") == "question" and block.get("page") == page_no:
            by_number.setdefault(block.get("number"), block)
    for entry in entries:
        number = entry["number"]
        target = by_number.get(number)
        if target is None and span and not span[0] <= number <= span[1]:
            skipped += 1
            continue
        if target is not None:
            if entry["stem"]:
                target["text"] = persian.restore(entry["stem"])
            if len(entry["options"]) >= len(target.get("options") or []):
                target["options"] = [
                    persian.restore("%d) %s" % (index, option))
                    for index, option in enumerate(entry["options"], start=1)
                    if option]
            if entry["figure"]:
                target["figure_bbox"] = entry["figure"]
                target["figure_bboxes"] = [entry["figure"]]
                target["figure_source"] = "organize"
                target["has_figure"] = True
            target["organized"] = True
            replaced += 1
            continue
        options = ["%d) %s" % (index, option)
                   for index, option in enumerate(entry["options"], start=1)
                   if option]
        block = _block("p%d-o%d" % (page_no, number), "question", page_no, 0,
                       number=number, text=entry["stem"], options=options,
                       source={"method": "organize", "page": page_no})
        block["organized"] = True
        if entry["figure"]:
            block["figure_bbox"] = entry["figure"]
            block["figure_bboxes"] = [entry["figure"]]
            block["figure_source"] = "organize"
            block["has_figure"] = True
        blocks.append(block)
        added += 1
    return replaced, added, skipped


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
        prompt = llm.stage_prompt(
            cfg, "extract", prompts.get("studio.ocr_json", OCR_JSON_PROMPT))
        result = llm.call_stage(cfg, "extract", prompt, blob,
                                "image/jpeg", raw=False, json_mode=True,
                                thinking=0)
        route = result.get("route") or route
        text = _reply_text(result)
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
    """A vision page's TEXT becomes blocks.

    The page was read by the model because the text lane cannot trust its
    structure (a scan, or formulas drawn as vector art). The reply is the
    organized text (rule 5 of the OCR prompt), so the deterministic splitter
    turns it into question/prose blocks with their options — the same blocks
    a text page would get. An empty reply yields none: inventing a block for
    a page the model could not read would put empty rows in the job list.
    """
    clean, split = layout.parse_split(text)
    if probe_mod._looks_like_answer_key(clean):
        return _key_entry_blocks(clean, page_no, source)
    blocks = [
        _block("p%d-o%d" % (page_no, index), raw["kind"], page_no, index,
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

def run(job_id, cfg, emit=None, organize="auto"):
    """Extract a job's source into layer1.json. Returns the job summary.

    ``organize`` is the per-run choice for the text-lane model re-read:
    ``auto`` (the default), ``off`` or ``all``.
    """
    emit = emit or (lambda *args, **kwargs: None)
    mode = (organize or "auto").strip().lower()
    if mode not in ("auto", "off", "all"):
        mode = "auto"
    src = workspace.source_path(job_id)
    if not src:
        raise ValueError("job has no source file")
    profile = workspace.read_artifact(job_id, "profile") or {}
    report = workspace.read_artifact(job_id, "probe") or {}
    key_pages = set(report.get("answer_key_pages") or [])

    assets, pages, blocks = [], [], []
    stats = {"text_pages": 0, "vision_pages": 0, "assets": 0, "calls": 0,
             "blocks": 0, "joined": 0, "errors": [], "mathless_pages": 0,
             "organized_pages": 0, "organize_calls": 0,
             "organized_blocks": 0, "organize_skipped": 0,
             "organize_sanitized": 0, "organize_mode": mode,
             "organize_errors": []}
    layout_prev = None

    if os.path.splitext(src)[1].lower() == ".pdf":
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(src)
        try:
            total = len(doc)
            emit("extract", "info", "reading %d page(s)" % total)
            counts = [len(re.sub(r"\s", "", _page_text(doc[index])))
                      for index in range(total)]
            # A text page that draws its formulas as vector art exposes no TeX
            # characters, so the text lane would import prose with the maths
            # silently missing (the crop keeps the picture, the text does not
            # point at it). Such a page is handed to the vision lane instead.
            mathless = sum(1 for page_no in range(1, total + 1)
                           if counts[page_no - 1]
                           >= probe_mod.TEXT_PAGE_MIN_CHARS
                           and not page_has_text_math(_page_text(
                               doc[page_no - 1])))
            if mathless:
                emit("extract", "info",
                     "%d page(s) carry no TeX in their text layer - read by "
                     "the vision lane" % mathless)
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
                    if (counts[page_no - 1] >= probe_mod.TEXT_PAGE_MIN_CHARS
                            and page_has_text_math(_page_text(page))):
                        results[page_no] = _text_page(
                            job_id, page, page_no, page_no in key_pages, assets,
                            start=layout_prev)
                        record = results[page_no][0] or {}
                        layout_prev = ((record.get("layout") or {}).get("last")
                                       or layout_prev)
                        stats["text_pages"] += 1
                    else:
                        vision.append(page_no)
                        if counts[page_no - 1] >= probe_mod.TEXT_PAGE_MIN_CHARS:
                            stats["mathless_pages"] += 1
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
                                exc),
                             fault=studio_faults.payload(
                                 studio_faults.classify(exc)))
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
                    if _organize_enabled(cfg, record, page_blocks, mode):
                        entries = _organize_page(
                            cfg, job_id, doc[page_no - 1], page_no, record,
                            emit, stats)
                        layout_info = record.get("layout") or {}
                        span = None
                        if layout_info.get("anchors"):
                            numbers = layout_info.get("numbers") or []
                            if numbers:
                                span = (min(numbers), max(numbers))
                        replaced, added, skipped = _apply_organize(
                            page_blocks, entries, page_no, span)
                        stats["organize_skipped"] += skipped
                        if entries:
                            stats["organized_pages"] += 1
                            stats["organized_blocks"] += replaced + added
                            emit("extract", "info",
                                 "organized page %s: %d question(s), "
                                 "%d outside the page range"
                                 % (page_no, len(entries), skipped))
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
        "extracted %d block(s) - text pages %d, vision pages %d "
        "(mathless %d), assets %d, joined %d, organized pages %d"
        % (len(blocks), stats["text_pages"], stats["vision_pages"],
           stats["mathless_pages"], stats["assets"], stats["joined"],
           stats["organized_pages"]))
    return {"job": workspace.summary(job_id), "stats": stats}
