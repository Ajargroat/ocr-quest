"""One-off, lossless copy of the flat OCR DB into the lessons model.

Nothing here runs at import or at startup: ``main()`` only connects when the
file is executed, and ``--apply`` is required to write a single row. Dry run is
the default, so a fat-fingered run prints what it *would* do. No DDL either -
the schema is pipeline/studio/migrations.sql, applied by the user first.

    python -m pipeline.studio.migrate_lessons --target-dsn "postgresql://..."
    python -m pipeline.studio.migrate_lessons --target-dsn "postgresql://..." --apply

Nothing imports this module: no startup path, importer or endpoint calls it.
The old Supabase DB stays the rollback copy.
"""
import argparse
import json
import re
import sys

import psycopg2
from psycopg2.extras import Json, execute_values

from pipeline.config import load_config
from pipeline.studio.tags import fold

CHUNK = 200
SCHEMA_FILE = "pipeline/studio/migrations.sql"

# Copy order (FK-safe): the mirror, then the source rows, then the lessons tree
# built from them, then everything that points at a question.
GROUPS = ("accounts", "sources", "courses", "chapters", "lessons", "questions",
          "options", "blocks", "answers", "reports")

ACCOUNT_TABLES = ("tenants", "domains", "users", "students", "tests",
                  "questions_sapienstech", "test_questions")

_ENV = re.compile(r"(\\begin\{([a-zA-Z*]+)\}.*?\\end\{\2\})", re.S)
_OBJECT = re.compile(r"/storage/v1/object/(?:public/)?[^/]+/(.+)$")
_QID = re.compile(r"-q(\d+)$")
_GRADE_FA = {10: "\u062f\u0647\u0645", 11: "\u06cc\u0627\u0632\u062f\u0647\u0645",
             12: "\u062f\u0648\u0627\u0632\u062f\u0647\u0645"}
_SUBJECT_FA = {"zist": "\u0632\u06cc\u0633\u062a\u200c\u0634\u0646\u0627\u0633\u06cc",
               "shimi": "\u0634\u06cc\u0645\u06cc",
               "physic": "\u0641\u06cc\u0632\u06cc\u06a9",
               "riazi": "\u0631\u06cc\u0627\u0636\u06cc",
               "zamin": "\u0632\u0645\u06cc\u0646\u200c\u0634\u0646\u0627\u0633\u06cc"}


# ── C2b: the repairs, as pure functions ───────────────────────────────────

def fence_latex(text):
    """Wraps each bare LaTeX environment in $$ so MathJax renders it (C2b).

    Only the environment is wrapped, never the whole field: several legacy rows
    are Persian prose followed by one array, and fencing the prose would ask
    MathJax to typeset the prose. A field that already carries a $$ or \\[
    fence anywhere is left alone - double-fencing breaks MathJax as badly as no
    fencing.
    """
    if not text or "\\begin{" not in text:
        return text
    if "$$" in text or "\\[" in text:
        return text
    return _ENV.sub(lambda m: "$$" + m.group(1) + "$$", text)


