"""Row building: merged view → DB-shaped questions + answers (pure).

Kept free of I/O on purpose: the batch importer, the JSON export and the
tests all build rows through here, so what the UI approved is exactly what a
target writes.
"""
import json
import re

from pipeline import questions as questions_mod
from pipeline.utils import norm_text, parse_number, sha1_hex, to_english_digits

from pipeline.studio import merge

TARGETS = (("supabase", "Database"),
           ("file", "JSON export"))

# The printed option labels a key page can use, in option order.
_LABELS = (("1", "الف", "ا"), ("2", "ب"), ("3", "ج"), ("4", "د"),
           ("5", "ه"), ("6", "و"))

def targets() -> list:
    """[{id, label}] for the Import panel's target picker."""
    return [{"id": t, "label": label} for t, label in TARGETS]


# MathJax 3 rejects a bare one-character subscript inside \mathrm when another
# subscripted letter follows it ("Double subscripts: use braces to clarify"),
# which the OCR output hits as C_nH_{2n+2} / C_6H_{10}. Bracing renders the
# same and clears the error box the dashboard would otherwise show.
_MATH_RM = re.compile(r"\\mathrm\{((?:[^{}]|\{[^{}]*\})*)\}")
_BARE_SUB = re.compile(r"_(?!\{)([A-Za-z0-9])")


def brace_subscripts(text):
    """`\\mathrm{C_nH_2}` -> `\\mathrm{C_{n}H_2}`; everything else untouched."""
    if not text or "\\mathrm" not in text:
        return text
    return _MATH_RM.sub(
        lambda m: "\\mathrm{" + _BARE_SUB.sub(r"_{\1}", m.group(1)) + "}", text)


# The OCR does not settle on one notation: one page writes \mathrm{CaCO_{3}},
# the next writes C_2H_6 with Persian digits inside the maths (H=۱۶), and
# MathJax renders those as slanted single letters with Persian numerals instead
# of a printed formula. Normalise every math span to the house style.
_MATH_SPAN = re.compile(
    r"\$\$[\s\S]*?\$\$|\$[^$]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]")
_ELEMENT = r"[A-Z][a-z]?(?:_\{?\d+\}?)?"
_STATE = r"(?:\((?:g|l|s|aq)\))?"
_CHEM_RUN = re.compile(
    # a run of >=2 element groups (C_2H_6, N_2O_5, SO_2) - an OCR coefficient
    # in front (2N_2) is left outside the match
    r"(?<![A-Za-z\\{])(?:" + _ELEMENT + r"){2,}" + _STATE +
    # or one element carrying a subscript (O_2, H_2), never a bare variable (H, K, X)
    r"|(?<![A-Za-z\\{])(?:[A-Z][a-z]?)_\{?\d+\}?" + _STATE)
_ELEMENT_PARTS = re.compile(r"([A-Z][a-z]?)(?:_\{?(\d+)\}?)?")
_BOND = re.compile(
    r"(?<![A-Za-z\\{])(?:[A-Z][a-z]?(?:\s*[-=\u2261]\s*[A-Z][a-z]?)+)(?![A-Za-z0-9])")
_MACRO_START = re.compile(r"\\(?:mathrm|text)\{")
# Persian prints decimals with a slash (۲/۵۶ is 2.56) - inside maths a plain
# slash would read as a fraction, so only a Persian-typed slash becomes a dot.
_PERSIAN_DECIMAL = re.compile(r"([۰-۹]+)\s*[/٫]\s*([۰-۹]+)")


def _protected_spans(text):
    """(start, end) of every \\mathrm{...} / \\text{...} group, braces balanced."""
    spans, pos = [], 0
    while True:
        match = _MACRO_START.search(text, pos)
        if not match:
            return spans
        depth, index = 0, match.end() - 1          # the opening brace
        while index < len(text):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if index >= len(text):                     # unbalanced source: leave it be
            return spans
        spans.append((match.start(), index + 1))
        pos = index + 1



