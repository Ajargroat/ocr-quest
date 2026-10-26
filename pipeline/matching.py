"""Answer ↔ question matching: the one place a link is decided (P5).

The live bank has 1,008 answers whose ``question_id`` is NULL, and the OCR now
stages answers before a human looks at them. A wrong link is worse than no
link, so the ladder is explicit and ordered by evidence strength:

``auto_ok``      exactly ONE candidate matches on the full key
                 (question number + canonical institute + canonical year).
                 Nothing else may also match that number.
``unlinked_ok``  one candidate matches on the number alone AND nothing
                 contradicts it (a different institute or year on both sides).
``manual_ok``    a human picked the link (only the Inspect UI sets this).
``needs_manual`` everything else: zero candidates, several plausible ones, or a
                 contradiction. Never "first match wins".

Every decision returns the candidate list it was made from, so the Inspect UI
can show a human *why* the ladder stopped where it did.
"""
from . import textnorm

METHODS = ("number+corp+year", "number", "manual")


def _number(value):
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _canon_corp(value, tags=None):
    if value is None or not str(value).strip():
        return ""
    if tags:
        from . import vocab
        matched = vocab.match_corp(value, tags)
        if matched:
            return vocab.fold(matched)
    return _fold_corp(value)


def _fold_corp(value):
    from . import vocab
    return vocab.fold(value)


def _canon_year(value):
    return textnorm.canonical_year(value)


def canon(record, tags=None):
    """The comparison triple for one question/answer shape."""
    return {"number": _number(record.get("question_number")),
            "corp": _canon_corp(record.get("corp"), tags),
            "year": _canon_year(record.get("year"))}


def scores(answer, question, tags=None):
    """(same_number, number_ok, corp_ok, year_ok) for one pair.

    A dimension the *question* does not carry cannot contradict: an empty
    institute on the question side is "unknown", not "different".
    """
    left, right = canon(answer, tags), canon(question, tags)
    if left["number"] is None or right["number"] is None:
        return None
    same = left["number"] == right["number"]
    corp_ok = (not right["corp"]) or (not left["corp"]) or left["corp"] == right["corp"]
    year_ok = (not right["year"]) or (not left["year"]) or left["year"] == right["year"]
    return same, corp_ok, year_ok


def decide(answer, candidates, tags=None):
    """The ladder. Returns ``{match_state, link_id, match_method, candidates}``.

    ``candidates`` may be {id, question_number, corp, year, ...} dicts.
    """
    pool = [c for c in (candidates or []) if isinstance(c, dict)
            and c.get("id") is not None]
    same_number = []
    for candidate in pool:
        scored = scores(answer, candidate, tags)
        if scored and scored[0]:
            same_number.append((candidate, scored[1], scored[2]))

    chosen, method, state = None, None, "needs_manual"
    full = [c for c, corp_ok, year_ok in same_number if corp_ok and year_ok]
    if len(same_number) == 1 and len(full) == 1:
        # One candidate on this number and nothing contradicts it. The method
        # says how strong the evidence actually was: a full institute+year key
        # is auto_ok; a number-only match is unlinked_ok (it still needs the
        # human's eye in the Inspect list).
        candidate, corp_ok, year_ok = same_number[0]
        left, right = canon(answer, tags), canon(candidate, tags)
        strong = bool(left["corp"] and right["corp"] and left["corp"] == right["corp"]
                      and left["year"] and right["year"] and left["year"] == right["year"])
        chosen = candidate
        method = "number+corp+year" if strong else "number"
        state = "auto_ok" if strong else "unlinked_ok"
    return {"match_state": state,
            "link_kind": "staged",
            "link_id": chosen.get("id") if chosen else None,
            "match_method": method,
            "candidates": summarize(pool)}


def summarize(pool, limit=25):
    """A compact, JSON-safe and order-stable view of the candidates."""
    out = []
    for row in pool[:limit]:
        out.append({"id": row.get("id"),
                    "question_number": row.get("question_number"),
                    "corp": row.get("corp"), "year": row.get("year"),
                    "origin": row.get("origin", ""),
                    "text": (row.get("question_text") or "")[:160]})
    return out


def link_candidates(answer, candidates, tags=None, subject=None, topic=None,
                    grade=None):
    """Narrow the pool by subject/topic/grade, then run the ladder."""
    pool = []
    for candidate in candidates or []:
        if not isinstance(candidate, dict):
            continue
        if subject and candidate.get("subject") and candidate["subject"] != subject:
            continue
        if topic and candidate.get("topic") and candidate["topic"] != topic:
            continue
        if grade and candidate.get("grade") is not None \
                and str(candidate["grade"]) != str(grade):
            continue
        pool.append(candidate)
    return decide(answer, pool, tags)


def iso_year(value):
    """A canonical year for display/compare ('' when unreadable)."""
    return _canon_year(value)