def _loads(raw):
    """A parsed JSON value, or [] for anything unreadable."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return []
    return raw if raw is not None else []


def media_from_legacy(diagram_url, diagram_bbox, source_storage_url):
    """The legacy picture spec as a media_images array (Q12 interface).

    Returns ``(entries, missing)``. The live rows whose diagram url ends in
    `<bucket>/unknown` name no usable object, so the source file's own storage
    url is the fallback picture; when neither resolves, the row is marked
    unreadable (that is the 329 `/unknown` rows measured 2026-10-19).
    """
    def _bbox(value):
        bbox = _loads(value)
        return bbox if is_real_bbox(bbox) else None

    if not (diagram_url or "").strip():
        # No picture was recorded for this row: nothing to show, not an error.
        return [], False
    object_name = None
    for url in (diagram_url, source_storage_url):
        match = _OBJECT.search(url or "")
        if not match:
            continue
        candidate = match.group(1)
        if candidate == "unknown" or candidate.endswith("/unknown"):
            continue
        object_name = candidate
        break
    if not object_name:
        return [], True
    return [{"object": object_name, "bbox": _bbox(diagram_bbox), "page": 1}], False


def duplicate_legacy_ids(rows):
    """The ids whose `<n>` in `<sha1>-q<n>` recurs with the same question text.

    Numbers alone are useless as a duplicate key: every file numbers its own
    questions q1..qN, so 3,839 of 3,931 rows share a number with another file.
    The flag therefore also requires the same prompt text (whitespace-normalised)
    under a different source - 6 groups / 12 rows measured on the live DB
    (2026-10-19).
    """
    def key(row):
        match = _QID.search(row.get("id") or "")
        if not match:
            return None
        text = re.sub(r"\s+", " ", row.get("question_text") or "").strip()
        return (match.group(1), text)

    sources = {}
    for row in rows:
        k = key(row)
        if k:
            sources.setdefault(k, set()).add(row.get("source_id"))
    out = set()
    for row in rows:
        k = key(row)
        if k and len(sources[k]) > 1:
            out.add(row["id"])
    return out


def question_kind(options_json):
    """`short_answer` for an optionless question, else `mcq_single`.

    Never `mcq_multi`: the legacy data has at most one correct label per
    question, so a multi-select here would be invented data.
    """
    return "short_answer" if not _options(options_json) else "mcq_single"


def options_rows(options_json, correct_label):
    """The question_options rows for one question's option list."""
    out = []
    for i, opt in enumerate(_options(options_json), 1):
        out.append({"label": opt["label"], "position": i,
                    "is_correct": str(opt["label"]) == str(correct_label)})
    return out


def grade_label(grade):
    return _GRADE_FA.get(grade)


def subject_fa(slug):
    return _SUBJECT_FA.get(slug, slug)


def _options(options_json):
    """The normalised [{label, text}] list of an options cell."""
    from pipeline.questions import normalize_options
    return normalize_options(_loads(options_json))


def is_real_bbox(bbox):
    """A bbox that encloses something - [0,0,0,0] is Gemini's 'no figure'."""
    from pipeline.questions import is_real_bbox as real
    return real(bbox)


# ── C2c: what the migration builds ────────────────────────────────────────

def course_rows(questions):
    """One course per (subject, grade) actually present in the data."""
    pairs = sorted({(q.get("subject"), q.get("grade")) for q in questions
                    if q.get("subject") and q.get("grade") is not None},
                   key=lambda p: (p[0] or "", p[1]))
    return [{"slug": "%s-%s" % (subject, grade),
             "title": "%s \u00b7 %s" % (subject_fa(subject),
                                        grade_label(grade) or grade),
             "subject": subject_fa(subject), "grade": grade, "position": i,
             "locale": "fa", "key": (subject, grade)}
            for i, (subject, grade) in enumerate(pairs, 1)]


def chapter_rows(questions, course_ids):
    """One chapter per (course, legacy topic) - the only topic source there is."""
    seen = {}
    for q in questions:
        if not q.get("subject") or q.get("grade") is None:
            continue
        key = (q.get("subject"), q.get("grade"), q.get("topic") or "")
        seen.setdefault(key, len(seen) + 1)
    out = []
    for key, position in sorted(seen.items()):
        subject, grade, topic = key
        out.append({"course_id": course_ids[(subject, grade)],
                    "slug": fold(topic) or "untitled",
                    "title": topic or "Untitled", "position": position,
                    "key": key})
    return out


def lesson_rows(sources, chapter_ids):
    """One lesson per legacy source row (slug = source id, title = file name).

    One file = one lesson is not necessarily how the author would group them;
    `lesson_blocks.position` is data, so the shape is re-editable later.
    """
    out = []
    positions = {}
    for s in sources:
        key = (s.get("subject"), s.get("grade"), s.get("topic") or "")
        if key not in chapter_ids:
            continue
        position = positions.get(key, 0) + 1
        positions[key] = position
        out.append({"chapter_id": chapter_ids[key], "slug": s.get("id"),
                    "title": s.get("file_name") or s.get("id") or "Untitled",
                    "summary": None, "reading_min": None, "position": position,
                    "status": "draft", "locale": "fa",
                    "created_at": s.get("created_at"), "key": s.get("id")})
    return out