def _upright(match):
    """`C_2H_6(g)` -> `\\mathrm{C}_{2}\\mathrm{H}_{6}\\mathrm{(g)}`."""
    token, state = match.group(0), ""
    trail = re.search(r"\((?:g|l|s|aq)\)$", token)
    if trail:
        state = "\\mathrm{%s}" % trail.group(0)
        token = token[:trail.start()]
    out, letters = [], ""
    for element, count in _ELEMENT_PARTS.findall(token):
        letters += element
        if count:
            out.append("\\mathrm{%s}_{%s}" % (letters, count))
            letters = ""
    if letters:
        out.append("\\mathrm{%s}" % letters)
    return "".join(out) + state


def _outside_macros(text, change):
    """Apply ``change`` only between \\mathrm{...} / \\text{...} groups."""
    out, pos = [], 0
    for start, end in _protected_spans(text):
        out.append(change(text[pos:start]))
        out.append(text[start:end])
        pos = end
    out.append(change(text[pos:]))
    return "".join(out)


def normalise_math(text):
    """Math spans: Latin digits, upright chemical formulas (book notation)."""
    if not text or ("$" not in text and "\\(" not in text and "\\[" not in text):
        return text

    def span(match):
        text = _PERSIAN_DECIMAL.sub(r"\1.\2", match.group(0))
        digits = to_english_digits(text)
        return _outside_macros(
            digits,
            lambda part: _BOND.sub(lambda b: "\\mathrm{%s}" % b.group(0),
                                   _CHEM_RUN.sub(_upright, part)))

    return _MATH_SPAN.sub(span, text)




def _clean_label(value):
    return to_english_digits(str(value or "")).strip().strip("()[]-.").strip()


def _same_label(a, b):
    """"2" vs "۲", "ب" vs the same letter written differently: same option."""
    if a == b:
        return True
    return any(a in names and b in names for names in _LABELS)


def option_text(options, label):
    """The option text a key letter/number points at, or None.

    ``options`` is the normalized [{label, text}] list; a label match wins,
    position is the fallback for lists that carry no labels.
    """
    want = _clean_label(label)
    if not want or not options:
        return None
    for option in options:
        if isinstance(option, dict) \
                and _same_label(_clean_label(option.get("label")), want):
            return option.get("text")
    for index, names in enumerate(_LABELS):
        if want in names and index < len(options):
            option = options[index]
            return option.get("text") if isinstance(option, dict) else option
    return None

def diagram_urls(view, blob=None):
    """Where each approved crop lives for a target.

    Without ``blob`` this is the JSON export's manifest: every approved crop
    gets its job-relative path (the review panel renders it on demand). With
    a ``blob`` of uploaded image entries (``{object, bbox, page, url}``), a
    crop that failed to upload is left out — and its question stays queued
    instead of importing bare.
    """
    out = {}
    for key, state in (view.get("crops") or {}).items():
        if not state.get("approved"):
            continue
        if blob is not None:
            if key in blob:
                out[key] = blob[key]
            continue
        if state.get("file"):
            out[key] = state["file"].replace("\\", "/")
        else:
            out[key] = "crops/%s.jpg" % key
    return out

def build_rows(view, diagram_urls=None):
    """Rows for the importable part of a merged job.

    A block whose crop is needed but missing from ``diagram_urls`` stays
    queued, so a failed upload can never import a question without its figure.
    """
    diagram_urls = diagram_urls or {}
    source_id = "studio-%s" % view.get("job_id")
    questions, answers, skipped, imported = [], [], [], []
    by_number = {}
    for block in view.get("blocks") or []:
        key = block.get("key")
        ok, reason = merge.importable(block)
        if ok and (block.get("crop") or {}).get("needed") \
                and key not in diagram_urls:
            ok, reason = False, "crop not uploaded"
        if not ok:
            skipped.append({"key": key, "reason": reason})
            continue
        imported.append(key)
        if block.get("kind") == "question":
            row = _question_row(source_id, block, diagram_urls.get(key))
            questions.append(row)
            number = parse_number(block.get("number"))
            if number is not None:
                by_number.setdefault(str(number), {
                    "id": row["id"],
                    "options": questions_mod.normalize_options(
                        block.get("options"))})
        else:
            answers.append(_answer_row(source_id, block, by_number))
    return {"questions": questions, "answers": answers, "skipped": skipped,
            "imported": imported}

