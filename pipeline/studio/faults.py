"""Studio-specific faults — what went wrong, in studio words.

``pipeline/faults.py`` already names every network and credential failure
(tunnel down, quota, bad key, geo block, database down…).  The studio adds a
layer of its own: the file never opened, the stage ran out of order, the PDF
is encrypted, the page rendered empty, the import target does not exist.

Those faults are studio-specific, so they are named here — with the same
shape as the pipeline kinds (``kind`` / ``label`` / ``emoji`` / ``group`` /
``retryable`` / ``hint`` / ``raw``) so the dashboard can chip them with the
exact same CSS the pipeline log uses.
"""
from pipeline import faults as base

# group = the colour family the dashboard chip uses
# (net / bill / auth / srv / model / store / local / misc).
KINDS = {
    "no_source": {
        "label": "NO SOURCE", "emoji": "", "group": "local",
        "retryable": False,
        "hint": "This job has no source file on disk — upload the file "
                "again."},
    "stage_order": {
        "label": "WRONG STAGE", "emoji": "", "group": "local",
        "retryable": False,
        "hint": "The stage ran before its input existed — run the previous "
                "stage first (upload, probe, extract, enrich, crops, "
                "import)."},
    "pdf_broken": {
        "label": "BAD PDF", "emoji": "", "group": "local",
        "retryable": False,
        "hint": "The PDF cannot be opened — it is corrupt, truncated or "
                "password-protected. Export it again and upload a new file."},
    "no_render": {
        "label": "NO RENDER", "emoji": "", "group": "local",
        "retryable": True,
        "hint": "The page could not be rendered into an image — the crop and "
                "bbox stages need that page picture."},
    "bad_file": {
        "label": "BAD FILE", "emoji": "", "group": "local",
        "retryable": False,
        "hint": "The file type is not supported — upload a PDF or an image."},
    "bad_target": {
        "label": "BAD TARGET", "emoji": "", "group": "store",
        "retryable": False,
        "hint": "The chosen import target does not exist — pick a target "
                "from the Import panel."},
    "empty_page": {
        "label": "EMPTY PAGE", "emoji": "", "group": "model",
        "retryable": True,
        "hint": "The page produced no usable text — it may be blank, or the "
                "page window fell outside the printed area."},
    "import_empty": {
        "label": "NOTHING TO IMPORT", "emoji": "", "group": "store",
        "retryable": True,
        "hint": "No row was ready for import — finish the crop review so "
                "the blocks turn ready."},
}

# signatures of a studio fault, most specific first
_SOURCE = ("no source file", "has no source", "source_path", "no source")
_ORDER = ("run extract first", "run enrich first", "run geometry first",
          "no sorted bulk yet", "no extraction yet")
_PDF = ("pdfium", "password", "encrypted", "invalid pdf", "corrupt",
        "failed to open the pdf", "cannot open the pdf", "document is not")
_BADFILE = ("unsupported file type",)
_BADTARGET = ("unknown import target",)
_EMPTY = ("no text content", "produced no text", "empty text",
          "no text on page")


def fault(kind, raw=""):
    """A mutable copy of the kind metadata plus its kind id and raw text."""
    meta = dict(KINDS.get(kind, KINDS.get("stage_order", {})))
    meta["kind"] = kind if kind in KINDS else "unknown"
    meta["label"] = meta.get("label") or "UNKNOWN FAULT"
    meta["group"] = meta.get("group") or "misc"
    meta["emoji"] = meta.get("emoji") or ""
    meta["retryable"] = bool(meta.get("retryable", True))
    meta["hint"] = meta.get("hint") or "No known signature matched."
    meta["raw"] = str(raw or "")[:400]
    return meta


def payload(flt):
    """The JSON shape the dashboard chips — small and flat."""
    return {"kind": flt.get("kind", "unknown"),
            "label": flt.get("label", "UNKNOWN FAULT"),
            "emoji": flt.get("emoji", ""),
            "group": flt.get("group", "misc"),
            "retryable": bool(flt.get("retryable", True)),
            "hint": flt.get("hint", ""),
            "raw": flt.get("raw", "")}


def _studio_kind(blob):
    """The studio kind for a lowercase blob, or None to fall through."""
    if any(w in blob for w in _BADFILE):
        return "bad_file"
    if any(w in blob for w in _BADTARGET):
        return "bad_target"
    if "nothing to import yet" in blob:
        return "import_empty"
    if any(w in blob for w in _SOURCE):
        return "no_source"
    if any(w in blob for w in _ORDER):
        return "stage_order"
    if any(w in blob for w in _PDF):
        return "pdf_broken"
    if "no page render" in blob or "no page image" in blob:
        return "no_render"
    if any(w in blob for w in _EMPTY):
        return "empty_page"
    return None


def classify(exc):
    """Classify a real exception — studio kinds first, pipeline kinds after."""
    chain = base._chain(exc)
    blob = " ".join("%s %s" % (type(e).__name__, e) for e in chain).lower()
    kind = _studio_kind(blob)
    if kind:
        return fault(kind, raw=str(exc)[:400])
    return base.classify(exc)


def classify_text(text):
    """Same detection for a failure string the studio builds itself."""
    blob = str(text or "").lower()
    kind = _studio_kind(blob)
    if kind:
        return fault(kind, raw=str(text)[:400])
    return base.classify_text(text)