def question_rows(questions, source_urls=None, sapienstech=None, duplicates=None):
    """The merged `questions` rows for the target (C2c).

    `id` is the target's own BIGSERIAL; the legacy `<sha1>-q<N>` id is kept in
    `legacy_id`, the OCR text moves to `prompt`, and `sapienstech_id` restores
    the tests wiring through `questions_sapienstech.supabase_id`.
    """
    source_urls = source_urls or {}
    sapienstech = sapienstech or {}
    duplicates = duplicates or set()
    out = []
    for q in questions:
        entries, missing = media_from_legacy(
            q.get("diagram_url"), q.get("diagram_bbox"),
            source_urls.get(q.get("source_id")))
        out.append({
            "legacy_id": q.get("id"),
            "type": question_kind(q.get("options")),
            "prompt": fence_latex(q.get("question_text") or ""),
            "source_id": q.get("source_id"),
            "question_number": q.get("question_number"),
            "options": q.get("options") or "[]",
            "media_images": json.dumps(entries, ensure_ascii=False),
            "subject": q.get("subject"),
            "topic": q.get("topic"),
            "grade": q.get("grade"),
            "corp": q.get("corp"),
            "year": q.get("year"),
            "difficulty": q.get("difficulty"),
            "tags": q.get("tags") or "[]",
            "review_status": q.get("review_status") or "pending",
            "raw_ocr_text": q.get("raw_ocr_text"),
            "content_hash": q.get("content_hash"),
            "split": None,                      # legacy data cannot carry it
            "duplicate": q.get("id") in duplicates,
            "unreadable": missing,
            "created_at": q.get("created_at"),
            "sapienstech_id": sapienstech.get(q.get("id")),
        })
    return out


def option_row_list(questions, question_ids=None):
    """One question_options row per option of every MCQ."""
    question_ids = question_ids or {}
    out = []
    for q in questions:
        for opt in options_rows(q.get("options"), q.get("correct_option_label")):
            opt["question_id"] = question_ids.get(q.get("id"), q.get("id"))
            out.append(opt)
    return out


def short_answer_key_rows(questions, answers, question_ids=None):
    """One key per optionless question with a linked answer.

    The linked answer's explanation is the only faithful model answer in the
    legacy data.
    """
    by_question = {}
    for a in answers:
        qid = a.get("question_id")
        if qid and a.get("answer_explanation"):
            by_question.setdefault(qid, a["answer_explanation"])
    question_ids = question_ids or {}
    out = []
    for q in questions:
        if question_kind(q.get("options")) != "short_answer":
            continue
        model_answer = by_question.get(q.get("id"))
        if not model_answer:
            continue
        out.append({"question_id": question_ids.get(q.get("id"), q.get("id")),
                    "model_answer": model_answer, "keywords": None})
    return out


def block_rows(questions, lesson_ids, question_ids=None):
    """One `question` block per migrated question (C2c).

    `position` follows the question number, which is what a reader expects; a
    number repeated inside one file (the legacy data has them) takes the next
    free position instead of colliding with UNIQUE (lesson_id, position).
    """
    question_ids = question_ids or {}
    used = {}
    out = []
    for q in questions:
        lesson_id = lesson_ids.get(q.get("source_id"))
        if lesson_id is None:
            continue
        taken = used.setdefault(lesson_id, set())
        position = q.get("question_number")
        try:
            position = int(position)
        except (TypeError, ValueError):
            position = 0
        while position < 1 or position in taken:
            position = position + 1 if position >= 1 else 1
        taken.add(position)
        out.append({"lesson_id": lesson_id, "position": position,
                    "type": "question", "body": None, "layout": None,
                    "media_id": None,
                    "question_id": question_ids.get(q.get("id"), q.get("id")),
                    "extra": "{}"})
    return out


