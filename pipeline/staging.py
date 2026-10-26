"""Local SQLite staging store: where OCR output lands before any human review.

An OCR run writes here and nowhere else — no Postgres row, no bucket object.
A human inspects/edits the staged rows (``ui/inspect.js``), marks them ready,
and only ``pipeline.importer`` writes the live database and bucket.

Layout on disk (all local, gitignored)::

    staging/staging.db                        SQLite: sources, questions, answers, batches
    staging/assets/<source_id>/p<N>.jpg       one page image per source page
    staging/assets/<source_id>/q<N>.<i>.jpg   a cut crop (deterministic name)
    staging/assets/<source_id>/source.pdf     a local copy of the source file

Identity contract (``docs/db-contract.md``):
``source_id = sha1(rel_path)[:40]``; a question is ``<source_id>-q<N>`` and an
answer ``<source_id>-a<N>`` (N = the printed question number, or the 1-based
position when the page carried none), so re-running OCR over the same file maps
onto the same rows instead of forking them.

Statuses — a record moves forward only, and OCR can never move it back:

``empty``    staged, but the OCR found nothing (kept and shown, never archived silently)
``staged``   written by OCR, untouched
``edited``   a human changed a field
``ready``    a human marked it importable
``imported`` written to the live database by the importer
``skipped``  a human excluded it from this batch
"""
import contextlib
import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from contextlib import contextmanager

DEFAULT_PATH = os.path.join("staging", "staging.db")

