"""Stage 0 — probe a source file *before* any AI runs.

Deterministic and cheap: read the PDF with pdfium, count characters per page
to decide whether a text layer exists, measure page sizes, spot figure-heavy
pages, and heuristically flag answer-key pages (dense number/option grids).
The result is the raw material a small inspection agent (or the user) turns
into a **profile** (see ``profile.py``) — the document that says which fields
are static and which must be extracted per question.

No network, no model, no writes: ``probe(path)`` returns a plain dict.
"""
import os
import re

# A page whose extracted text has at least this many non-space characters is
# treated as text-selectable. Real exam pages carry hundreds; a scanned page
# carries a handful of stray glyphs (or none).
TEXT_PAGE_MIN_CHARS = 40

# Answer-key pages look like long runs of "<number> <option-digit>" pairs.
_KEY_ROW = re.compile(r"\d+\s*[\)\.]?\s*[\u06F0-\u06F9\u0660-\u06691-4]\b")
_KEY_HINT = re.compile(r"پاسخ\s*کلیدی|کلید\s*پاسخ")
# A line with this many Persian letters is a sentence (a question stem or an
# option text), never a key-grid cell. Computed pages carry several of them;
# a key grid carries digits only.
_KEY_SENTENCE_LETTERS = 8
_FA_LETTER = re.compile(r"[\u0600-\u06FF]")


def _page_text(page):
    try:
        return page.get_textpage().get_text_range() or ""
    except Exception:
        return ""


def _count_figures(page):
    """Rough figure count: pdfium image objects on the page.

    pdfium object types (pypdfium2.raw): 1 = text, 2 = path, 3 = image.
    Images are the reliable "this page has a picture" signal; vector paths
    flood-count rules and text on every page, so only 3 counts here.
    """
    figures = 0
    try:
        for obj in page.get_objects():
            if obj.type == 3:          # image object
                figures += 1
    except Exception:
        return 0
    return figures


def _looks_like_answer_key(text: str) -> bool:
    """True when a page reads like a bare answer-key grid (no prose).

    A question page spells its options as "۱) ۲/۶", so the pair pattern alone
    matches half of a normal exam page; the sentence count is what separates
    the two. Misreading a question page as a key would drop every stem, while
    misreading a key as questions only adds recoverable prose blocks.
    """
    if _KEY_HINT.search(text):
        return True
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) < 60:
        return False
    hits = len(_KEY_ROW.findall(compact))
    if hits < 8:
        return False
    sentences = sum(1 for line in (text or "").splitlines()
                    if len(_FA_LETTER.findall(line)) >= _KEY_SENTENCE_LETTERS)
    if sentences > 1:
        return False
    # Many number/option pairs and few sentence-like stretches (Persian words
    # with a trailing period/question mark) => a key table.
    prose = len(re.findall(r"[\u0600-\u06FF]{4,}[.؟!]", compact))
    return prose <= 2


def probe(path: str, max_pages: int = None) -> dict:
    """Inspect one PDF/image and return a probe report.

    Returns a dict with ``ok`` False and an ``error`` when the file cannot be
    opened, so the caller never has to guard against exceptions.
    """
    if not path or not os.path.isfile(path):
        return {"ok": False, "error": "source file not found", "path": path}

    ext = os.path.splitext(path)[1].lower()
    if ext != ".pdf":
        # A single image: no text layer to read; it is a one-page vision job.
        return {
            "ok": True, "path": path, "file_type": ext.lstrip("."),
            "page_count": 1, "text_pages": 0, "image_pages": 1,
            "has_text_layer": False, "source_kind": "images",
            "pages": [], "figure_pages": [], "answer_key_pages": [],
            "size_bytes": os.path.getsize(path),
        }

    try:
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(path)
    except Exception as exc:
        return {"ok": False, "error": f"cannot open PDF: {exc}", "path": path}

    try:
        total = len(doc)
        pages = []
        text_pages = image_pages = 0
        figure_pages, key_pages = [], []
        for i in range(total):
            if max_pages is not None and i >= max_pages:
                break
            page = doc[i]
            text = _page_text(page)
            chars = len(re.sub(r"\s", "", text))
            try:
                width, height = page.get_size()
            except Exception:
                width = height = 0
            figures = _count_figures(page)
            is_text = chars >= TEXT_PAGE_MIN_CHARS
            if is_text:
                text_pages += 1
            else:
                image_pages += 1
            if figures >= 1:
                figure_pages.append(i + 1)
            if _looks_like_answer_key(text):
                key_pages.append(i + 1)
            pages.append({"page": i + 1, "chars": chars, "figures": figures,
                          "width": round(width, 1), "height": round(height, 1),
                          "text": is_text})

        if text_pages and image_pages:
            kind = "mixed"
        elif text_pages:
            kind = "text_pdf"
        else:
            kind = "image_pdf"

        return {
            "ok": True, "path": path, "file_type": "pdf",
            "page_count": total,
            "probed_pages": len(pages),
            "text_pages": text_pages, "image_pages": image_pages,
            "has_text_layer": text_pages > 0,
            "source_kind": kind,
            "figure_pages": figure_pages[:200],
            "answer_key_pages": key_pages[:200],
            "pages": pages[:50],          # a sample; the count is authoritative
            "size_bytes": os.path.getsize(path),
        }
    finally:
        try:
            doc.close()
        except Exception:
            pass


def suggest_profile(probe_report: dict) -> dict:
    """A first-cut profile from the probe alone (no AI).

    This is what the UI shows as the editable default; a small inspection
    agent may refine it, and the user can always override every field.
    """
    if not probe_report.get("ok"):
        return {"source_kind": "unknown", "compilation": "unknown",
                "static": {}, "extract_per_question": [], "answer_layout": "unknown"}

    kind = probe_report.get("source_kind", "unknown")
    key_pages = probe_report.get("answer_key_pages") or []
    total = probe_report.get("page_count") or 0

    # Answer layout heuristic: key pages clustered at the very end => the
    # common "questions first, key at the back" compilation.
    if key_pages and total and min(key_pages) >= max(1, int(total * 0.8)):
        answer_layout = "key_pages_at_end"
    elif key_pages:
        answer_layout = "separate_section"
    else:
        answer_layout = "unknown"

    return {
        "source_kind": kind,
        "compilation": "unknown",       # an agent / the user fills this in
        "static": {"subject": None, "corp": None, "year": None, "grade": None},
        "extract_per_question": ["corp", "year", "topic", "difficulty"],
        "answer_layout": answer_layout,
        "notes": ("text layer present on %d/%d pages" % (
            probe_report.get("text_pages", 0), total)),
    }