def answer_rows(answers, question_ids=None):
    """The `answers` rows for the target (C2c).

    `id` is TEXT and keeps its value; the linked question is resolved to the
    new BIGSERIAL and the legacy text id is kept in `legacy_question_id`.
    """
    question_ids = question_ids or {}
    out = []
    for a in answers:
        legacy_qid = a.get("question_id")
        out.append({
            "id": a.get("id"),
            "legacy_id": None,
            "source_id": a.get("source_id"),
            "question_id": question_ids.get(legacy_qid) if legacy_qid else None,
            "legacy_question_id": legacy_qid,
            "question_number": a.get("question_number"),
            "answer_explanation": fence_latex(a.get("answer_explanation")),
            "correct_option_label": a.get("correct_option_label"),
            "correct_option_text": a.get("correct_option_text"),
            "assets": a.get("assets") or "[]",
            "media_images": "[]",
            "subject": a.get("subject"),
            "topic": a.get("topic"),
            "grade": a.get("grade"),
            "corp": a.get("corp"),
            "year": a.get("year"),
            "difficulty": a.get("difficulty"),
            "tags": a.get("tags") or "[]",
            "review_status": a.get("review_status") or "pending",
            "raw_ocr_text": a.get("raw_ocr_text"),
            "content_hash": a.get("content_hash"),
            "created_at": a.get("created_at"),
        })
    return out


# ── C2a: the driver ──────────────────────────────────────────────────────

QUESTION_COLS = ("legacy_id", "type", "prompt", "source_id", "question_number",
                 "options", "media_images", "subject", "topic", "grade",
                 "corp", "year", "difficulty", "tags", "review_status",
                 "raw_ocr_text", "content_hash", "split", "duplicate",
                 "unreadable", "created_at", "sapienstech_id")
ANSWER_COLS = ("id", "legacy_id", "source_id", "question_id",
               "legacy_question_id", "question_number", "answer_explanation",
               "correct_option_label", "correct_option_text", "assets",
               "media_images", "subject", "topic", "grade", "corp", "year",
               "difficulty", "tags", "review_status", "raw_ocr_text",
               "content_hash", "created_at")
LESSON_COLS = ("chapter_id", "slug", "title", "summary", "reading_min",
               "position", "status", "locale", "created_at")
CHAPTER_COLS = ("course_id", "slug", "title", "position")
COURSE_COLS = ("slug", "title", "subject", "grade", "position", "locale")
OPTION_COLS = ("question_id", "label", "is_correct", "position")
KEY_COLS = ("question_id", "model_answer", "keywords")
BLOCK_COLS = ("lesson_id", "position", "type", "body", "layout", "media_id",
              "question_id", "extra")

SOURCE_WANTED = ("id", "file_name", "mime_type", "file_size_bytes",
                 "storage_url", "subject", "grade", "topic", "type",
                 "created_at")
QUESTION_WANTED = ("id", "source_id", "question_number", "question_text",
                   "options", "subject", "topic", "grade", "corp", "year",
                   "difficulty", "tags", "review_status", "raw_ocr_text",
                   "content_hash", "diagram_url", "diagram_bbox",
                   "correct_option_label", "created_at")
ANSWER_WANTED = ("id", "source_id", "question_id", "question_number",
                 "answer_explanation", "correct_option_label",
                 "correct_option_text", "assets", "subject", "topic", "grade",
                 "corp", "year", "difficulty", "tags", "review_status",
                 "raw_ocr_text", "content_hash", "created_at")


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target-dsn", required=True,
                    help="target Postgres DSN (the fresh database)")
    ap.add_argument("--apply", action="store_true",
                    help="actually write; without it nothing is committed")
    ap.add_argument("--only", default="",
                    help="comma list: accounts,sources,courses,chapters,lessons,"
                         "questions,options,blocks,answers,reports")
    return ap.parse_args(argv)


