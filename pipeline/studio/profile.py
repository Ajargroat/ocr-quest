"""Stage 0 — the profile: which fields are STATIC and which are EXTRACTED.

The profile is the studio's answer to "most of the time the file isn't a
compilation of different institutes; I know things about it." Anything we know
about the file (its subject, the one institute, the year range) is **pinned**
here and never guessed by a model; anything left null becomes an extraction
target, and if the model cannot fill it the question is *stalled* rather than
imported wrong.

The document is small, versioned and human-editable — the UI shows it and lets
the user override every field before Stage 1 runs.
"""
import time

COMPILATIONS = (
    "single_institute_multi_year",   # one institute, many years
    "multi_institute_single_year",   # many institutes, one year
    "multi_institute_multi_year",    # the general case
    "single_subject_multi_chapter",  # one subject, many chapters
    "multi_subject",                 # several subjects in one file
    "unknown",
)

SOURCE_KINDS = ("text_pdf", "image_pdf", "mixed", "images", "unknown")

ANSWER_LAYOUTS = ("inline", "separate_section", "key_pages_at_end", "none", "unknown")

# Fields a question row can carry; the profile decides static vs extracted.
FIELDS = ("subject", "corp", "year", "grade", "topic", "difficulty")

PROFILE_VERSION = 1


def blank(source_kind: str = "unknown") -> dict:
    """A fresh, empty profile — everything unknown, nothing pinned."""
    return {
        "version": PROFILE_VERSION,
        "source_kind": source_kind if source_kind in SOURCE_KINDS else "unknown",
        "compilation": "unknown",
        "static": {f: None for f in FIELDS},
        "extract_per_question": ["corp", "year", "topic", "difficulty"],
        "answer_layout": "unknown",
        "tag_list_ref": None,          # None => the shipped seed tag list
        "notes": "",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def normalize(raw: dict) -> dict:
    """Coerce an arbitrary dict (from disk, an agent, or the UI) into a valid
    profile. Unknown enum values fall back to 'unknown'; unknown keys drop."""
    raw = raw if isinstance(raw, dict) else {}
    out = blank(raw.get("source_kind", "unknown"))
    if raw.get("compilation") in COMPILATIONS:
        out["compilation"] = raw["compilation"]
    if raw.get("answer_layout") in ANSWER_LAYOUTS:
        out["answer_layout"] = raw["answer_layout"]

    static = raw.get("static") if isinstance(raw.get("static"), dict) else {}
    for f in FIELDS:
        value = static.get(f)
        out["static"][f] = (str(value).strip() or None) if value is not None else None

    extract = raw.get("extract_per_question")
    if isinstance(extract, list):
        out["extract_per_question"] = [f for f in extract if f in FIELDS]
    else:
        # Default: whatever is not pinned must be extracted.
        out["extract_per_question"] = [f for f in FIELDS
                                       if not out["static"].get(f)]

    out["tag_list_ref"] = raw.get("tag_list_ref") or None
    out["notes"] = str(raw.get("notes") or "")
    out["updated_at"] = str(raw.get("updated_at") or out["updated_at"])
    return out


def apply_overrides(profile: dict, overrides: dict) -> dict:
    """Fold a partial edit (from the UI or an agent) over a profile.

    Only keys present in ``overrides`` change; ``static`` merges per-field so
    pinning one field never clears the others. Returns a normalized profile.
    """
    merged = dict(profile or {})
    for key in ("source_kind", "compilation", "answer_layout", "tag_list_ref",
                "notes"):
        if key in overrides and overrides[key] is not None:
            merged[key] = overrides[key]
    if isinstance(overrides.get("static"), dict):
        static = dict(merged.get("static") or {})
        for f in FIELDS:
            if f in overrides["static"]:
                value = overrides["static"][f]
                static[f] = (str(value).strip() or None) if value is not None else None
        merged["static"] = static
    if isinstance(overrides.get("extract_per_question"), list):
        merged["extract_per_question"] = overrides["extract_per_question"]
    return normalize(merged)


def required_missing(profile: dict) -> list:
    """Extraction fields the profile demands but does not pin.

    These are exactly the fields a question must fill in Stage 2 or be stalled
    on — so the UI can warn before a run starts.
    """
    return [f for f in (profile.get("extract_per_question") or [])
            if not (profile.get("static") or {}).get(f)]
