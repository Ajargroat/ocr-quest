"""The Inspect API: read, edit and mark staged rows ready (P4).

Everything here works on the LOCAL staging store — no Postgres row, no bucket
object. The importer is the only writer of the live database, and it runs from
a human's explicit Import press.

Route map (mounted by ``main.py`` at ``/api/staging``):

``GET  /sources``            list staged sources (filter by status)
``GET  /sources/{sid}``      one source with its questions and answers
``GET  /records``            list records (kind, source, status)
``POST /records/{kind}/{id}`` one human edit (fields, bbox, caption, tags, link)
``POST /records/{kind}/{id}/status``  move a record (ready / skipped / empty)
``GET  /page/{sid}/{page}``  the page image a bbox is measured against
``GET  /crop/{sid}/{name}``  one cut crop
``POST /recrop``             re-cut a crop from an edited bbox (same key)
``GET  /counts``             status counters for the Inspect header
``POST /sources/{sid}/ready``  mark every staged record of one source ready
``GET  /vocab``              the vocabulary the reviewer validates against
``POST /vocab/alias``        accept one human-approved alias
"""
import json
import os

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import crops, textnorm, vocab
from .staging import (MATCH_STATES, RECORD_STATUSES, StagingStore,
                      source_id_for)


class Edit(BaseModel):
    """A partial edit: only the keys present are written."""
    question_text: str | None = None
    answer_explanation: str | None = None
    options: list | None = None
    tags: list | None = None
    caption: str | None = None
    table_text: str | None = None
    table_format: str | None = None
    notes: str | None = None
    bbox: list | None = None
    options: list | None = None
    subject: str | None = None
    topic: str | None = None
    grade: int | None = None
    corp: str | None = None
    year: str | None = None
    difficulty: str | None = None
    link_kind: str | None = None
    link_id: str | None = None
    match_state: str | None = None
    match_method: str | None = None


class Recrop(BaseModel):
    source_id: str
    kind: str
    id: str
    bbox: list
    index: int = 0


class AliasBody(BaseModel):
    alias: str
    tag: str


def _store_path(cfg):
    from .staging import DEFAULT_PATH
    return getattr(cfg, "staging_path", None) or DEFAULT_PATH


