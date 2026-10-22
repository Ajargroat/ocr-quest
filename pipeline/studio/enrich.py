"""Stage 2 — sorting: layer1 → layer2 (one importable row per block).

Half of the work is deterministic — profile pins, the printed metadata line,
tag folding, the answer-key link — and half needs a model: repairing obvious
OCR misreadings and choosing a topic from the controlled vocabulary. The model
is never allowed to invent; a field it cannot see stays empty and the row is
flagged, so import can skip it and a human can look.

Every block leaves with ``status`` (ready | warn | error), ``issues`` (what a
human must look at), ``missing`` (demanded fields still empty) and
``proposed`` (values that are not in the tag list yet). Stage 4 imports
ready/warn rows and leaves the rest queued.
"""
import json
import re
import time

from pipeline import gemini as gemini_mod
from pipeline import prompts
from pipeline.revision.pairing import norm_scalar
from pipeline.utils import to_english_digits

from pipeline.studio import (faults as studio_faults, layout, llm,
                             profile as profile_mod, tags, workspace)

# Blocks per model call. Small enough that one bad reply loses little work.
CHUNK = 10
# Fields a row can carry (mirrors profile.FIELDS; kept local for filtering).
FIELDS = ("subject", "corp", "year", "grade", "topic", "difficulty")

PROMPT_HEAD = """You sort OCR blocks from Iranian exam files into importable rows.

Return ONE JSON object: {"blocks": [...]} — one entry per input block, same
"key" values, same order, nothing else.

Per block output:
  "key"        same as input
  "kind"       "question" | "prose" | "answer_key_entry"
  "text"       the stem, verbatim; repair only clear OCR misreadings, never
               rephrase, never translate; "" when the block has no stem
  "options"    option texts in printed order — keep the landed count; an
               unreadable option stays ""
  "corp"       the institute exactly as printed, or null
  "year"       the exam year in digits (e.g. 1402 or 1402-1403), or null
  "difficulty" one of "difficulties" below, or null
  "subject"    one exact value from "subjects" below, or null
  "topic"      one exact value from "topics" below, or null
  "missing"    the "requested" fields this block does not print

Rules:
- Persian stays Persian; keep ZWNJ half-spaces, punctuation and LaTeX as printed.
- Never guess a value that is not visible in the block, and never copy one
  from a neighbouring question.
- Never move stem or option content from one numbered question into another:
  if a fragment clearly belongs to a different number, leave it out and set
  that field missing.
- An option belongs to the SAME numbered question as its stem. When the landed
  options cannot be proven to belong to this question, output "" for every
  option of this block instead of borrowing them from a neighbour.
- Repair LaTeX/units only when the misreading is unambiguous (a split numeric
  "138/6", a broken exponent). Never paraphrase, never translate.
- Output no commentary, no markdown fences, nothing outside the JSON.
"""


def _req(profile):
    """The fields the profile demands and does not pin (= must be extracted)."""
    return list(profile_mod.required_missing(profile or {}))


def _tag_list(profile):
    ref = (profile or {}).get("tag_list_ref")
    return tags.load(ref) if ref else tags.load()


def _payload(block):
    """The compact slice of one block the model sees."""
    return {"key": block.get("key"), "kind": block.get("kind"),
            "number": block.get("number"), "text": block.get("text") or "",
            "options": block.get("options") or [],
            "metadata_line": block.get("metadata_line"),
            "corp": block.get("corp"), "year": block.get("year"),
            "difficulty": block.get("difficulty")}


def _prompt(req, vocab, payload):
    return (prompts.get("studio.enrich", PROMPT_HEAD)
            + "\ndifficulties: " + json.dumps(list(layout._DIFFICULTY),
                                              ensure_ascii=False)
            + "\nrequested: " + json.dumps(req)
            + "\nsubjects: " + json.dumps(vocab.get("subjects") or [],
                                          ensure_ascii=False)
            + "\ntopics: " + json.dumps(vocab.get("topics") or [],
                                        ensure_ascii=False)
            + "\ncorps: " + json.dumps(vocab.get("corps") or [],
                                       ensure_ascii=False)
            + "\nblocks: " + json.dumps(payload, ensure_ascii=False))


# ── deterministic prefill ───────────────────────────────────────────────────