def connect(cfg=None, dsn=None):
    """A connection to the target DSN, or to the configured (source) DB."""
    if dsn:
        return psycopg2.connect(dsn, connect_timeout=30)
    cfg = cfg or load_config()
    return psycopg2.connect(
        host=cfg.postgres_host, port=cfg.postgres_port, dbname=cfg.postgres_db,
        user=cfg.postgres_user, password=cfg.postgres_password,
        sslmode=cfg.postgres_sslmode or "prefer", connect_timeout=30)


def _table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,))
    return [r[0] for r in cur.fetchall()]


def _fetch(cur, table, wanted):
    """Rows of the `wanted` columns that really exist, oldest first."""
    have = set(_table_columns(cur, table))
    cols = [c for c in wanted if c in have]
    order = [c for c in ("created_at", "id") if c in cols]
    sql = "SELECT %s FROM public.%s%s" % (
        ", ".join(cols), table, " ORDER BY " + ", ".join(order) if order else "")
    cur.execute(sql)
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _json_value(value):
    """JSONB columns arrive as dicts; psycopg2 needs the Json adapter."""
    return Json(value) if isinstance(value, dict) else value


def _insert(cur, table, cols, rows):
    """INSERT ... ON CONFLICT DO NOTHING in chunks; returns the rows offered."""
    rows = [r for r in rows if r]
    if not rows:
        return 0
    sql = "INSERT INTO public.%s (%s) VALUES %%s ON CONFLICT DO NOTHING" % (
        table, ", ".join(cols))
    for start in range(0, len(rows), CHUNK):
        batch = [tuple(_json_value(r.get(c)) for c in cols)
                 for r in rows[start:start + CHUNK]]
        execute_values(cur, sql, batch)
    return len(rows)


def copy_table(scur, tcur, table):
    """Copy one table's shared columns verbatim; ids are preserved."""
    tgt_cols = _table_columns(tcur, table)
    cols = [c for c in _table_columns(scur, table) if c in tgt_cols]
    if not cols:
        return 0
    return _insert(tcur, table, cols, _fetch(scur, table, cols))


def copy_sources(scur, tcur):
    """Copy the source rows; the legacy model never stored the folder tree, so
    those rows take an empty folder id (NOT NULL) instead of failing."""
    tgt_cols = _table_columns(tcur, "sources")
    cols = [c for c in _table_columns(scur, "sources") if c in tgt_cols]
    rows = _fetch(scur, "sources", cols)
    if "folder_id" in tgt_cols and "folder_id" not in cols:
        cols = ["folder_id"] + cols
        for r in rows:
            r["folder_id"] = ""
    return _insert(tcur, "sources", cols, rows)


def _id_map(cur, table, key_col):
    """{key: id} for one table, so a later group can point at its rows."""
    cur.execute("SELECT %s, id FROM public.%s WHERE %s IS NOT NULL"
                % (key_col, table, key_col))
    return {k: v for k, v in cur.fetchall()}


def _has_lessons_schema(cur):
    cur.execute("SELECT to_regclass('public.courses')")
    row = cur.fetchone()
    return bool(row and row[0])


def _commit(conn, apply):
    if apply:
        conn.commit()