def build_router(cfg):
    """The router, bound to the live config (call from ``main.py``)."""
    api = APIRouter(prefix="/api/staging")

    def store():
        return StagingStore(_store_path(cfg))

    def _bad(message, code=400):
        return JSONResponse({"ok": False, "error": message}, status_code=code)

    # ── reads ───────────────────────────────────────────────────
    @api.get("/counts")
    async def counts():
        with _closedable(store()) as st:
            return JSONResponse({"counts": st.counts()})

    @api.get("/sources")
    async def sources(status: str = "", limit: int = 200):
        with _closedable(store()) as st:
            rows = st.list_sources(status or None, limit=min(int(limit), 1000))
        for row in rows:
            row.pop("meta_json", None)
        return JSONResponse({"sources": rows})

    @api.get("/sources/{source_id}")
    async def source(source_id: str):
        with _closedable(store()) as st:
            row = st.get_source(source_id)
        if not row:
            return _bad("no such staged source", 404)
        # The reviewer works on DECODED rows (bbox, options, candidates); the
        # columns in the store are raw JSON text, so each record is shaped by
        # the same `_view` the record routes use.
        row["questions"] = [_view("question", r) for r in row["questions"]]
        row["answers"] = [_view("answer", r) for r in row["answers"]]
        row["question_count"] = len(row["questions"])
        row["answer_count"] = len(row["answers"])
        row["page_url"] = "/api/staging/page/%s/1" % source_id
        return JSONResponse(row)

    @api.get("/records")
    async def records(kind: str, source_id: str = "", status: str = "",
                      limit: int = 200):
        if kind not in ("question", "answer"):
            return _bad("kind must be question or answer")
        with _closedable(store()) as st:
            rows = st.list_records(kind, source_id or None, status or None,
                                   limit=min(int(limit), 1000))
        return JSONResponse({"records": [_view(kind, r) for r in rows]})

    @api.get("/vocab")
    async def vocabulary():
        tags = vocab.load()

        def names(entries):
            """The seed mixes plain strings (subjects) with tagged objects
            (corps); the picker only needs the canonical name of each."""
            out = []
            for entry in entries or []:
                name = entry if isinstance(entry, str) else entry.get("tag")
                if name:
                    out.append(name)
            return out

        return JSONResponse({"corps": names(tags.get("corps")),
                             "subjects": names(tags.get("subjects")),
                             "topics": names(tags.get("chapters"))})

    @api.get("/page/{source_id}/{page}")
    async def page(source_id: str, page: int):
        with _closedable(store()) as st:
            path = st.page_path(source_id, page)
        if not os.path.exists(path):
            return _bad("no such page", 404)
        with open(path, "rb") as fh:
            return Response(content=fh.read(), media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=600"})

    @api.get("/crop/{source_id}/{name}")
    async def crop(source_id: str, name: str):
        with _closedable(store()) as st:
            data = st.read_asset(source_id, name)
        if data is None:
            return _bad("no such crop", 404)
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=600"})

    # ── writes ──────────────────────────────────────────────────
    @api.post("/records/{kind}/{record_id}")
    async def edit(kind: str, record_id: str, body: Edit):
        """One human edit. Text is repaired the same way OCR text is, so the
        reviewer never has to fix ZWNJ or bare TeX by hand."""
        if kind not in ("question", "answer"):
            return _bad("kind must be question or answer")
        values = {k: v for k, v in body.model_dump().items() if v is not None}
        for field in ("question_text", "answer_explanation", "table_text"):
            if field in values:
                values[field] = textnorm.restore_zwnj(textnorm.canonical(values[field]))
        if "match_state" in values and values["match_state"] not in MATCH_STATES:
            return _bad("invalid match_state: %r" % values["match_state"])
        if "bbox" in values and values["bbox"] and crops.normalize(values["bbox"]) is None:
            return _bad("bbox must be 4 numbers on a 0-1000 scale")
        if "options" in values:
            cleaned = []
            for position, option in enumerate(values["options"] or []):
                if isinstance(option, str):
                    option = {"text": option}
                if not isinstance(option, dict):
                    return _bad("each option must be {label, text}")
                text = str(option.get("text") or "").strip()
                if not text:
                    continue          # an empty row is a blank, not a choice
                label = str(option.get("label") or (position + 1)).strip()
                cleaned.append({"label": label or str(position + 1),
                                "text": text})
            values["options"] = cleaned
        with _closedable(store()) as st:
            try:
                row = st.update_record(kind, record_id, values)
            except KeyError:
                return _bad("no such record", 404)
            except ValueError as exc:
                return _bad(str(exc))
        return JSONResponse({"ok": True, "record": _view(kind, row)})

    @api.post("/records/{kind}/{record_id}/status")
    async def status(kind: str, record_id: str, value: str):
        if kind not in ("question", "answer"):
            return _bad("kind must be question or answer")
        if value not in RECORD_STATUSES:
            return _bad("invalid status: %r" % value)
        with _closedable(store()) as st:
            current = st.get(kind, record_id)
            if not current:
                return _bad("no such record", 404)
            # `imported` is a one-way door: the live row exists and owns its
            # text now, so reopening the record would let a second import
            # overwrite a bank row the owner has since reviewed.
            if current["status"] == "imported" and value != "imported":
                return _bad("an imported record cannot be reopened")
            if not st.set_status(kind, record_id, value):
                return _bad("no such record", 404)
            row = st.get(kind, record_id)
        return JSONResponse({"ok": True, "record": _view(kind, row)})

    @api.post("/sources/{source_id}/ready")
    async def ready_source(source_id: str, only: str = "staged"):
        """Mark every record of one source ready (the batch-review shortcut).

        ``only=staged`` skips rows a human already edited, marked ready or
        skipped, so the button never resurrects a deliberate decision.
        """
        with _closedable(store()) as st:
            moved = 0
            for kind in ("question", "answer"):
                for row in st.list_records(kind, source_id, only or None,
                                           limit=1000):
                    if st.set_status(kind, row["id"], "ready"):
                        moved += 1
            if not moved and only != "staged":
                return _bad("nothing to mark ready", 404)
            st.set_source_status(source_id, "ready")
        return JSONResponse({"ok": True, "moved": moved})

    @api.post("/recrop")
    async def recrop(body: Recrop):
        """Re-cut one crop from an edited bbox under the SAME deterministic
        key, so a row never accumulates crop variants."""
        if body.kind not in ("question", "answer"):
            return _bad("kind must be question or answer")
        with _closedable(store()) as st:
            row = st.get(body.kind, body.id)
            if row is None:
                return _bad("no such record", 404)
            box = crops.normalize(body.bbox)
            if box is None:
                return _bad("bbox must be 4 numbers on a 0-1000 scale")
            page_no = row.get("page") or 1
            path = st.page_path(row["source_id"], page_no)
            if not os.path.exists(path):
                return _bad("this record has no stored page", 404)
            with open(path, "rb") as fh:
                image = crops.open_image(fh.read())
            data = crops.render(image, box)
            if data is None:
                return _bad("that box is inside the page margin")
            number = row.get("question_number")
            key = crops.crop_key(row["source_id"], body.kind[0], number,
                                 body.index)
            st.save_crop(row["source_id"], os.path.basename(key), data)
            updated = st.update_record(body.kind, body.id,
                                       {"bbox": list(box), "crop_key": key,
                                        "crop_state": "cut"})
        return JSONResponse({"ok": True, "crop_key": key,
                             "record": _view(body.kind, updated)})

    @api.post("/reparse-options")
    async def reparse_options(source_id: str = "", dry_run: bool = False):
        """Re-read the choices out of every staged row's question text.

        Rows staged before the option splitter existed still carry their
        choices glued to the stem with an empty ``options`` list, and a re-OCR
        is the only other cure. This walks the store, splits each stem once,
        and writes only the rows the splitter actually improved — so it is
        safe to run again, and ``dry_run`` reports without writing.
        """
        from .questions import clean_options
        changed = []
        with _closedable(store()) as st:
            rows = st.list_records("question", source_id=source_id or None,
                                   limit=100000)
            for row in rows:
                if row.get("status") == "imported":
                    continue                      # the live bank owns it now
                before = _decode(row.get("options_json")) or []
                stem, options = clean_options(before, row.get("question_text") or "")
                if not options or len(options) <= len(before):
                    continue
                changed.append({"id": row["id"], "before": len(before),
                                "after": len(options)})
                if not dry_run:
                    # status travels with the write: a machine re-read is not a
                    # human edit, so a `staged` row must not become `edited`
                    st.update_record("question", row["id"],
                                     {"question_text": stem, "options": options,
                                      "status": row.get("status") or "staged"})
        return JSONResponse({"ok": True, "dry_run": bool(dry_run),
                             "changed": changed, "count": len(changed)})

    @api.post("/import")
    async def run_import(db=None, limit: int = 0):
        """Import every READY staged record into the live bank (P6).

        Crop first, then the row: a picture that failed to upload stops that
        record and leaves it ready for the next attempt, so a resumed import
        never double-writes. ``ready`` becomes ``approved`` in the live
        database (the review state the owner already uses) and ``imported``
        in staging, which freezes the row against re-OCR.
        """
        from . import importer
        with _closedable(store()) as st:
            batch = importer.run(st, live=db, cfg=cfg, limit=limit or None)
        return JSONResponse(batch)

    @api.post("/vocab/alias")
    async def alias(body: AliasBody):
        """Accept one human-approved alias into the global vocabulary.

        Only a human reaches this route: the importer and the OCR prompts never
        add vocabulary on their own.
        """
        try:
            vocab.accept_alias(body.alias, body.tag)
        except (KeyError, ValueError) as exc:
            return _bad(str(exc))
        return JSONResponse({"ok": True})

    return api


class _closedable:
    """``with`` wrapper that always closes the store (SQLite + WAL handles)."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self.store

    def __exit__(self, *exc):
        self.store.close()
        return False


def _view(kind, row):
    """One record shaped for the reviewer (JSON columns decoded)."""
    if row is None:
        return None
    out = dict(row)
    for column, name in (("options_json", "options"), ("fields_json", "fields"),
                         ("tags_json", "tags"), ("bbox_json", "bbox"),
                         ("assets_json", "assets"),
                         ("candidates_json", "candidates")):
        if column in out:
            out[name] = _decode(out.pop(column))
    for column in ("ocr_json", "edit_json"):
        out.pop(column, None)
    if out.get("crop_key"):
        out["crop_url"] = "/api/staging/crop/%s/%s" % (
            out.get("source_id"), os.path.basename(out["crop_key"]))
    if out.get("source_id"):
        out["page_url"] = "/api/staging/page/%s/%s" % (
            out["source_id"], out.get("page") or 1)
    out["kind"] = kind
    return out


def _decode(value):
    if isinstance(value, (list, dict)) or value is None:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
