"""Stage 2 (pairing half): definitively link answers to questions and backfill.

Identity: a question and an answer point at the same item when
(question_number, subject, topic, grade) all agree. corp / year / difficulty
then CONFIRM the link; a disagreement on those is reported as a conflict and
never auto-resolved. All fills here derive from data already in the database —
no AI, no invention.
"""
import json

from ..utils import norm_text, parse_number, to_english_digits
from .completeness import is_missing

IDENTITY_FIELDS = ("question_number", "subject", "topic", "grade")
CHECK_FIELDS = ("corp", "year", "difficulty")
NULL_FILL_FIELDS = IDENTITY_FIELDS + CHECK_FIELDS


def norm_scalar(v):
    """Normalized comparable form: numbers collapse 12/12.0/'۱۲' -> '12'."""
    if v is None:
        return None
    n = parse_number(to_english_digits(str(v)))
    if n is not None:
        f = float(n)
        return str(int(f)) if f.is_integer() else str(f)
    t = norm_text(to_english_digits(str(v))).lower()
    return t or None


_norm_scalar = norm_scalar


def identity_key(row):
    k = tuple(_norm_scalar(row.get(f)) for f in IDENTITY_FIELDS)
    return None if any(p is None for p in k) else k


def check_conflicts(q, a, fields=CHECK_FIELDS):
    """Fields both sides define non-NULL but with different values."""
    out = []
    for f in fields:
        qv, av = norm_scalar(q.get(f)), norm_scalar(a.get(f))
        if qv is not None and av is not None and qv != av:
            out.append({"field": f, "question_value": q.get(f),
                        "answer_value": a.get(f)})
    return out


def _rep(field, issue, before, after):
    return {"field": field, "issue": issue, "severity": "info",
            "source": "code", "before_value": before, "after_value": after}


def parse_options_field(value):
    try:
        opts = json.loads(value) if isinstance(value, str) else (value or [])
    except Exception:
        opts = []
    if not isinstance(opts, list):
        opts = []
    return [{"label": to_english_digits(str(o.get("label", ""))).strip(),
             "text": norm_text(str(o.get("text", "")))}
            for o in opts if isinstance(o, dict)]


def backfill_pair(q, a):
    """Definitive cross-table fills for a linked pair.

    Mutates the q/a dicts in place. Returns (conflicts, reports) where each
    report is a tuple (entity, entity_id, repair_dict). Conflicts never block
    the link itself but force both rows into the human queue.
    """
    reports = []

    if a.get("question_id") and q.get("id"):
        conflicts = check_conflicts(q, a, IDENTITY_FIELDS + CHECK_FIELDS)
    else:
        conflicts = check_conflicts(q, a, CHECK_FIELDS)

    if a.get("question_id") != q.get("id"):
        a["question_id"] = q.get("id")
        reports.append(("answer", a.get("id"),
                        _rep("question_id", "linked to matched question",
                             None, str(q.get("id")))))

    # Metadata: a NULL column on one side that the pair defines is a definite fill.
    for src, dst in ((q, a), (a, q)):
        src_name = "question" if src is q else "answer"
        for f in NULL_FILL_FIELDS:
            if is_missing(dst.get(f)) and not is_missing(src.get(f)):
                dst[f] = src[f]
                reports.append((("answer" if dst is a else "question"), dst.get("id"),
                                _rep(f, f"filled from paired {src_name}",
                                     None, str(src[f])[:150])))
        for f in ("correct_option_label", "correct_option_text"):
            if is_missing(dst.get(f)) and not is_missing(src.get(f)):
                dst[f] = src[f]
                reports.append((("answer" if dst is a else "question"), dst.get("id"),
                                _rep(f, f"filled from paired {src_name}",
                                     None, str(src[f])[:150])))

    # label <-> text via the question's option list (answer rows have no options
    # of their own), mirrored onto the question row as well.
    opts = parse_options_field(q.get("options"))
    for target in (q, a):
        label = to_english_digits(str(target.get("correct_option_label") or "")).strip()
        text = norm_text(str(target.get("correct_option_text") or ""))
        entity = "question" if target is q else "answer"
        if label and not text:
            hit = next((o for o in opts if o["label"] == label), None)
            if hit:
                target["correct_option_text"] = hit["text"]
                reports.append((entity, target.get("id"),
                                _rep("correct_option_text",
                                     "filled from options by label", None, hit["text"])))
        elif text and not label:
            hit = next((o for o in opts if o["text"] == text), None)
            if hit:
                target["correct_option_label"] = hit["label"]
                reports.append((entity, target.get("id"),
                                _rep("correct_option_label",
                                     "filled from options by text", None, hit["label"])))

    return conflicts, reports


def pair_batch(questions, answers):
    """Match answers to questions within a batch.

    Returns a dict:
      pairs     [{q, a, conflicts}]  (linked directly or by identity)
      unmatched [{row, entity, reason}]  'qa_unmatched_question' / 'qa_unmatched_answer'
                                         / 'qa_ambiguous_pair'
    """
    by_id_q = {q.get("id"): q for q in questions}
    used = set()
    pairs = []
    unmatched_q = []
    ambiguous = set()

    # Answers already linked by question_id take precedence.
    linked_by_qid = {}
    for a in answers:
        qid = a.get("question_id")
        if qid and qid in by_id_q:
            linked_by_qid.setdefault(qid, []).append(a)

    by_key = {}
    for a in answers:
        if a.get("question_id"):
            continue
        k = identity_key(a)
        if k:
            by_key.setdefault(k, []).append(a)

    for q in questions:
        qid = q.get("id")
        cands = [a for a in linked_by_qid.get(qid, []) if a["id"] not in used]
        if not cands:
            k = identity_key(q)
            cands = [a for a in (by_key.get(k, []) if k else [])
                     if a["id"] not in used]
        if len(cands) > 1:
            ambiguous.add(qid)
            ambiguous.update(a["id"] for a in cands)
            unmatched_q.append({"row": q, "entity": "question",
                                "reason": "qa_ambiguous_pair"})
            continue
        if len(cands) == 1:
            a = cands[0]
            used.add(a["id"])
            conflicts, reports = backfill_pair(q, a)
            pairs.append({"q": q, "a": a, "conflicts": conflicts, "reports": reports})
        else:
            unmatched_q.append({"row": q, "entity": "question",
                                "reason": "qa_unmatched_answer"})

    unmatched = []
    for a in answers:
        if a["id"] in used:
            continue
        reason = "qa_ambiguous_pair" if a["id"] in ambiguous else "qa_unmatched_question"
        unmatched.append({"row": a, "entity": "answer", "reason": reason})

    return {"pairs": pairs, "unmatched": unmatched_q + unmatched}
