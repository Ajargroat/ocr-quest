"""The controlled vocabulary (tag list) the studio compares OCR against.

A tag list is a versioned JSON document. The repo ships a **seed** derived
from the Sapienstech ``QuestionBankMeta`` canonical lists (subjects + the
institute spelling-merge table) so the studio works out of the box; a job or
the user can override it with their own list later, and approved new tags get
folded back in (see the review screen).

Shape:

    {
      "version": 1,
      "subjects": ["زیست‌شناسی", "شیمی", ...],
      "corps":    [{"tag": "قلم‌چی", "variants": ["قلمچی", "قلم چی"]}, ...],
      "chapters": [{"subject": "زیست‌شناسی", "grade": "دهم",
                    "chapter": 1, "topics": ["سطوح سازمانیابی حیات"]}, ...]
    }

Comparison is done **fuzzy-first in code** (fold Arabic→Persian forms, strip
ZWNJ and spaces) so only the residue is ever handed to the LLM — we never
spend tokens matching an obvious variant.
"""
import json
import os
import re

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tags", "seed.json")

# Arabic letter forms folded to Persian, alef variants unified — the same
# mapping QuestionBankMeta::corpKey uses, so the two agree on what is "equal".
_FOLD = {
    "ي": "ی", "ك": "ک", "ى": "ی",
    "آ": "ا", "أ": "ا", "إ": "ا", "ٱ": "ا",
    "ة": "ه", "ؤ": "و", "ء": "",
}
# Zero-width and bidi control characters, plus whitespace, dropped for the key.
_STRIP = re.compile(r"[\u200b-\u200f\u202a-\u202e\u200c\u200d\s]+")


def fold(value: str) -> str:
    """A spelling-insensitive comparison key (see QuestionBankMeta::corpKey)."""
    out = str(value or "")
    for src, dst in _FOLD.items():
        out = out.replace(src, dst)
    return _STRIP.sub("", out)


def load(path: str = None) -> dict:
    """Load a tag list, defaulting to the shipped seed. Never raises."""
    path = path or SEED_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("version", 1)
    data.setdefault("subjects", [])
    data.setdefault("corps", [])
    data.setdefault("chapters", [])
    return data