def _prefill(block, profile, tag_list):
    """Code-side facts: profile pins, the printed metadata line, tag folding."""
    static = profile.get("static") or {}
    for field in ("subject", "grade", "corp", "year", "difficulty"):
        if static.get(field):
            block[field] = static[field]
    block["year"] = _year(block.get("year"))
    block["difficulty"] = _difficulty(block.get("difficulty"))
    if not static.get("corp") and block.get("corp"):
        block["corp"] = str(block["corp"]).strip() or None
    block.setdefault("tags", [])
    block.setdefault("proposed", [])
    block.setdefault("issues", [])
    block.setdefault("fixes", [])
    block.setdefault("missing", [])
    block.setdefault("answer", None)
    block["proposed"] = [p for p in block["proposed"]
                         if isinstance(p, dict) and p.get("field")]


def _year(value):
    """Digits, English, dashed ranges kept: "۱۴۰۲ - ۱۴۰۳" -> "1402-1403"."""
    if value is None:
        return None
    text = to_english_digits(str(value)).strip()
    parts = [p.strip() for p in text.replace("–", "-").split("-") if p.strip()]
    parts = [p for p in parts if p.isdigit()]
    if not parts:
        return text or None
    return "-".join(parts)


def _difficulty(value):
    if value is None:
        return None
    raw = str(value).strip()
    for word in layout._DIFFICULTY:
        if word in raw:
            return word
    return None


def _propose(block, field, value):
    """Remember a value the tag list does not know yet (human approves later)."""
    value = str(value).strip()
    if not value:
        return
    for entry in block["proposed"]:
        if entry.get("field") == field and entry.get("value") == value:
            return
    block["proposed"].append({"field": field, "value": value})


# ── the model pass ──────────────────────────────────────────────────────────

def _call_chunk(cfg, payload, req, vocab):
    result = llm.call_stage(
        cfg, "enrich",
        llm.stage_prompt(cfg, "enrich", _prompt(req, vocab, payload)),
        raw=False)
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        parsed = gemini_mod.extract_json(result.get("text") or "")
    if not isinstance(parsed, dict):
        return {}
    return {entry.get("key"): entry for entry in parsed.get("blocks") or []
            if isinstance(entry, dict) and entry.get("key")}


def _keep_text(old, new):
    """The model may repair text but never blank what the OCR did land."""
    if not isinstance(new, str) or not new.strip():
        return old or "", False
    return new, new.strip() != (old or "").strip()


_OPTION_LABEL_RE = re.compile(r"^\s*\d{0,2}\s*[\)\.]\s*")
_LETTER_LABEL_RE = re.compile(r"^[\u0621-\u064a]{1,3}\s*[\)\.]")


def _fixes_options(block, patch):
    """Option texts: the model may repair one, never drop a landed one."""
    old = block.get("options") or []
    new = patch.get("options")
    if not isinstance(new, list) or not any(str(o).strip() for o in new):
        return
    if len(new) > 4:
        # An exam question has at most four options. A longer list means the
        # patch swallowed a neighbour's options; keep the landed set.
        if "options beyond 4" not in block["issues"]:
            block["issues"].append("options beyond 4")
        return
    cleaned = [_keep_text(old[i] if i < len(old) else None,
                          new[i] if i < len(new) else None)[0]
               for i in range(max(len(old), len(new)))]
    if [str(o).strip() for o in cleaned] != [str(o).strip() for o in old]:
        block["fixes"].append("options repaired")
    cleaned = [layout.one_line(o) for o in cleaned]
    # Options carry their printed label canonically. The model's reply
    # style (bare "6" or "1) 6") may differ run to run, so a numeric set
    # is rebuilt from the landed order; a letter-labelled set (الف/ب/ج/د)
    # carries its own order and answer linkage, so it is kept verbatim.
    if any(_LETTER_LABEL_RE.match(str(o)) for o in cleaned):
        block["options"] = [str(o).strip() for o in cleaned
                           if str(o).strip()]
        return
    bare = [_OPTION_LABEL_RE.sub("", str(o).strip())
            for o in cleaned]
    block["options"] = ["%d) %s" % (index, text.strip())
                       for index, text in enumerate(bare, start=1)
                       if text.strip()]


