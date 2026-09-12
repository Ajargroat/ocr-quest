"""Deterministic polish (Stage 2, per-record half).

Code-only normalisation — no AI. Digit policy per the revision redesign:
digits INSIDE math spans must be English, digits in prose must be Persian.
Every change is logged as an info-severity repair so it can be reviewed.
"""
import json
import re

from ..utils import norm_text, parse_number, to_english_digits, to_persian_digits

MATH_RE = re.compile(r'(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\$[^$\n]+?\$|\\\([\s\S]+?\\\))')
_BARE_ENV_RE = re.compile(r'\\begin\{[a-zA-Z*]+\}[\s\S]*?\\end\{[a-zA-Z]+\}')
# A LaTeX command plus every argument group that follows it, so multi-argument
# commands (\frac{1}{2}, \sqrt[3]{x}) are wrapped whole instead of after the
# first argument, which left the rest as literal braces outside the math span.
_CMD_ARG = r'(?:\{[^{}]*\}|\[[^\]]*\])'
_CMD_RE = re.compile(r'\\[a-zA-Z]+' + _CMD_ARG
                     + r'(?:\s*' + _CMD_ARG
                     + r'|\s*\\[a-zA-Z]+' + _CMD_ARG + r')*')
_SUBSUP_RE = re.compile(r'(?<!\$)([A-Za-z0-9\)\]](?:[_^]\{?[A-Za-z0-9]+\}?)+)')
_OPTION_RE = re.compile(r'^\s*([^)\.ـ:]{1,3})\s*[\)\.ـ:]\s*(.*)$')


def fix_coeffs(t: str) -> str:
    """Removes stray underscore coefficients before digits."""
    return re.sub(r'(?<![A-Za-z0-9\)\}])_(?=\d)', '', str(t))


def wrap_bare(t: str) -> str:
    """Wraps bare LaTeX constructs in $...$ / $$...$$ delimiters."""
    s = str(t)
    s = _BARE_ENV_RE.sub(lambda m: '$$' + m.group(0) + '$$', s)
    s = _CMD_RE.sub(lambda m: '$' + m.group(0) + '$', s)
    s = _SUBSUP_RE.sub(lambda m: '$' + m.group(0) + '$', s)
    return s


def sanitize_math(raw):
    """Applies fix_coeffs inside math spans and wrap_bare outside them."""
    if not raw:
        return raw
    parts = MATH_RE.split(str(raw))
    out = []
    for i, part in enumerate(parts):
        out.append(fix_coeffs(part) if i % 2 == 1 else wrap_bare(fix_coeffs(part)))
    return ''.join(out)


def apply_digit_policy(raw):
    """English digits inside math spans, Persian digits in prose.

    Run AFTER sanitize_math so freshly wrapped spans are honoured.
    """
    if not raw:
        return raw
    parts = MATH_RE.split(str(raw))
    out = []
    for i, part in enumerate(parts):
        out.append(to_english_digits(part) if i % 2 == 1 else to_persian_digits(part))
    return ''.join(out)


def delimiters_balanced(raw) -> bool:
    """True when $ / \\[ \\] / \\( \\) delimiters all pair up.

    Unbalanced delimiters mean the span-based digit policy above may have
    treated prose as math (or vice versa), so the row needs the AI's eyes.
    """
    s = str(raw or "")
    return (s.count("$") % 2 == 0
            and s.count(r"\[") == s.count(r"\]")
            and s.count(r"\(") == s.count(r"\)"))


def parse_options(raw):
    arr = raw
    if isinstance(arr, str):
        try:
            arr = json.loads(arr)
        except Exception:
            arr = []
    if not isinstance(arr, list):
        return []
    out = []
    for i, o in enumerate(arr):
        if isinstance(o, str):
            m = _OPTION_RE.match(o)
            if m:
                out.append({"label": to_english_digits(m.group(1).strip()),
                            "text": norm_text(m.group(2))})
            else:
                out.append({"label": str(i + 1), "text": norm_text(o)})
        elif isinstance(o, dict):
            out.append({"label": to_english_digits(str(o.get("label", i + 1))),
                        "text": norm_text(o.get("text", ""))})
        else:
            out.append({"label": str(i + 1), "text": norm_text(str(o))})
    return [o for o in out if o["text"]]


def _rep(field, issue, source, before, after):
    return {"field": field, "issue": issue, "severity": "info",
            "source": source, "before_value": before, "after_value": after}


def _polish_numeric_fields(row, repairs):
    # Numeric fields: Persian/Arabic digits -> real numbers
    for f in ("question_number", "grade"):
        if row.get(f) is not None:
            n = parse_number(to_english_digits(row[f]))
            if n is not None:
                row[f] = n

    # Year digits (years are kept Western so they stay comparable/sortable)
    if row.get("year"):
        y = to_english_digits(str(row["year"]))
        if y != row["year"]:
            repairs.append(_rep("year", "Persian digits converted to English",
                                "code", row["year"], y))
            row["year"] = y


