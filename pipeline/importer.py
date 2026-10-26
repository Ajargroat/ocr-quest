"""The importer: ready staged rows -> the live bank (P6).

This is the ONLY module that writes Postgres rows and bucket objects. It runs
from a human's explicit Import press in Inspect, never from an OCR run.

Order per record, and why:

1. **Crop first.** The picture is uploaded (or its deterministic key reused)
   before the row exists. A row is never created pointing at a picture that
   did not land, so a crashed import leaves a ``ready`` row to retry, not a
   half-written live row.
2. **Then the row**, as ``approved`` — the review state the owner already uses
   for imported content.
3. **Then staging flips to ``imported``**, which freezes the record against a
   later OCR pass.

Resumability comes from that ordering plus the deterministic crop key: a
second run finds the crop already uploaded (same key, same bytes) and skips
straight to the row, which ``db.upsert_*`` treats idempotently.
"""
from . import crops

READY = ("ready",)


def _assets(record):
    """The crop files a record owns, in deterministic order."""
    key = record.get("crop_key")
    if not key:
        return []
    return [key]


def _public_url(cfg, key):
    return "/".join([(cfg.supabase_url or "").rstrip("/"), "storage", "v1", "object",
                     "public", cfg.supabase_bucket or "", key])


def upload_crops(store, cfg, record, upload=None):
    """Upload the record's crop(s); returns True when every one landed.

    ``upload`` is injected for tests (storage.upload_bytes in production). A
    missing local crop is not an error: the row simply carries no picture, and
    the human already chose to keep it.
    """
    for key in _assets(record):
        name = key.rsplit("/", 1)[-1]
        data = store.read_asset(record["source_id"], name)
        if data is None:
            continue
        if upload is None:
            from .storage import upload_bytes
            upload = upload_bytes
        try:
            upload(cfg, key, data)
        except Exception:
            return False
    return True


def run(store, live=None, cfg=None, limit=None, upload=None):
    """Import every ready record. Returns a JSON-safe batch report.

    ``live`` is a ``pipeline.db.Database``; None means "read the live database
    from config" (the production path). ``limit`` caps one run so a first
    attempt can be a small, eyeballed batch.
    """
    if live is None:
        from .config import load_config
        from .db import Database
        cfg = cfg or load_config()
        live = Database(cfg)
    cfg = cfg or load_config()

    questions = store.list_records("question", status="ready", limit=limit or 5000)
    answers = store.list_records("answer", status="ready", limit=limit or 5000)
    batch = store.create_batch("import", scope={"questions": len(questions),
                                                "answers": len(answers)},
                               rows=[("question", r["id"]) for r in questions] +
                                    [("answer", r["id"]) for r in answers],
                               total=len(questions) + len(answers))
    batch_id = batch["batch_id"]
    store.set_batch_status(batch_id, "importing")

    imported = failed = 0
    errors = []

    for kind, rows in (("question", questions), ("answer", answers)):
        for record in rows:
            try:
                if not upload_crops(store, cfg, record, upload=upload):
                    raise RuntimeError("crop upload failed for %s" % record.get("crop_key"))
                row = to_live(kind, record, cfg)
                if kind == "question":
                    live.upsert_questions([row])
                else:
                    live.upsert_answers([row])
                store.set_status(kind, record["id"], "imported")
                store.set_batch_row(batch_id, kind, record["id"], "imported")
                imported += 1
            except Exception as exc:              # noqa: BLE001 - reported, not raised
                message = " ".join(str(exc).split())[:300]
                errors.append({"kind": kind, "id": record["id"], "error": message})
                store.set_batch_row(batch_id, kind, record["id"], "failed", message)
                failed += 1

    # A batch that landed everything is "imported"; a batch that landed only
    # some is "failed" (the retry is the point), but the counts still say how
    # much survived - never report zero when rows did land.
    status = "imported" if not failed else "failed"
    store.set_batch_status(batch_id, status, imported=imported, failed=failed,
                           report={"errors": errors})
    return {"batch_id": batch_id, "status": status, "imported": imported,
            "failed": failed, "errors": errors}


def to_live(kind, record, cfg):
    """One staged record as the row shape ``db.upsert_*`` expects.

    The live schema keeps the text id in ``legacy_id`` (``id`` is the DB's own
    BIGSERIAL), the question text in ``prompt``, and the picture in
    ``media_images``; ``ready`` becomes ``approved``. Nothing is invented: a
    field the reviewer left blank stays blank, and a missing crop leaves
    ``media_images`` empty rather than pointing at a URL that does not exist.
    """
    import json

    def decode(value, default):
        if value in (None, ""):
            return default
        if isinstance(value, (list, dict)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default

    fields = decode(record.get("fields_json"), {})
    if not isinstance(fields, dict):
        fields = {}
    grade = fields.get("grade")
    if isinstance(grade, str) and grade.strip().isdigit():
        grade = int(grade.strip())
    elif not isinstance(grade, int):
        grade = None
    key = record.get("crop_key")
    bbox = decode(record.get("bbox_json"), None)
    picture = {"diagram_url": _public_url(cfg, key) if key else None,
               "diagram_bbox": bbox}
    tags = decode(record.get("tags_json"), [])

    row = {
        "id": record["id"],                     # the contract id -> legacy_id
        "source_id": record["source_id"],
        "question_number": record.get("question_number"),
        "subject": fields.get("subject"),
        "topic": fields.get("topic"),
        "grade": grade,
        "corp": fields.get("corp"),
        "year": fields.get("year"),
        "difficulty": fields.get("difficulty"),
        "tags": tags,
        "review_status": "approved",
        "raw_ocr_text": record.get("ocr_json") or "",
        "created_at": None,
    }
    if kind == "question":
        row.update({
            "type": None,                       # db.question_type derives it
            "question_text": record.get("question_text"),
            "options": decode(record.get("options_json"), []),
            "split": False, "duplicate": False, "unreadable": False,
            **picture,
        })
        return row
    row.update({
        "question_id": record.get("link_id"),
        "answer_explanation": record.get("answer_explanation"),
        "correct_option_label": fields.get("correct_option_label"),
        "correct_option_text": fields.get("correct_option_text"),
        "assets": decode(record.get("assets_json"), []),
        **picture,
    })
    return row