def _apply(block, patch, profile, tag_list, vocab):
    """Fold one model reply entry into a block, tag-canonicalised."""
    static = profile.get("static") or {}

    text, changed = _keep_text(block.get("text"), patch.get("text"))
    if changed:
        block["fixes"].append("stem repaired")
    block["text"] = text

    # A stem the print wrapped mid-sentence leaves enrich as one flow.
    flowed = "\n".join(layout.reflow_lines(
        (block["text"] or "").split("\n")))
    if flowed != block["text"]:
        block["fixes"].append("stem reflowed")
        block["text"] = flowed
    options = [layout.one_line(o) for o in block.get("options") or []]
    if options != [str(o) for o in block.get("options") or []]:
        block["fixes"].append("options reflowed")
        block["options"] = options

    _fixes_options(block, patch)

    if patch.get("kind") in ("question", "prose", "answer_key_entry") \
            and patch["kind"] != block.get("kind"):
        block["fixes"].append("kind -> %s" % patch["kind"])
        block["kind"] = patch["kind"]

    if not static.get("corp") and patch.get("corp"):
        printed = str(patch["corp"]).strip()
        if printed:
            block["corp"] = printed
            if not tags.match_corp(printed, tag_list):
                _propose(block, "corp", printed)

    if not static.get("year"):
        block["year"] = _year(patch.get("year")) or block.get("year")

    if not static.get("difficulty"):
        found = _difficulty(patch.get("difficulty"))
        if found:
            block["difficulty"] = found
        elif not block.get("difficulty") and patch.get("difficulty"):
            _propose(block, "difficulty", patch["difficulty"])

    if not static.get("subject") and patch.get("subject"):
        if tags.match_subject(patch["subject"], tag_list):
            block["subject"] = str(patch["subject"]).strip()
        else:
            _propose(block, "subject", patch["subject"])

    if not static.get("topic") and patch.get("topic"):
        wanted = str(patch["topic"]).strip()
        if wanted in set(tags.known_topics(tag_list)):
            block["topic"] = wanted
        else:
            _propose(block, "topic", wanted)

    # ``missing`` is recomputed from the landed values in ``_health`` after
    # the whole patch is applied. The model's own claims are NOT trusted: a
    # value it just filled used to stay listed as missing forever.


# ── the answer key ──────────────────────────────────────────────────────────

def _label(value):
    if value is None:
        return None
    text = to_english_digits(str(value)).strip().strip("()[]").strip()
    return text or None


def _link_answers(blocks):
    """Question number → option label, from the key blocks. Never guesses."""
    entries, dupes = {}, set()
    for block in blocks:
        if block.get("kind") != "answer_key_entry":
            continue
        key = norm_scalar(block.get("number"))
        if not key or not _label(block.get("label")):
            continue
        if key in entries:
            dupes.add(key)
        else:
            entries[key] = _label(block["label"])

    linked = 0
    question_keys = set()
    for block in blocks:
        if block.get("kind") == "question" and block.get("number") is not None:
            question_keys.add(norm_scalar(block["number"]))
    for block in blocks:
        if block.get("kind") != "question":
            continue
        key = norm_scalar(block.get("number"))
        if key and key in entries:
            block["answer"] = entries[key]
            block["answer_source"] = "key"
            linked += 1
    for block in blocks:
        if block.get("kind") != "answer_key_entry":
            continue
        key = norm_scalar(block.get("number"))
        if key in dupes:
            block["issues"].append("duplicate number in the answer key")
        elif key and key not in question_keys:
            block["issues"].append("no question with this number")
    return linked


# ── health ──────────────────────────────────────────────────────────────────

def _field_value(block, field):
    return block.get(field) if field in FIELDS else None


# A printed metadata line is the only place corp/year can come from. When the
# whole job lacks one, no row can supply those fields, so their absence is a
# job-level note (merge.merged carries it) and not one warning per row.
SOFT_METADATA = ("corp", "year")


def _job_soft(blocks):
    """Metadata fields absent from EVERY question - unknowable for this job."""
    absent = set(SOFT_METADATA)
    for block in blocks or []:
        if block.get("kind") != "question":
            continue
        for field in SOFT_METADATA:
            if _field_value(block, field):
                absent.discard(field)
    return absent


