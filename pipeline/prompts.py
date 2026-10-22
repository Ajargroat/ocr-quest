"""Editable LLM prompts: one override store, defaults stay in the code.

Every prompt the app sends to a model has an id and a built-in default. The
Prompts view can rewrite the wording of any of them; an override lives in
`.prompt-overrides.json` beside `.gemini-usage.json` (local-only, never
committed, never a browser secret).

Call sites read through `get(id, DEFAULT)` at CALL time — an edit applies to
the very next request, no restart and no module reload. Dynamic payloads
(candidate banks, page lists, block JSON) are appended by the calling module,
so an override only ever replaces the instructions.
"""
import importlib
import json
import os
import threading
from datetime import datetime, timezone

# Beside .gemini-usage.json / .gemini-calls.json — same local-only spot.
STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".prompt-overrides.json")

# id, panel label, group, "module:constant" holding the built-in default.
# importlib keeps these lazy: pipeline.prompts is imported by the very modules
# that own the defaults, so a plain import here would be circular.
CATALOG = (
    ("pipeline.questions", "Questions · scan prompt", "Pipeline",
     "pipeline.questions:QUESTION_PROMPT"),
    ("pipeline.answers", "Answers · scan prompt", "Pipeline",
     "pipeline.answers:ANSWER_TEMPLATE"),
    ("pipeline.questions_pages", "Questions · pages-per-call prompt", "Pipeline",
     "pipeline.questions:PAGES_PROMPT"),
    ("pipeline.answers_pages", "Answers · pages-per-call prompt", "Pipeline",
     "pipeline.answers:ANSWER_PAGES_TEMPLATE"),
    ("revision.audit", "Revision · audit prompt", "Revision",
     "pipeline.revision.audit:PROMPT_HEADER"),
    ("studio.ocr", "Extract · page OCR prompt", "Studio",
     "pipeline.studio.extract:OCR_PAGE_PROMPT"),
    ("studio.organize", "Extract · page organize prompt", "Studio",
     "pipeline.studio.extract:ORGANIZE_PAGE_PROMPT"),
    ("studio.enrich", "Enrich · sorting prompt", "Studio",
     "pipeline.studio.enrich:PROMPT_HEAD"),
    ("studio.locate", "Locate · multi-page bands prompt", "Studio",
     "pipeline.studio.geometry:LOCATE_PROMPT"),
    ("studio.geometry", "Locate · single-page prompt", "Studio",
     "pipeline.studio.geometry:GEOMETRY_PROMPT"),
    ("studio.band", "Locate · band picture prompt", "Studio",
     "pipeline.studio.geometry:BAND_PROMPT"),
    ("assistant.system", "Assistant · system prompt", "Assistant",
     "pipeline.assistant:SYSTEM_PROMPT"),
)

_lock = threading.RLock()


def _path():
    """Derived at call time so a test can redirect STORE_PATH safely."""
    return STORE_PATH


def load() -> dict:
    """{prompt_id: {"text": str, "updated_utc": str}} — tolerant of junk."""
    try:
        with open(_path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    entries = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(entries, dict):
        return {}
    return {k: v for k, v in entries.items()
            if isinstance(v, dict) and isinstance(v.get("text"), str)}


def get(prompt_id, default=""):
    """The text to send: the saved override, or the built-in default."""
    entry = load().get(prompt_id) or {}
    text = entry.get("text")
    return text if isinstance(text, str) and text.strip() else default


def save(prompt_id, text):
    """Store one override. An empty/blank text clears it (back to default)."""
    with _lock:
        entries = load()
        text = "" if text is None else str(text)
        if text.strip():
            entries[prompt_id] = {
                "text": text,
                "updated_utc": datetime.now(timezone.utc).isoformat(),
            }
        else:
            entries.pop(prompt_id, None)
        _write(entries)
        return entries.get(prompt_id)


def clear(prompt_id):
    """Drop one override (the default is used again)."""
    with _lock:
        entries = load()
        entries.pop(prompt_id, None)
        _write(entries)


def clear_all():
    with _lock:
        _write({})


def _write(entries):
    path = _path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "prompts": entries}, fh,
                      ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except OSError:
        # A prompt override is never worth breaking a request over.
        pass


def default_of(prompt_id):
    """The built-in text for one id ('' when the catalog has no such id)."""
    for entry in catalog():
        if entry["id"] == prompt_id:
            return entry["default"]
    return ""


def catalog():
    """[{id, label, group, default}] — the built-in prompts, resolved lazily."""
    out = []
    for prompt_id, label, group, ref in CATALOG:
        mod_name, attr = ref.split(":", 1)
        try:
            mod = importlib.import_module(mod_name)
            default = getattr(mod, attr)
        except (ImportError, AttributeError):
            default = ""
        out.append({"id": prompt_id, "label": label, "group": group,
                    "default": default})
    return out


def view():
    """The Prompts panel payload: every prompt with its default + live text."""
    entries = load()
    rows = []
    for entry in catalog():
        override = entries.get(entry["id"]) or {}
        rows.append({
            "id": entry["id"],
            "label": entry["label"],
            "group": entry["group"],
            "default": entry["default"],
            "text": override.get("text") or entry["default"],
            "overridden": bool((override.get("text") or "").strip()),
            "updated_utc": override.get("updated_utc") or "",
        })
    return rows
