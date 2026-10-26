"""Bridge between one OCR result and the local staging store (P3).

The runner used to upload the source to the bucket, upsert a ``sources`` row and
UPSERT ``questions``/``answers`` into Postgres while the run was live. That is
exactly what this module replaces: an OCR file now becomes

* a mirrored page image per PDF page (``staging/assets/<id>/p<N>.jpg``) so a
  bbox always has the page frame it was measured in;
* ``st_questions`` / ``st_answers`` rows with the contract ids
  (``<source_id>-q<N>`` / ``<source_id>-a<N>``) and the original OCR JSON kept
  beside the parsed fields;
* NOTHING else — no Postgres, no Supabase.

Answer candidates deliberately mix both banks: questions already staged for
this subject/topic/grade (including other files of the same unimported batch)
and questions that already live in the database, so a brand-new batch can link
an answer to a question the same run just read.

Empty extraction is a first-class outcome: the source (or record) is staged
with ``status='empty'`` and returned in ``StagedFile.empty_records`` so the
runner reports it and the Inspect UI shows it. It is never archived as if it
had succeeded silently.
"""
import base64
import io
import json
import os

from . import crops, pages, textnorm
from .questions import clean_options as questions_opts


class StagedFile:
    """What one file produced in staging: ids, counts and the empty records."""

    def __init__(self, source_id, rel_path, pages=0):
        self.source_id = source_id
        self.rel_path = rel_path
        self.pages = pages
        self.question_ids = []
        self.answer_ids = []
        self.empty_records = []       # [("question"|"answer"|"source", id)]
        self.crop_files = 0

    @property
    def count(self):
        return len(self.question_ids) + len(self.answer_ids)

    @property
    def is_empty(self):
        return not (self.question_ids or self.answer_ids)

    def report(self):
        return {"source_id": self.source_id, "rel_path": self.rel_path,
                "pages": self.pages, "questions": len(self.question_ids),
                "answers": len(self.answer_ids), "empty": self.empty_records,
                "crops": self.crop_files}


def _loads(value, default):
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def page_images(raw, mime_type, limit_pages=None):
    """Split one source file into per-page images: ``[(page_no, jpeg_bytes)]``.

    A PDF is rendered page by page; a single image is page 1 exactly as it
    arrived. Splitting BEFORE OCR keeps every bbox in the frame of the page it
    was measured on — a multi-page PDF read whole would leave every bbox
    pointing at the wrong page.
    """
    kind = (mime_type or "").lower()
    if kind == "application/pdf" or raw[:5] == b"%PDF-":
        import pypdfium2 as pdfium
        doc = None
        try:
            doc = pdfium.PdfDocument(io.BytesIO(raw))
            total = len(doc)
            if limit_pages:
                total = min(total, int(limit_pages))
            out = []
            for index in range(total):
                image = pages.render(doc[index])
                if image.mode != "RGB":
                    image = image.convert("RGB")
                buf = io.BytesIO()
                image.save(buf, "JPEG", quality=crops.DEFAULT_QUALITY,
                           optimize=True)
                out.append((index + 1, buf.getvalue()))
            return out
        except Exception as exc:
            raise ValueError("cannot render the PDF into pages: %s" % exc) from exc
        finally:
            if doc is not None:
                pages.close(doc)
    return [(1, raw)]


def stage_images(store, source_id, images):
    """Write each rendered page and remember its size for the bbox frame."""
    sizes, saved = [], []
    for page_no, data in images:
        image = crops.open_image(data)
        saved.append((page_no, data))
        store.save_page(source_id, page_no, data)
        sizes.append({"page": page_no, "w": image.width, "h": image.height})
    return sizes, saved