def _health(block, req, profile, soft=()):
    """(status, issues) - the pre-import check every row goes through.

    ``error`` is reserved for a row that is structurally broken - an empty
    stem, no options.  A gap in a demanded field is a ``warn``: the review
    queue must see it, but the row still imports.  A field in ``soft`` is one
    the whole job cannot supply (no printed metadata line) - its absence is
    shown in ``missing`` yet does not downgrade the row to ``warn``.
    """
    issues = list(block["issues"])
    kind = block.get("kind")
    if kind == "question":
        # Computed exactly once, from the values the row actually lands: the
        # model's own "missing" claims and any stale stored list never decide
        # this. A field a patch just filled must stop being listed.
        block["missing"] = [f for f in req if not _field_value(block, f)]
    else:
        block["missing"] = []
    hard = [f for f in block["missing"] if f not in soft]

    if kind == "question":
        if not (block.get("text") or "").strip():
            issues.append("empty stem")
        options = [str(o).strip() for o in block.get("options") or []]
        if not options:
            issues.append("no options")
        elif len(options) < 2:
            issues.append("single option")
    elif kind == "prose":
        issues.append("not a question")
    elif kind == "answer_key_entry":
        if not _label(block.get("label")):
            issues.append("no answer label")

    if layout.mid_sentence_breaks(block.get("text") or ""):
        issues.append("coherence")

    for entry in block["proposed"]:
        issues.append("%s not in the tag list" % entry.get("field"))

    if not block.get("number") and kind in ("question", "answer_key_entry"):
        issues.append("no question number")

    if kind == "question" and "empty stem" in issues:
        status = "error"
    elif issues or hard:
        # A metadata gap the job cannot supply is a note, not a wall; any
        # other gap still keeps the row in the review queue.
        status = "warn"
    else:
        status = "ready"
    return status, issues


# ── the stage entry point ───────────────────────────────────────────────────

def run(job_id, cfg, emit=None):
    """Enrich a job's layer1 into layer2.json. Returns the job summary."""
    emit = emit or (lambda *args, **kwargs: None)
    layer1 = workspace.read_artifact(job_id, "layer1")
    if not layer1:
        raise ValueError("job has no extraction yet — run extract first")
    profile = workspace.read_artifact(job_id, "profile") \
        or layer1.get("profile") or profile_mod.blank()
    tag_list = _tag_list(profile)
    vocab = tags.vocab_for_prompt(tag_list)
    req = _req(profile)

    blocks = [dict(b) for b in layer1.get("blocks") or []]
    for block in blocks:
        _prefill(block, profile, tag_list)

    stats = {"blocks": len(blocks), "questions": 0, "prose": 0,
             "key_entries": 0, "ready": 0, "warn": 0, "error": 0,
             "answers_linked": 0, "calls": 0, "errors": []}

    todo = [b for b in blocks if b.get("kind") in ("question", "prose")]
    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        emit("enrich", "info", "sorting blocks %d-%d of %d"
             % (start + 1, start + len(chunk), len(todo)))
        stats["calls"] += 1
        try:
            patches = _call_chunk(cfg, [_payload(b) for b in chunk], req,
                                  vocab)
        except Exception as exc:
            patches = {}
            stats["errors"].append({"blocks": [b["key"] for b in chunk],
                                    "error": str(exc)})
            emit("enrich", "error", "chunk at block %d: %s" % (start + 1, exc),
                 fault=studio_faults.payload(studio_faults.classify(exc)))
        for block in chunk:
            patch = patches.get(block["key"])
            if patch is None:
                block["issues"].append("sorting call returned nothing")
            else:
                _apply(block, patch, profile, tag_list, vocab)

    stats["answers_linked"] = _link_answers(blocks)
    soft = _job_soft(blocks)
    stats["metadata_missing"] = sorted(soft)

    for block in blocks:
        block["tags"] = [v for v in (block.get("subject"), block.get("topic"))
                         if v]
        status, issues = _health(block, req, profile, soft)
        block["status"] = status
        block["issues"] = issues
        block["needs_human"] = status != "ready"
        kind = block.get("kind")
        if kind == "question":
            stats["questions"] += 1
        elif kind == "answer_key_entry":
            stats["key_entries"] += 1
        else:
            stats["prose"] += 1
        stats[status] += 1

    layer2 = {
        "version": 1,
        "job_id": job_id,
        "source": layer1.get("source"),
        "kind": layer1.get("kind"),
        "profile": profile,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "pages": layer1.get("pages") or [],
        "assets": layer1.get("assets") or [],
        "blocks": blocks,
        "stats": stats,
    }
    workspace.write_artifact(job_id, "layer2", layer2)
    workspace.log_event(
        job_id, "enrich", "success",
        "sorted %d block(s) — ready %d, warn %d, error %d, answers %d"
        % (stats["blocks"], stats["ready"], stats["warn"], stats["error"],
           stats["answers_linked"]))
    return {"job": workspace.summary(job_id), "stats": stats}