def _polish_text_fields(row, repairs, fields):
    for f in fields:
        if row.get(f):
            healed = apply_digit_policy(sanitize_math(norm_text(str(row[f]))))
            if healed != row[f]:
                repairs.append(_rep(f, "text normalized + LaTeX sanitized + digit policy",
                                    "code", str(row[f])[:150], healed[:150]))
                row[f] = healed


def _polish_correct_option(row, repairs):
    # Correct-option normalization + label <-> text cross fill
    if row.get("correct_option_label"):
        row["correct_option_label"] = to_english_digits(str(row["correct_option_label"])).strip()

    opts = parse_options(row.get("options")) if "options" in row else []

    if row.get("correct_option_label") and not row.get("correct_option_text"):
        hit = next((o for o in opts if o["label"] == row["correct_option_label"]), None)
        if hit:
            row["correct_option_text"] = hit["text"]
            repairs.append(_rep("correct_option_text", "filled from options by label",
                                "code", None, hit["text"]))

    if not row.get("correct_option_label") and row.get("correct_option_text"):
        target = apply_digit_policy(norm_text(str(row["correct_option_text"])))
        hit = next((o for o in opts if o["text"] == target), None)
        if hit:
            row["correct_option_label"] = hit["label"]
            repairs.append(_rep("correct_option_label", "filled from options by text",
                                "code", None, hit["label"]))


def polish_row(q: dict, run_id: str) -> dict:
    """Polish one questions-table row (answers ride along via pairing)."""
    row = dict(q)
    repairs = []
    row["run_id"] = run_id

    _polish_numeric_fields(row, repairs)

    _polish_text_fields(row, repairs,
                        ("question_text", "answer_explanation", "correct_option_text"))

    # Options rebuild + math sanitization + digit policy
    opts = [{"label": o["label"], "text": apply_digit_policy(sanitize_math(o["text"]))}
            for o in parse_options(row.get("options"))]
    opts_str = json.dumps(opts, ensure_ascii=False)
    old_opts = row.get("options") if isinstance(row.get("options"), str) \
        else json.dumps(row.get("options") or [], ensure_ascii=False)
    if opts_str != old_opts:
        repairs.append(_rep("options", "options normalized/rebuilt + LaTeX sanitized",
                            "code", None, opts_str[:200]))
    row["options"] = opts_str

    _polish_correct_option(row, repairs)

    # Tags rebuilt from metadata
    tags = [v for v in (row.get("subject"), row.get("topic"),
                        row.get("difficulty"), row.get("corp")) if v]
    tags_str = json.dumps(tags, ensure_ascii=False)
    old_tags = row.get("tags") if isinstance(row.get("tags"), str) \
        else json.dumps(row.get("tags") or [], ensure_ascii=False)
    if tags_str != old_tags:
        repairs.append(_rep("tags", "tags rebuilt from metadata", "code", None, tags_str))
    row["tags"] = tags_str

    # Diagram completion
    bbox = row.get("diagram_bbox")
    if isinstance(bbox, str):
        try:
            bbox = json.loads(bbox)
        except Exception:
            bbox = None
    has_b = isinstance(bbox, list) and len(bbox) == 4
    row["has_diagram"] = has_b
    row["diagram_bbox"] = json.dumps(bbox) if has_b else None
    if has_b and not row.get("diagram_url") and row.get("source_storage_url"):
        row["diagram_url"] = row["source_storage_url"]
        repairs.append(_rep("diagram_url", "filled from source storage url",
                            "code", None, row["diagram_url"]))

    row["_repairs"] = repairs
    return row


def polish_answer_row(a: dict, run_id: str) -> dict:
    """Polish one answers-table row (no options/diagram on this table)."""
    row = dict(a)
    repairs = []
    row["run_id"] = run_id

    _polish_numeric_fields(row, repairs)
    _polish_text_fields(row, repairs, ("answer_explanation", "correct_option_text"))
    if row.get("correct_option_label"):
        row["correct_option_label"] = to_english_digits(str(row["correct_option_label"])).strip()

    # Tags rebuilt from metadata
    tags = [v for v in (row.get("subject"), row.get("topic"),
                        row.get("difficulty"), row.get("corp")) if v]
    old_tags = row.get("tags") if isinstance(row.get("tags"), str) \
        else json.dumps(row.get("tags") or [], ensure_ascii=False)
    if tags and old_tags != json.dumps(tags, ensure_ascii=False):
        row["tags"] = json.dumps(tags, ensure_ascii=False)
        repairs.append(_rep("tags", "tags rebuilt from metadata", "code", None, row["tags"]))

    row["_repairs"] = repairs
    return row