def candidate_questions(store, db, subject, topic, grade):
    """Answer candidates from the staged bank AND the live database.

    Staged rows come first (a fresh unimported batch must be able to link its
    own answers); live rows follow. Duplicates by number+corp+year are dropped
    so the answer prompt never receives the same question twice.
    """
    out, seen = [], set()

    def key(row):
        return (str(row.get("question_number")), str(row.get("corp") or ""),
                str(row.get("year") or ""))

    for row in store.list_records("question", status=None, limit=2000):
        if row.get("status") == "skipped":
            continue
        fields = _loads(row.get("fields_json"), {})
        if subject and fields.get("subject") not in (None, subject):
            continue
        if topic and fields.get("topic") not in (None, topic):
            continue
        if grade and fields.get("grade") is not None \
                and str(fields["grade"]) != str(grade):
            continue
        entry = {"id": row["id"], "question_number": row.get("question_number"),
                 "options": _loads(row.get("options_json"), []),
                 "subject": fields.get("subject"), "topic": fields.get("topic"),
                 "grade": fields.get("grade"), "corp": fields.get("corp"),
                 "year": fields.get("year"), "origin": "staged",
                 "question_text": row.get("question_text")}
        if key(entry) in seen:
            continue
        seen.add(key(entry))
        out.append(entry)

    if db is not None:
        try:
            live = db.fetch_context_questions(subject, topic, grade) or []
        except Exception:
            live = []                       # the live bank is a bonus, not a gate
        for row in live:
            entry = dict(row)
            entry.setdefault("origin", "db")
            if key(entry) in seen:
                continue
            seen.add(key(entry))
            out.append(entry)
    return out


