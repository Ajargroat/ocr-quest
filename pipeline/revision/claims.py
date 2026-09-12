"""Definitive correct-option derivation from the answer explanation.

Persian answer keys review every WRONG option under a heading like
«بررسی سایر گزینه‌ها» ("review of the other options"). The only option never
reviewed there is therefore the correct one — no AI needed when the section
parses cleanly. Ambiguous or missing structure falls back to the AI audit.
"""
import re

from ..utils import norm_text, to_english_digits

# Heading variants tolerate the ZWNJ/spacing spellings seen in real OCR output.
_SECTION_RE = re.compile(r"بررسی\s*سایر\s*گزینه[\u200cه]?\s*ها?")
# «گزینه ۳», «گزینه (ب)», «گزینهٔ 4» ...
_MENTION_RE = re.compile(
    r"گزینه[\s\u200cٔ]*(?:[\(\[\{]\s*)?([\u06F0-\u06FF0-9]{1,3})(?:\s*[\)\]\}])?")
# Words that mark a mention as naming the CORRECT option rather than a wrong one.
_CORRECT_MARKERS = ("صحیح", "صحيحة", "درست", "پاسخ")
_FA_DIGITS = "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9"


def _normalize_label(raw: str):
    t = norm_text(raw)
    if not t:
        return None
    if all(c in _FA_DIGITS for c in t):
        return to_english_digits(t)
    return t


def _find_mentions(text: str, labels):
    """(label, is_correct_assertion) for every «گزینه X» naming a real option.

    A mention counts as 'correct' when a correctness word sits right before or
    after it («پاسخ گزینه ۳ صحیح است»); «نادرست» is stripped first so its
    «درست» substring never flips the verdict.
    """
    out = []
    for m in _MENTION_RE.finditer(text):
        label = _normalize_label(m.group(1))
        if label is None or label not in labels:
            continue
        before = text[max(0, m.start() - 30):m.start()].replace("نادرست", "")
        after = text[m.end():m.end() + 15].replace("نادرست", "")
        is_correct = any(w in before or w in after for w in _CORRECT_MARKERS)
        out.append((label, is_correct))
    return out


def analyze(answer, options):
    """Compare the explanation against the option list.

    `answer` is an answers-table row (uses 'answer_explanation' and
    'correct_option_label'); a plain explanation string is also accepted.
    `options` is the questions-table option list as [{label, text}, ...].

    Returns a dict:
      status   'ok'           derived == stored label
               'fill'         no stored label; derivation fills it definitively
               'mismatch'     derivation contradicts the stored label -> AI judges
               'inconclusive' structure missing/ambiguous -> AI verification
      derived  the option label nothing in the review section mentions (or None)
      current  normalized stored label (or None)
    """
    labels = []
    for o in options if isinstance(options, list) else []:
        if isinstance(o, dict) and o.get("label") is not None:
            labels.append(to_english_digits(str(o["label"])).strip())

    if isinstance(answer, dict):
        text = norm_text(str(answer.get("answer_explanation") or ""))
        current = to_english_digits(str(answer.get("correct_option_label") or "")).strip()
    else:
        text = norm_text(str(answer or ""))
        current = ""

    base = {"labels": labels, "current": current or None}
    if not text or len(labels) < 2:
        return {**base, "status": "inconclusive", "derived": None}

    section_m = _SECTION_RE.search(text)
    section = text[section_m.end():] if section_m else text
    mentions = _find_mentions(section, set(labels))
    if not mentions:
        return {**base, "status": "inconclusive", "derived": None}

    wrong = {lab for lab, is_correct in mentions if not is_correct}
    asserted = [lab for lab, is_correct in mentions if is_correct]
    remaining = [lab for lab in labels if lab not in wrong]

    if not section_m:
        # No review heading: only an explicit «پاسخ گزینه X صحیح است» style
        # assertion is strong enough to be definitive.
        if wrong or len(asserted) != 1:
            return {**base, "status": "inconclusive", "derived": None}
        derived = asserted[0]
    else:
        if len(remaining) != 1:
            return {**base, "status": "inconclusive", "derived": None}
        derived = remaining[0]
        if asserted and asserted[-1] != derived:
            return {**base, "status": "inconclusive", "derived": None}

    if not current:
        return {**base, "status": "fill", "derived": derived}
    if current == derived:
        return {**base, "status": "ok", "derived": derived}
    return {**base, "status": "mismatch", "derived": derived}
