"""Local SQLite staging store for OCR output; no network or live DB writes."""
import json
import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_PATH = os.path.join("staging", "staging.db")
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

def _lock_for(path):
    path = os.path.abspath(path)
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())

def _json(value):
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except ValueError:
            pass
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

class StagingStore:
    """Thread-safe, idempotent staging store. Each method owns a transaction."""
    def __init__(self, path=DEFAULT_PATH):
        self.path = os.fspath(path)
        self.lock = _lock_for(self.path)
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS st_sources (
                    source_id TEXT PRIMARY KEY, rel_path TEXT NOT NULL,
                    local_path TEXT, subject TEXT, grade TEXT, topic TEXT,
                    type TEXT, page_w INTEGER, page_h INTEGER, ocr_at TEXT,
                    status TEXT NOT NULL DEFAULT 'staged'
                );
                CREATE TABLE IF NOT EXISTS st_questions (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES st_sources(source_id),
                    question_number INTEGER, question_text TEXT, options_json TEXT,
                    fields_json TEXT NOT NULL DEFAULT '{}', bbox_json TEXT,
                    crop_state TEXT NOT NULL DEFAULT 'pending',
                    status TEXT NOT NULL DEFAULT 'staged'
                      CHECK(status IN ('staged','edited','ready','imported','skipped')),
                    ocr_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS st_answers (
                    id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES st_sources(source_id),
                    question_number INTEGER, answer_explanation TEXT, assets_json TEXT,
                    fields_json TEXT NOT NULL DEFAULT '{}', bbox_json TEXT,
                    link_kind TEXT NOT NULL DEFAULT 'staged' CHECK(link_kind IN ('staged','db')),
                    link_id TEXT, match_method TEXT, match_state TEXT NOT NULL DEFAULT 'needs_manual',
                    status TEXT NOT NULL DEFAULT 'staged'
                      CHECK(status IN ('staged','edited','ready','imported','skipped')),
                    ocr_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS st_batches (
                    batch_id TEXT PRIMARY KEY, scope_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'staged', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS st_questions_scope
                    ON st_questions(source_id, question_number);
                CREATE INDEX IF NOT EXISTS st_answers_scope
                    ON st_answers(source_id, question_number);
            """)

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout = 30000")
        return db

    @contextmanager
    def _transaction(self):
        with self.lock:
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

    def upsert_source(self, row):
        source_id = row["source_id"]
        with self._transaction() as db:
            db.execute("""
                INSERT INTO st_sources
                  (source_id,rel_path,local_path,subject,grade,topic,type,page_w,page_h,ocr_at,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source_id) DO UPDATE SET
                  rel_path=excluded.rel_path, local_path=excluded.local_path,
                  subject=excluded.subject, grade=excluded.grade, topic=excluded.topic,
                  type=excluded.type, page_w=excluded.page_w, page_h=excluded.page_h,
                  ocr_at=COALESCE(excluded.ocr_at,st_sources.ocr_at)
            """, (source_id,row.get("rel_path",""),row.get("local_path"),row.get("subject"),
                  str(row["grade"]) if row.get("grade") is not None else None,
                  row.get("topic"),row.get("type"),row.get("page_w"),row.get("page_h"),
                  row.get("ocr_at"),row.get("status","staged")))

    def upsert_question(self, row):
        self._upsert_record("st_questions", row, "question")

    def upsert_answer(self, row):
        self._upsert_record("st_answers", row, "answer")

    def _upsert_record(self, table, row, kind):
        source_id, record_id = row["source_id"], row["id"]
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO st_sources(source_id,rel_path) VALUES (?,?)",
                       (source_id, row.get("rel_path", "")))
            existing = db.execute(f"SELECT status, fields_json, bbox_json FROM {table} WHERE id=?",
                                  (record_id,)).fetchone()
            if existing and existing["status"] in ("edited", "ready", "imported", "skipped"):
                # OCR refresh must never overwrite human edits or final state.
                return
            fields = row.get("fields", {})
            known = {"id","source_id","question_number","question_text","answer_explanation",
                     "options","assets","bbox","bbox_json","crop_state","status","ocr_json",
                     "fields","link_kind","link_id","match_method","match_state","rel_path"}
            fields = {**{k:v for k,v in row.items() if k not in known}, **fields}
            if kind == "question":
                cols = ("id","source_id","question_number","question_text","options_json",
                        "fields_json","bbox_json","crop_state","status","ocr_json")
                vals = (record_id,source_id,row.get("question_number"),row.get("question_text"),
                        _json(row.get("options",[])),_json(fields),_json(row.get("bbox")) if row.get("bbox") is not None else row.get("bbox_json"),
                        row.get("crop_state","pending"),row.get("status","staged"),_json(row.get("ocr_json",row)))
            else:
                cols = ("id","source_id","question_number","answer_explanation","assets_json",
                        "fields_json","bbox_json","link_kind","link_id","match_method","match_state","status","ocr_json")
                vals = (record_id,source_id,row.get("question_number"),row.get("answer_explanation"),
                        _json(row.get("assets",[])),_json(fields),_json(row.get("bbox")) if row.get("bbox") is not None else row.get("bbox_json"),
                        row.get("link_kind","staged"),row.get("link_id"),row.get("match_method"),
                        row.get("match_state","needs_manual"),row.get("status","staged"),_json(row.get("ocr_json",row)))
            placeholders = ",".join("?" for _ in cols)
            updates = ",".join(f"{col}=excluded.{col}" for col in cols if col not in ("id","status"))
            db.execute(f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
                       f"ON CONFLICT(id) DO UPDATE SET {updates}, status=CASE "
                       f"WHEN {table}.status IN ('edited','ready','imported','skipped') "
                       f"THEN {table}.status ELSE excluded.status END, updated_at=CURRENT_TIMESTAMP", vals)

    def set_status(self, kind, record_id, status):
        if kind not in ("question", "answer") or status not in ("staged","edited","ready","imported","skipped"):
            raise ValueError("Invalid staging kind or status")
        table = "st_questions" if kind == "question" else "st_answers"
        with self._transaction() as db:
            cur = db.execute(f"UPDATE {table} SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                             (status,record_id))
            return cur.rowcount == 1

    def get(self, kind, record_id):
        if kind not in ("question", "answer"):
            raise ValueError("Invalid staging kind")
        table = "st_questions" if kind == "question" else "st_answers"
        with self._connect() as db:
            row = db.execute(f"SELECT * FROM {table} WHERE id=?", (record_id,)).fetchone()
            return dict(row) if row else None

    def close(self):
        """Compatibility no-op; connections are scoped to individual operations."""
        return None