def run(src, tgt, apply=False, only=None):
    """Copy the source DB into the target, group by group.

    Returns the per-group row counts, or None when the target has no lessons
    model yet. Dry run (`apply=False`) rolls back at the end; `apply=True`
    commits per group, so a partial run is resumable (every insert is
    ON CONFLICT DO NOTHING).
    """
    groups = [g for g in GROUPS if not only or g in only]
    counts = {}
    with src.cursor() as scur, tgt.cursor() as tcur:
        if not _has_lessons_schema(tcur):
            print("refusing: the target has no lessons model - apply %s to it "
                  "first" % SCHEMA_FILE)
            return None

        if "accounts" in groups:
            counts["accounts"] = sum(
                copy_table(scur, tcur, t) for t in ACCOUNT_TABLES)
            _commit(tgt, apply)

        if "sources" in groups:
            counts["sources"] = copy_sources(scur, tcur)
            _commit(tgt, apply)
        sources = _fetch(scur, "sources", SOURCE_WANTED)
        source_urls = {s.get("id"): s.get("storage_url") for s in sources}

        questions = _fetch(scur, "questions", QUESTION_WANTED)
        answers = _fetch(scur, "answers", ANSWER_WANTED)
        duplicates = duplicate_legacy_ids(questions)
        sapienstech = _id_map(tcur, "questions_sapienstech", "supabase_id")

        courses = course_rows(questions)
        if "courses" in groups:
            counts["courses"] = _insert(tcur, "courses", COURSE_COLS, courses)
            _commit(tgt, apply)
        by_slug = _id_map(tcur, "courses", "slug")
        course_ids = {c["key"]: by_slug.get(c["slug"]) for c in courses}

        chapters = chapter_rows(questions, course_ids)
        if "chapters" in groups:
            counts["chapters"] = _insert(tcur, "chapters", CHAPTER_COLS,
                                         chapters)
            _commit(tgt, apply)
        by_chapter = _chapter_id_map(tcur)
        chapter_ids = {c["key"]: by_chapter.get((c["course_id"], c["slug"]))
                       for c in chapters}

        lessons = lesson_rows(sources, chapter_ids)
        if "lessons" in groups:
            counts["lessons"] = _insert(tcur, "lessons", LESSON_COLS, lessons)
            _commit(tgt, apply)
        by_lesson = _id_map(tcur, "lessons", "slug")
        lesson_ids = {l["key"]: by_lesson.get(l["slug"]) for l in lessons}

        qrows = question_rows(questions, source_urls, sapienstech, duplicates)
        if "questions" in groups:
            counts["questions"] = _insert(tcur, "questions", QUESTION_COLS,
                                          qrows)
            _commit(tgt, apply)
        question_ids = _id_map(tcur, "questions", "legacy_id")

        if "options" in groups:
            counts["options"] = _insert(
                tcur, "question_options", OPTION_COLS,
                option_row_list(questions, question_ids))
            counts["keys"] = _insert(
                tcur, "short_answer_keys", KEY_COLS,
                short_answer_key_rows(questions, answers, question_ids))
            _commit(tgt, apply)

        if "blocks" in groups:
            counts["blocks"] = _insert(
                tcur, "lesson_blocks", BLOCK_COLS,
                block_rows(questions, lesson_ids, question_ids))
            _commit(tgt, apply)

        if "answers" in groups:
            counts["answers"] = _insert(tcur, "answers", ANSWER_COLS,
                                        answer_rows(answers, question_ids))
            _commit(tgt, apply)

        if "reports" in groups:
            counts["reports"] = sum(copy_table(scur, tcur, t)
                                    for t in ("revision_reports",
                                              "revision_summary"))
            _commit(tgt, apply)

        if not apply:
            tgt.rollback()
    return counts


def _chapter_id_map(cur):
    """{(course_id, slug): id} - a chapter is unique inside its course."""
    cur.execute("SELECT course_id, slug, id FROM public.chapters")
    return {(course_id, slug): cid for course_id, slug, cid in cur.fetchall()}


def main(argv=None):
    args = parse_args(argv)
    src = connect()
    tgt = connect(dsn=args.target_dsn)
    if src.dsn == tgt.dsn:
        print("refusing: source and target are the same database")
        return 2
    only = [p.strip() for p in (args.only or "").split(",") if p.strip()]
    counts = run(src, tgt, apply=args.apply, only=only or None)
    if counts is None:
        return 1
    print("apply - committed per group" if args.apply else
          "dry run - nothing written (use --apply)")
    for name in list(GROUPS) + sorted(set(counts) - set(GROUPS)):
        if name in counts:
            print("  %-12s %d rows offered" % (name, counts[name]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