SOURCE_STATUSES = ("empty", "staged", "edited", "ready", "imported", "skipped")
RECORD_STATUSES = ("empty", "staged", "edited", "ready", "imported", "skipped")
# A record in one of these states is a human/final decision: OCR may not touch it.
PROTECTED = ("edited", "ready", "imported", "skipped")
MATCH_STATES = ("auto_ok", "manual_ok", "unlinked_ok", "needs_manual")
BATCH_STATUSES = ("staged", "reviewed", "importing", "imported", "failed", "aborted")

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS st_sources (
    source_id      TEXT PRIMARY KEY,
    rel_path       TEXT NOT NULL UNIQUE,
    local_path     TEXT,
    subject        TEXT,
    grade          TEXT,
    topic          TEXT,
    type           TEXT,
    corp           TEXT,
    year           TEXT,
    question_file  TEXT,
    answer_file    TEXT,
    page_count     INTEGER,
    page_w         INTEGER,
    page_h         INTEGER,
    ocr_at         TEXT,
    status         TEXT NOT NULL DEFAULT 'staged',
    file_fault     TEXT,
    meta_json      TEXT NOT NULL DEFAULT '{}',
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS st_questions (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES st_sources(source_id),
    question_number INTEGER,
    question_text  TEXT,
    options_json   TEXT NOT NULL DEFAULT '[]',
    fields_json    TEXT NOT NULL DEFAULT '{}',
    bbox_json      TEXT,
    page           INTEGER,
    crop_key       TEXT,
    crop_state     TEXT NOT NULL DEFAULT 'pending',
    tags_json      TEXT NOT NULL DEFAULT '[]',
    caption        TEXT,
    table_text     TEXT,
    table_format   TEXT,
    status         TEXT NOT NULL DEFAULT 'staged',
    ocr_json       TEXT NOT NULL DEFAULT '{}',
    edit_json      TEXT NOT NULL DEFAULT '{}',
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS st_answers (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES st_sources(source_id),
    question_number INTEGER,
    answer_explanation TEXT,
    assets_json    TEXT NOT NULL DEFAULT '[]',
    fields_json    TEXT NOT NULL DEFAULT '{}',
    bbox_json      TEXT,
    page           INTEGER,
    crop_key       TEXT,
    tags_json      TEXT NOT NULL DEFAULT '[]',
    link_kind      TEXT NOT NULL DEFAULT 'staged'
                     CHECK(link_kind IN ('staged','db')),
    link_id        TEXT,
    match_method   TEXT,
    match_state    TEXT NOT NULL DEFAULT 'needs_manual'
                     CHECK(match_state IN ('auto_ok','manual_ok','unlinked_ok','needs_manual')),
    candidates_json TEXT NOT NULL DEFAULT '[]',
    status         TEXT NOT NULL DEFAULT 'staged',
    ocr_json       TEXT NOT NULL DEFAULT '{}',
    edit_json      TEXT NOT NULL DEFAULT '{}',
    notes          TEXT,
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS st_batches (
    batch_id       TEXT PRIMARY KEY,
    label          TEXT,
    scope_json     TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'staged'
                     CHECK(status IN ('staged','reviewed','importing','imported','failed','aborted')),
    total          INTEGER NOT NULL DEFAULT 0,
    imported       INTEGER NOT NULL DEFAULT 0,
    failed         INTEGER NOT NULL DEFAULT 0,
    report_json    TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS st_batch_rows (
    batch_id       TEXT NOT NULL REFERENCES st_batches(batch_id),
    kind           TEXT NOT NULL CHECK(kind IN ('source','question','answer')),
    record_id      TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending',
    error          TEXT,
    updated_at     TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (batch_id, kind, record_id)
);
CREATE INDEX IF NOT EXISTS st_questions_scope ON st_questions(source_id, question_number);
CREATE INDEX IF NOT EXISTS st_answers_scope   ON st_answers(source_id, question_number);
CREATE INDEX IF NOT EXISTS st_questions_status ON st_questions(status);
CREATE INDEX IF NOT EXISTS st_answers_status   ON st_answers(status);
"""

# Columns a v1 staging.db (the foundation commit) is missing; added in place so
# an existing local store keeps its rows.
_MIGRATIONS = {
    "st_sources": ("corp", "year", "question_file", "answer_file", "page_count",
                   "status", "file_fault", "meta_json"),
    "st_questions": ("page", "crop_key", "tags_json", "caption", "table_text",
                     "table_format", "edit_json", "notes"),
    "st_answers": ("page", "crop_key", "tags_json", "candidates_json",
                   "edit_json", "notes"),
}

_TABLES = {"question": "st_questions", "answer": "st_answers"}


def source_id_for(rel_path):
    """``sha1('/'-normalised rel_path)[:40]`` — the contract's source identity."""
    rel = str(rel_path or "").replace("\\", "/").strip().lstrip("/")
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:40]


def _json(value, default=None):
    if value is None:
        return json.dumps(default if default is not None else {}, ensure_ascii=False,
                          separators=(",", ":"))
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except ValueError:
            pass
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _nullable(value):
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return _json(value)


def _text(value):
    return None if value is None else str(value)


def _lock_for(path):
    path = os.path.abspath(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


class StagingStore:
    """Thread-safe, idempotent staging store. Each method owns a transaction."""

    def __init__(self, path=DEFAULT_PATH, root=None):
        self.path = os.fspath(path)
        self.root = os.fspath(root) if root else os.path.dirname(os.path.abspath(self.path))
        self.lock = _lock_for(self.path)
        self._closed = False
        if self.path != ":memory:":
            os.makedirs(self.root, exist_ok=True)
        with contextlib.closing(self._connect()) as db:
            db.executescript(SCHEMA)
            self._add_missing_columns(db)

    # ── plumbing ────────────────────────────────────────────────
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        if self.path != ":memory:":
            db.execute("PRAGMA journal_mode = WAL")
        return db

    def _add_missing_columns(self, db):
        for table, columns in _MIGRATIONS.items():
            existing = {row["name"] for row in db.execute("PRAGMA table_info(%s)" % table)}
            for column in columns:
                if column not in existing:
                    db.execute("ALTER TABLE %s ADD COLUMN %s TEXT" % (table, column))
        db.commit()

    @contextmanager
    def _transaction(self):
        with self.lock:
            if self._closed:
                raise ValueError("StagingStore is closed")
            db = self._connect()
            try:
                db.execute("BEGIN IMMEDIATE")
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise
            finally:
                db.close()

    def close(self):
        with self.lock:
            self._closed = True

    @property
    def closed(self):
        return self._closed

    # ── sources ─────────────────────────────────────────────────
    def upsert_source(self, row):
        """Insert or refresh one source. A human/final status is never reset."""
        source_id = row.get("source_id") or source_id_for(row.get("rel_path"))
        rel_path = str(row.get("rel_path") or "").replace("\\", "/")
        with self._transaction() as db:
            existing = db.execute("SELECT status FROM st_sources WHERE source_id=?",
                                  (source_id,)).fetchone()
            status = row.get("status") or "staged"
            if existing and existing["status"] in PROTECTED:
                status = existing["status"]
            if existing:
                db.execute("""
                    UPDATE st_sources SET
                      local_path=COALESCE(?,local_path), subject=COALESCE(?,subject),
                      grade=COALESCE(?,grade), topic=COALESCE(?,topic),
                      type=COALESCE(?,type), corp=COALESCE(?,corp),
                      year=COALESCE(?,year), question_file=COALESCE(?,question_file),
                      answer_file=COALESCE(?,answer_file),
                      page_count=COALESCE(?,page_count),
                      page_w=COALESCE(?,page_w), page_h=COALESCE(?,page_h),
                      ocr_at=COALESCE(?,ocr_at), status=?,
                      file_fault=?, meta_json=?, updated_at=CURRENT_TIMESTAMP
                    WHERE source_id=?""", (
                    row.get("local_path"), row.get("subject"), _text(row.get("grade")),
                    row.get("topic"), row.get("type"), row.get("corp"), row.get("year"),
                    row.get("question_file"), row.get("answer_file"),
                    row.get("page_count"), row.get("page_w"), row.get("page_h"),
                    row.get("ocr_at"), status, row.get("file_fault"),
                    _json(row.get("meta"), {}), source_id))
            else:
                db.execute("""
                    INSERT INTO st_sources
                      (source_id,rel_path,local_path,subject,grade,topic,type,corp,
                       year,question_file,answer_file,page_count,page_w,page_h,ocr_at,
                       status,file_fault,meta_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    source_id, rel_path, row.get("local_path"), row.get("subject"),
                    _text(row.get("grade")), row.get("topic"), row.get("type"),
                    row.get("corp"), row.get("year"), row.get("question_file"),
                    row.get("answer_file"), row.get("page_count"), row.get("page_w"),
                    row.get("page_h"), row.get("ocr_at"), status,
                    row.get("file_fault"), _json(row.get("meta"), {})))
        return source_id

    def get_source(self, source_id):
        """One source with its staged records, or None."""
        with contextlib.closing(self._connect()) as db:
            row = db.execute("SELECT * FROM st_sources WHERE source_id=?",
                             (source_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["questions"] = _children(db, "st_questions", source_id)
            out["answers"] = _children(db, "st_answers", source_id)
            return out

    def find_source(self, rel_path):
        with contextlib.closing(self._connect()) as db:
            row = db.execute("SELECT source_id FROM st_sources WHERE rel_path=?",
                             (str(rel_path).replace("\\", "/"),)).fetchone()
            return row["source_id"] if row else None

    def list_sources(self, status=None, limit=200):
        # The Inspect list shows each source's own tally, because "how much is
        # staged here" is the question a reviewer asks of the list itself.
        sql = ("SELECT s.*, "
               "(SELECT COUNT(*) FROM st_questions q WHERE q.source_id = s.source_id) "
               "AS question_count, "
               "(SELECT COUNT(*) FROM st_answers a WHERE a.source_id = s.source_id) "
               "AS answer_count "
               "FROM st_sources s")
        args = []
        if status:
            sql += " WHERE s.status=?"
            args.append(status)
        sql += " ORDER BY s.updated_at DESC LIMIT ?"
        args.append(int(limit))
        with contextlib.closing(self._connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def set_source_status(self, source_id, status):
        if status not in SOURCE_STATUSES:
            raise ValueError("invalid status: %r" % (status,))
        with self._transaction() as db:
            cur = db.execute("UPDATE st_sources SET status=?, "
                             "updated_at=CURRENT_TIMESTAMP WHERE source_id=?",
                             (status, source_id))
            return cur.rowcount == 1

    # ── records ─────────────────────────────────────────────────
    def upsert_question(self, row):
        return self._upsert("question", row)

    def upsert_answer(self, row):
        return self._upsert("answer", row)

    def _upsert(self, kind, row):
        table = _TABLES[self._table_key(kind)]
        source_id = row.get("source_id") or source_id_for(row.get("rel_path"))
        record_id = row.get("id")
        if not record_id:
            raise ValueError("a staged %s needs an id" % kind)
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO st_sources(source_id,rel_path) VALUES (?,?)",
                       (source_id, str(row.get("rel_path") or "").replace("\\", "/")))
            existing = db.execute("SELECT status FROM %s WHERE id=?" % table,
                                  (record_id,)).fetchone()
            if existing and existing["status"] in PROTECTED:
                # Re-OCR must never overwrite a human edit or a final state.
                return False
            fields = row.get("fields") or {}
            known = {"id", "source_id", "rel_path", "question_number", "fields",
                     "ocr_json", "status", "page", "crop_key"}
            # A DB column value enters `fields` only when it is non-null: a null
            # here would OVERWRITE a human edit (SQLite's json_set writes it).
            fields = {**{k: v for k, v in row.items()
                         if k not in known and v is not None}, **fields}
            if kind == "question":
                cols = ("id", "source_id", "question_number", "question_text",
                        "options_json", "fields_json", "bbox_json", "page",
                        "crop_key", "crop_state", "tags_json", "caption",
                        "table_text", "table_format", "status", "ocr_json")
                vals = (record_id, source_id, row.get("question_number"),
                        row.get("question_text"), _json(row.get("options"), []),
                        _json(fields), _nullable(row.get("bbox")), row.get("page"),
                        row.get("crop_key"), row.get("crop_state", "pending"),
                        _json(row.get("tags"), []), row.get("caption"),
                        row.get("table_text"), row.get("table_format"),
                        row.get("status", "staged"), _json(row.get("ocr_json"), row))
            else:
                cols = ("id", "source_id", "question_number", "answer_explanation",
                        "assets_json", "fields_json", "bbox_json", "page", "crop_key",
                        "tags_json", "link_kind", "link_id", "match_method",
                        "match_state", "candidates_json", "status", "ocr_json")
                vals = (record_id, source_id, row.get("question_number"),
                        row.get("answer_explanation"), _json(row.get("assets"), []),
                        _json(fields), _nullable(row.get("bbox")), row.get("page"),
                        row.get("crop_key"), _json(row.get("tags"), []),
                        row.get("link_kind", "staged"), row.get("link_id"),
                        row.get("match_method"), row.get("match_state", "needs_manual"),
                        _json(row.get("candidates"), []), row.get("status", "staged"),
                        _json(row.get("ocr_json"), row))
            placeholders = ",".join("?" for _ in cols)
            updates = ",".join("%s=excluded.%s" % (c, c) for c in cols
                               if c not in ("id", "status"))
            db.execute(
                "INSERT INTO %s (%s) VALUES (%s) ON CONFLICT(id) DO UPDATE SET %s, "
                "status=CASE WHEN %s.status IN ('edited','ready','imported','skipped') "
                "THEN %s.status ELSE excluded.status END, updated_at=CURRENT_TIMESTAMP"
                % (table, ",".join(cols), placeholders, updates, table, table), vals)
        return True

    def get(self, kind, record_id):
        table = _TABLES[self._table_key(kind)]
        with contextlib.closing(self._connect()) as db:
            row = db.execute("SELECT * FROM %s WHERE id=?" % table, (record_id,)).fetchone()
            return dict(row) if row else None

    def list_records(self, kind, source_id=None, status=None, limit=500):
        table = _TABLES[self._table_key(kind)]
        sql, args, where = "SELECT * FROM %s" % table, [], []
        if source_id:
            where.append("source_id=?")
            args.append(source_id)
        if status:
            where.append("status=?")
            args.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY source_id, question_number LIMIT ?"
        args.append(int(limit))
        with contextlib.closing(self._connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def set_status(self, kind, record_id, status):
        return self._set_status(_TABLES[self._table_key(kind)], record_id,
                                status, RECORD_STATUSES)

    def update_record(self, kind, record_id, values):
        """A human edit: write the columns and move the row to ``edited``.

        ``values`` may carry any writable column; the JSON-shaped ones accept
        Python objects. An ``imported`` row is frozen (the live DB owns it).
        Returns the updated row; raises ValueError for an unknown column.
        """
        table = _TABLES[self._table_key(kind)]
        json_cols = {"options": "options_json", "fields": "fields_json",
                     "tags": "tags_json", "assets": "assets_json",
                     "candidates": "candidates_json", "bbox": "bbox_json"}
        plain = {"question_text", "answer_explanation", "caption", "table_text",
                 "table_format", "notes", "crop_key", "crop_state", "page",
                 "question_number", "link_kind", "link_id", "match_method",
                 "match_state", "status"}
        meta = {"subject", "topic", "grade", "corp", "year", "difficulty"}
        sets, args, meta_values = [], [], {}
        for key, value in (values or {}).items():
            if key in json_cols:
                sets.append("%s=?" % json_cols[key])
                args.append(_nullable(value) if key == "bbox" else _json(value))
            elif key in plain:
                if key == "status" and value not in RECORD_STATUSES:
                    raise ValueError("unknown status: %r" % (value,))
                sets.append("%s=?" % key)
                args.append(value)
            elif key in meta:
                # Metadata lives in the JSONB-shaped fields_json. One json_set call
                # carries every metadata key: SQLite evaluates the SET list against
                # the ORIGINAL row, so two json_set calls on one column would lose the
                # first one. json_patch is not usable either - it DELETES a key whose
                # written value is null.
                meta_values[key] = value
            else:
                raise ValueError("not an editable field: %s" % key)
        if meta_values:
            sets.append("fields_json=json_set(COALESCE(NULLIF(fields_json,''),"
                        "'{}'),%s)" % ",".join("'$.%s',?" % k for k in meta_values))
            args.extend(meta_values.values())
        if not sets:
            return self.get(kind, record_id)
        with self._transaction() as db:
            current = db.execute("SELECT status FROM %s WHERE id=?" % table,
                                 (record_id,)).fetchone()
            if not current:
                raise KeyError(record_id)
            if current["status"] == "imported":
                raise ValueError("an imported row is owned by the live database")
            status = values.get("status") or (
                "ready" if current["status"] == "ready" else "edited")
            db.execute("UPDATE %s SET %s, status=?, updated_at=CURRENT_TIMESTAMP "
                       "WHERE id=?" % (table, ",".join(sets)),
                       (*args, status, record_id))
        return self.get(kind, record_id)

    # ── files ───────────────────────────────────────────────────
    def asset_dir(self, source_id):
        path = os.path.join(self.root, "assets", str(source_id))
        os.makedirs(path, exist_ok=True)
        return path

    def page_path(self, source_id, page):
        return os.path.join(self.asset_dir(source_id), "p%d.jpg" % int(page))

    def crop_path(self, source_id, name):
        return os.path.join(self.asset_dir(source_id), os.path.basename(str(name)))

    def save_page(self, source_id, page, data):
        """Persist one page image atomically (temp file then replace)."""
        return self._write(self.page_path(source_id, page), data)

    def save_crop(self, source_id, name, data):
        return self._write(self.crop_path(source_id, name), data)

    def read_asset(self, source_id, name):
        path = self.crop_path(source_id, name)
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            return fh.read()

    def _write(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return path

    def mirror_source(self, source_id, local_path):
        """Keep a copy of the source file so a review never needs the input tree."""
        if not local_path or not os.path.exists(local_path):
            return None
        dest = os.path.join(self.asset_dir(source_id),
                            "source" + os.path.splitext(local_path)[1].lower())
        if not os.path.exists(dest):
            shutil.copy2(local_path, dest)
        return dest

    # ── batches ─────────────────────────────────────────────────
    def create_batch(self, label="", scope=None, batch_id=None, rows=(), total=0):
        batch_id = batch_id or uuid.uuid4().hex[:12]
        with self._transaction() as db:
            db.execute("INSERT INTO st_batches(batch_id,label,scope_json,total) "
                       "VALUES (?,?,?,?) ON CONFLICT(batch_id) DO NOTHING",
                       (batch_id, label, _json(scope, {}), int(total)))
            for kind, record_id in rows:
                db.execute("INSERT OR IGNORE INTO st_batch_rows(batch_id,kind,record_id) "
                           "VALUES (?,?,?)", (batch_id, kind, record_id))
        return self.get_batch(batch_id)

    def get_batch(self, batch_id):
        with contextlib.closing(self._connect()) as db:
            row = db.execute("SELECT * FROM st_batches WHERE batch_id=?",
                             (batch_id,)).fetchone()
            if not row:
                return None
            out = dict(row)
            out["rows"] = [dict(r) for r in db.execute(
                "SELECT * FROM st_batch_rows WHERE batch_id=? ORDER BY rowid",
                (batch_id,))]
            return out

    def list_batches(self, limit=50):
        with contextlib.closing(self._connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM st_batches ORDER BY created_at DESC LIMIT ?",
                (int(limit),))]

    def set_batch_status(self, batch_id, status, **counts):
        if status not in BATCH_STATUSES:
            raise ValueError("unknown batch status: %r" % (status,))
        sets = ["status=?", "updated_at=CURRENT_TIMESTAMP"]
        args = [status]
        for key in ("imported", "failed", "total"):
            if key in counts:
                sets.append("%s=?" % key)
                args.append(int(counts[key]))
        if "report" in counts:
            sets.append("report_json=?")
            args.append(_json(counts["report"], {}))
        args.append(batch_id)
        with self._transaction() as db:
            db.execute("UPDATE st_batches SET %s WHERE batch_id=?" % ",".join(sets), args)
        return self.get_batch(batch_id)

    def set_batch_row(self, batch_id, kind, record_id, status, error=None):
        with self._transaction() as db:
            db.execute("UPDATE st_batch_rows SET status=?, error=?, "
                       "updated_at=CURRENT_TIMESTAMP WHERE batch_id=? AND kind=? "
                       "AND record_id=?", (status, error, batch_id, kind, record_id))

    def batch_rows(self, batch_id, pending_only=False):
        sql = "SELECT * FROM st_batch_rows WHERE batch_id=?"
        if pending_only:
            sql += " AND status='pending'"
        sql += " ORDER BY rowid"
        with contextlib.closing(self._connect()) as db:
            return [dict(r) for r in db.execute(sql, (batch_id,))]

    # ── helpers ─────────────────────────────────────────────────
    @staticmethod
    def _table_key(kind):
        if kind not in _TABLES:
            raise ValueError("Invalid staging kind")
        return kind

    def _set_status(self, table, record_id, status, allowed):
        if status not in allowed:
            raise ValueError("invalid status: %r" % (status,))
        with self._transaction() as db:
            cur = db.execute("UPDATE %s SET status=?, updated_at=CURRENT_TIMESTAMP "
                             "WHERE id=?" % table, (status, record_id))
            return cur.rowcount == 1

    def counts(self):
        """How many records sit in each status — the Inspect header's numbers."""
        out = {}
        with contextlib.closing(self._connect()) as db:
            for table in ("st_sources", "st_questions", "st_answers"):
                for row in db.execute("SELECT status, COUNT(*) AS n FROM %s "
                                      "GROUP BY status" % table):
                    out.setdefault(table, {})[row["status"]] = row["n"]
        return out


def _children(db, table, source_id):
    return [dict(r) for r in db.execute(
        "SELECT * FROM %s WHERE source_id=? ORDER BY question_number" % table,
        (source_id,))]