def _number(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clean(text):
    """The same two text repairs the review UI relies on, applied once."""
    if not text:
        return text
    return textnorm.restore_zwnj(textnorm.canonical(text))


def _bbox_and_crop(store, source_id, raw_bbox, page_no, kind, number, index=0):
    """Persist the crop for one bbox; returns (bbox, crop_key, page)."""
    box = crops.normalize(raw_bbox)
    if box is None:
        return None, None, page_no
    page_path = store.page_path(source_id, page_no or 1)
    if not os.path.exists(page_path):
        return box, None, page_no
    with open(page_path, "rb") as fh:
        image = crops.open_image(fh.read())
    data = crops.render(image, box)
    if data is None:
        return box, None, page_no
    key = crops.crop_key(source_id, kind, number, index)
    store.save_crop(source_id, os.path.basename(key), data)
    return box, key, page_no


def stage_questions(store, item, parsed, page_no=1):
    """Write one OCR reply's questions into staging. Returns ``StagedFile``."""
    result = StagedFile(item.source_id, item.rel_path, pages=1)
    questions = parsed.get("questions") if isinstance(parsed, dict) else None
    if not isinstance(questions, list):
        questions = []
    position = 0
    for entry in questions:
        if not isinstance(entry, dict):
            continue
        position += 1
        raw_number = _number(entry.get("question_number")) or position
        record_id = "%s-q%d" % (item.source_id, raw_number)
        box, crop_key, page = _bbox_and_crop(
            store, item.source_id, entry.get("diagram_bbox"), page_no, "q",
            raw_number)
        options = entry.get("options") if isinstance(entry.get("options"), list) else []
        text = _clean(entry.get("question_text"))
        # Gemini often answers "options": [] (or four label-only stubs) and
        # leaves the choices inside question_text. Split them out once here so
        # every consumer — Inspect, the importer, the report — sees choices as
        # choices and a stem without them. Signature is (raw, question_text).
        text, options = questions_opts(options, text)
        row = {
            "id": record_id, "source_id": item.source_id,
            "question_number": raw_number, "question_text": text,
            "options": options, "bbox": box, "page": page, "crop_key": crop_key,
            "crop_state": "cut" if crop_key else "pending",
            "tags": [t for t in (item.subject, item.topic) if t],
            "caption": _clean(entry.get("caption")),
            "table_text": _clean(entry.get("table_text")),
            "table_format": entry.get("table_format") or (
                "markdown_table" if entry.get("table_text") else None),
            "status": "staged" if (text or options) else "empty",
            "fields": {"subject": item.subject, "topic": item.topic,
                       "grade": item.grade, "corp": entry.get("corp"),
                       "year": entry.get("year"),
                       "difficulty": entry.get("difficulty")},
            "ocr_json": entry,
        }
        store.upsert_question(row)
        stored = store.get("question", record_id)
        if stored is None:
            continue
        result.question_ids.append(record_id)
        if crop_key:
            result.crop_files += 1
        if stored["status"] == "empty":
            result.empty_records.append(("question", record_id))
    return result


def stage_answers(store, item, parsed, candidates, page_no=1):
    """Write one OCR reply's answers into staging with a first-pass link."""
    from .matching import link_candidates

    result = StagedFile(item.source_id, item.rel_path, pages=1)
    answers = parsed.get("answers") if isinstance(parsed, dict) else None
    if not isinstance(answers, list):
        answers = []
    position = 0
    for entry in answers:
        if not isinstance(entry, dict):
            continue
        position += 1
        prepared = _prepare(entry, candidates, position)
        number = _number(prepared.get("question_number")) or position
        record_id = "%s-a%d" % (item.source_id, number)
        box, crop_key, page = _bbox_and_crop(
            store, item.source_id, entry.get("diagram_bbox"), page_no, "a",
            number)
        link = link_candidates(prepared, candidates, subject=item.subject,
                               topic=item.topic, grade=item.grade)
        explanation = _clean(prepared.get("answer_explanation"))
        row = {
            "id": record_id, "source_id": item.source_id,
            "question_number": number, "answer_explanation": explanation,
            "assets": prepared.get("assets") or [], "bbox": box, "page": page,
            "crop_key": crop_key,
            "link_kind": link["link_kind"], "link_id": link["link_id"],
            "match_method": link["match_method"], "match_state": link["match_state"],
            "candidates": link["candidates"],
            "tags": [t for t in (item.subject, item.topic) if t],
            "status": "staged" if explanation else "empty",
            "fields": {"subject": item.subject, "topic": item.topic,
                       "grade": item.grade, "corp": prepared.get("corp"),
                       "year": prepared.get("year"),
                       "difficulty": prepared.get("difficulty")},
            "ocr_json": entry,
        }
        store.upsert_answer(row)
        stored = store.get("answer", record_id)
        if stored is None:
            continue
        result.answer_ids.append(record_id)
        if crop_key:
            result.crop_files += 1
        if stored["status"] == "empty":
            result.empty_records.append(("answer", record_id))
    return result


def _prepare(entry, candidates, position):
    """One raw answer entry -> the legacy 'prepared' shape (options resolved)."""
    from .answers import prepare_answer_rows
    try:
        prepared = prepare_answer_rows({"answers": [entry]})
    except Exception:
        prepared = []
    if prepared:
        row = dict(prepared[0])
        row.setdefault("question_number", entry.get("question_number") or position)
        return row
    return {"answer_number": position, "question_number": entry.get("question_number"),
            "answer_explanation": entry.get("answer_explanation"),
            "correct_option_label": entry.get("correct_option_label"),
            "correct_option_text": entry.get("correct_option_text"),
            "corp": entry.get("corp"), "year": entry.get("year"),
            "difficulty": entry.get("difficulty"),
            "raw_ocr_text": entry.get("raw_ocr_text"),
            "assets": entry.get("assets") or []}


def stage_file(store, item, parsed, *, pages_data=None, candidates=None, db=None):
    """Stage one OCR file end to end (mirror pages, then rows).

    ``pages_data`` is the ``[(page_no, jpeg)]`` list already rendered for the
    OCR call, so the same bytes are stored instead of a second render.
    """
    if pages_data is None:
        with open(item.file_path, "rb") as fh:
            pages_data = page_images(fh.read(), item.mime_type)
    sizes, _ = stage_images(store, item.source_id, pages_data)
    main = sizes[0] if sizes else {"w": None, "h": None}
    page_count = len(sizes)
    store.upsert_source({
        "source_id": item.source_id, "rel_path": item.rel_path,
        "local_path": item.file_path, "subject": item.subject,
        "grade": item.grade, "topic": item.topic, "type": item.type,
        "page_count": page_count, "page_w": main.get("w"), "page_h": main.get("h"),
        "question_file": item.source_id if item.type == "سوال" else None,
        "answer_file": item.source_id if item.type != "سوال" else None,
        "ocr_at": _now(), "status": "staged",
        "meta": {"mime_type": item.mime_type, "file_name": item.file_name,
                 "file_size": item.file_size, "pages": sizes},
    })
    staged = stage_questions(store, item, parsed, page_no=1) \
        if item.type == "سوال" else \
        stage_answers(store, item, parsed, candidates or [], page_no=1)
    staged.pages = page_count
    if staged.is_empty:
        # Empty extraction is recorded, visibly, and the source says so.
        store.upsert_source({"source_id": item.source_id, "rel_path": item.rel_path,
                             "status": "empty"})
        staged.empty_records.append(("source", item.source_id))
    return staged


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64(data):
    return base64.b64encode(data).decode()