def marks(view, rows):
    """Per-block import marks for imports.json (the UI's imported badge)."""
    skipped = {s["key"]: s.get("reason") for s in rows["skipped"]}
    done = set(rows.get("imported") or [])
    out = []
    for block in view.get("blocks") or []:
        key = block.get("key")
        if key in done:
            out.append({"key": key, "status": "ok"})
        else:
            out.append({"key": key, "status": "skipped",
                        "reason": skipped.get(key) or "not a question"})
    return out

def _image_entry(value):
    """Normalize a target's crop value to one image dict (dict or bare path)."""
    if isinstance(value, dict):
        return value
    if value:
        return {"object": value, "url": value}
    return {}


def media_images(block, entry):
    """The row's JSON-array entries for one block (reading order: one per crop)."""
    image = _image_entry(entry)
    obj = image.get("object")
    if not obj:
        return []
    bbox = (block.get("crop") or {}).get("bbox") or image.get("bbox")
    real = list(bbox) if (bbox and questions_mod.is_real_bbox(bbox)) else None
    return [{"object": obj, "bbox": real,
             "page": int(image.get("page") or 0)}]


def figure_marker(block, count):
    """The inline marker for a figure the text lane could not reproduce."""
    if not block.get("figure_marker") or not count:
        return None
    return "[[figure:%d]]" % count


def _question_row(source_id, block, image):
    image = _image_entry(image)
    bbox = (block.get("crop") or {}).get("bbox") or image.get("bbox")
    url = image.get("url") or image.get("object")
    real = bbox if (url and questions_mod.is_real_bbox(bbox)) else None
    pictures = media_images(block, image)
    text = normalise_math(brace_subscripts(block.get("text")))
    options = [dict(option, text=normalise_math(brace_subscripts(option.get("text"))))
               if isinstance(option, dict) else option
               for option in questions_mod.normalize_options(block.get("options"))]
    marker = figure_marker(block, len(pictures))
    if marker and text and marker not in text:
        text = (text.rstrip() + "\n" + marker).strip()
    return {
        "id": "%s-%s" % (source_id, block.get("key")),
        "source_id": source_id,
        "question_number": parse_number(block.get("number")),
        "question_text": text,
        "options": json.dumps(options, ensure_ascii=False),
        "subject": block.get("subject"),
        "topic": block.get("topic"),
        "tags": json.dumps(block.get("tags") or [], ensure_ascii=False),
        "difficulty": block.get("difficulty"),
        "review_status": "pending",
        "raw_ocr_text": block.get("raw_ocr_text"),
        "diagram_url": url if real else None,
        "diagram_bbox": json.dumps(real) if real else None,
        "media_images": json.dumps(pictures, ensure_ascii=False),
        "grade": block.get("grade"),
        "corp": block.get("corp"),
        "year": block.get("year"),
    }

def _answer_row(source_id, block, by_number):
    number = parse_number(block.get("number"))
    linked = by_number.get(str(number)) if number is not None else None
    label = block.get("label") or block.get("answer")
    return {
        "id": "%s-%s" % (source_id, block.get("key")),
        "source_id": source_id,
        "question_id": linked["id"] if linked else None,
        "question_number": number,
        "answer_explanation": (block.get("text") or "").strip() or None,
        "correct_option_label": label,
        "correct_option_text": option_text(linked["options"], label)
        if linked else None,
        "assets": "[]",
        "subject": block.get("subject"),
        "topic": block.get("topic"),
        "corp": block.get("corp"),
        "year": block.get("year"),
        "difficulty": block.get("difficulty"),
        "tags": json.dumps(block.get("tags") or [], ensure_ascii=False),
        "review_status": "pending" if linked else "unlinked",
        "raw_ocr_text": block.get("raw_ocr_text"),
        "content_hash": sha1_hex(norm_text(block.get("key") or "")),
        "grade": block.get("grade"),
    }