def save(data: dict, path: str) -> str:
    """Write a tag list atomically. Returns the path written."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return path

# Hard caps so a bad upload cannot wedge the enrich prompt or the UI.
_MAX_SUBJECTS = 200
_MAX_CORPS = 200
_MAX_VARIANTS = 50
_MAX_CHAPTERS = 500
_MAX_TOPICS = 200

def validate(data: dict) -> list:
    """Shape-check a tag list. Returns error strings ([] means valid)."""
    errors = []
    if not isinstance(data, dict):
        return ["tag list must be a JSON object"]
    subjects = data.get("subjects", [])
    if not isinstance(subjects, list):
        errors.append("subjects must be a list")
    elif len(subjects) > _MAX_SUBJECTS:
        errors.append("subjects has %d entries (max %d)"
                       % (len(subjects), _MAX_SUBJECTS))
    elif any(not isinstance(s, str) or not s.strip() for s in subjects):
        errors.append("every subject must be a non-empty string")
    corps = data.get("corps", [])
    if not isinstance(corps, list):
        errors.append("corps must be a list")
    elif len(corps) > _MAX_CORPS:
        errors.append("corps has %d entries (max %d)"
                       % (len(corps), _MAX_CORPS))
    else:
        for i, entry in enumerate(corps):
            if not isinstance(entry, dict) or not str(entry.get("tag") or "").strip():
                errors.append("corps[%d] needs a non-empty tag" % i)
                continue
            variants = entry.get("variants", [])
            if not isinstance(variants, list):
                errors.append("corps[%d] variants must be a list" % i)
            elif len(variants) > _MAX_VARIANTS:
                errors.append("corps[%d] has too many variants (max %d)" % (i, _MAX_VARIANTS))
            elif any(not isinstance(v, str) or not v.strip() for v in variants):
                errors.append("corps[%d] variants must be non-empty strings" % i)
    chapters = data.get("chapters", [])
    if not isinstance(chapters, list):
        errors.append("chapters must be a list")
    elif len(chapters) > _MAX_CHAPTERS:
        errors.append("chapters has %d entries (max %d)"
                       % (len(chapters), _MAX_CHAPTERS))
    else:
        for i, ch in enumerate(chapters):
            if not isinstance(ch, dict):
                errors.append("chapters[%d] must be an object" % i)
                continue
            topics = ch.get("topics", [])
            if not isinstance(topics, list):
                errors.append("chapters[%d] topics must be a list" % i)
            elif len(topics) > _MAX_TOPICS:
                errors.append("chapters[%d] has too many topics (max %d)" % (i, _MAX_TOPICS))
            elif any(not isinstance(t, str) or not t.strip() for t in topics):
                errors.append("chapters[%d] topics must be non-empty strings" % i)
    return errors

def normalize(data: dict) -> dict:
    """Strip, drop empties and dedupe (order-preserving) a valid tag list."""
    def _strs(items):
        seen, out = set(), []
        for item in items or []:
            text = str(item).strip()
            if text and text not in seen:
                seen.add(text)
                out.append(text)
        return out
    corps = []
    for entry in (data.get("corps") or []):
        if not isinstance(entry, dict):
            continue
        tag = str(entry.get("tag") or "").strip()
        if not tag:
            continue
        corps.append({"tag": tag, "variants": _strs(entry.get("variants"))})
    chapters = []
    for ch in (data.get("chapters") or []):
        if not isinstance(ch, dict):
            continue
        chapters.append({
            "subject": str(ch.get("subject") or "").strip(),
            "grade": str(ch.get("grade") or "").strip(),
            "chapter": ch.get("chapter"),
            "topics": _strs(ch.get("topics")),
        })
    return {
        "version": data.get("version") or 1,
        "subjects": _strs(data.get("subjects")),
        "corps": corps,
        "chapters": chapters,
    }


def _corp_index(tags: dict) -> dict:
    """fold(variant|tag) -> canonical tag, for every corp in the list."""
    index = {}
    for entry in tags.get("corps", []):
        if not isinstance(entry, dict):
            continue
        tag = str(entry.get("tag") or "").strip()
        if not tag:
            continue
        index[fold(tag)] = tag
        for variant in entry.get("variants", []) or []:
            if str(variant).strip():
                index[fold(variant)] = tag
    return index


def _subject_index(tags: dict) -> dict:
    """fold(subject) -> canonical subject."""
    return {fold(s): s for s in tags.get("subjects", []) if str(s).strip()}


def match_corp(raw, tags: dict):
    """Canonical institute name for a raw OCR string, or None.

    A fold-exact hit wins immediately; otherwise None (the caller may ask the
    LLM to decide, then the human approves a new tag).
    """
    if raw is None or not str(raw).strip():
        return None
    return _corp_index(tags).get(fold(raw))


def match_subject(raw, tags: dict):
    if raw is None or not str(raw).strip():
        return None
    return _subject_index(tags).get(fold(raw))


def topics_for(subject: str, grade=None, chapter=None, tags: dict = None) -> list:
    """Every known topic, optionally narrowed to one subject/grade/chapter."""
    tags = tags or load()
    out = []
    for ch in tags.get("chapters", []):
        if not isinstance(ch, dict):
            continue
        if subject and fold(ch.get("subject")) != fold(subject):
            continue
        if grade is not None and str(ch.get("grade") or "") != str(grade):
            continue
        if chapter is not None and str(ch.get("chapter") or "") != str(chapter):
            continue
        out.extend(t for t in (ch.get("topics") or []) if str(t).strip())
    return out


def known_topics(tags: dict = None) -> list:
    """Every topic across the whole list (for a picker / LLM prompt)."""
    return topics_for(None, None, None, tags or load())


def vocab_for_prompt(tags: dict = None) -> dict:
    """The compact slice an enrich prompt embeds: subjects, corp tags, topics.

    Kept small on purpose — the LLM only needs the canonical spellings, not
    the full variant tables (code already folded those away)."""
    tags = tags or load()
    return {
        "subjects": list(tags.get("subjects", [])),
        "corps": [e.get("tag") for e in tags.get("corps", [])
                  if isinstance(e, dict) and e.get("tag")],
        "topics": known_topics(tags),
    }
