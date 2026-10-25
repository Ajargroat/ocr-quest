"""All Postgres operations: upserts for sources/questions/answers
and the context fetch used by the answer branch.

The SQL targets the flat pipeline core - sources / questions / answers -
which is the schema the app writes:
`questions` (BIGSERIAL `id`) carries the OCR columns, the old `<sha1>-q<N>` id
lives in `legacy_id` (it is also the crop picture's bucket name,
`crops/<legacy_id>.jpg`), the OCR text in `prompt` and a question's picture in
the `media_images` array (Q12 interface). The Python key names the rest of the
pipeline speaks (`question_text`, `diagram_url`, `diagram_bbox`) are kept -
only the columns moved.
"""
import json
import posixpath
import re

import psycopg2

from .config import Config
from .questions import is_real_bbox
from .scanner import SourceItem

# The Supabase storage URL shape of a legacy diagram url: everything after the
# bucket name is the object path.
_STORAGE_OBJECT = re.compile(r"/storage/v1/object/(?:public/)?[^/]+/(.+)$")


def folder_id(rel_path):
    """Folder-tree id for a source: the directory a scanned file sits in.

    The scanner's source id is sha1(rel_path); the DB also points at the folder
    itself so a Database viewer can locate the file on disk (Q8).
    """
    folder = posixpath.dirname((rel_path or "").replace("\\", "/"))
    return folder or None


def storage_object_name(url):
    """The bucket object path inside a storage URL, or None when there is none.

    A legacy url ending in `<bucket>/unknown` names no usable object (the
    broken proxy url the migration repairs, C2b), so it counts as "no picture".
    """
    match = _STORAGE_OBJECT.search(url or "")
    if not match:
        return None
    name = match.group(1)
    if name == "unknown" or name.endswith("/unknown"):
        return None
    return name


def _bbox_value(raw):
    """A real [x0, y0, x1, y1] bbox or None - the all-zero placeholder Gemini
    emits for a figure-less question is not a bbox."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    return raw if is_real_bbox(raw) else None


def _json_list(raw):
    """A JSON array of dicts from a str/list value; [] for anything else."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    return [dict(entry) for entry in raw if isinstance(entry, dict)]


def question_type(raw_type, options_json):
    """The lessons question type: the row's own, else derived from its options.

    No options means a short answer; every OCR question carries at most one
    correct label, so `mcq_multi` would be invented data (C2b).
    """
    if raw_type:
        return raw_type
    options = options_json
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            options = []
    return "mcq_single" if options else "short_answer"


def media_from_diagram(diagram_url, diagram_bbox):
    """The legacy diagram spec as a media_images array (Q12 interface).

    Returns ``(entries, missing)``; ``missing`` is True when the legacy url
    names no usable object, i.e. the picture cannot be rendered at all.
    """
    name = storage_object_name(diagram_url)
    if not name:
        return [], True
    return [{"object": name, "bbox": _bbox_value(diagram_bbox), "page": 1}], False


def media_images_value(row):
    """The media_images JSON for one question row (the upsert side).

    A studio-importer row already carries the array; an OCR row still carries
    diagram_url/diagram_bbox, which is that same single entry.
    """
    media = row.get("media_images")
    if media:
        return media if isinstance(media, str) else json.dumps(
            media, ensure_ascii=False)
    if not row.get("diagram_url") and not row.get("diagram_bbox"):
        return "[]"
    entries, _ = media_from_diagram(row.get("diagram_url"),
                                    row.get("diagram_bbox"))
    return json.dumps(entries, ensure_ascii=False)


def media_images_update(row):
    """The media_images value for one revision update.

    The revision lane still speaks its own keys - diagram_bbox (JSON text or
    None), diagram_url, has_diagram - and pins them in tests. The array the row
    was read with is reused, so a question with several pictures is never
    narrowed to one entry: only the first entry is patched.
    """
    media = _json_list(row.get("media_images"))
    url = row.get("diagram_url")
    name = storage_object_name(url)
    if not media:
        if not row.get("has_diagram"):
            return "[]"
        entries, _ = media_from_diagram(url, row.get("diagram_bbox"))
        return json.dumps(entries, ensure_ascii=False)
    entry = dict(media[0])
    bbox = _bbox_value(row.get("diagram_bbox"))
    if bbox is not None:
        entry["bbox"] = bbox
    elif not row.get("has_diagram"):
        entry["bbox"] = None
    if name:
        entry["object"] = name
    media[0] = entry
    return json.dumps(media, ensure_ascii=False)


def set_media_bbox(media, index, bbox):
    """Rewrites the bbox of one entry in a media_images array.

    A new list is returned and the input is left untouched, so the caller keeps
    the array it just read.
    """
    out = _json_list(media)
    if 0 <= index < len(out):
        out[index]["bbox"] = bbox
    return out


class Database:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._conn = None

    # ── connection handling ────────────────────────────────────────
    def _connect(self):
        kwargs = dict(host=self.cfg.postgres_host,
                      port=self.cfg.postgres_port,
                      dbname=self.cfg.postgres_db,
                      user=self.cfg.postgres_user,
                      password=self.cfg.postgres_password,
                      connect_timeout=10)
        if self.cfg.postgres_sslmode:
            kwargs["sslmode"] = self.cfg.postgres_sslmode   # "" → libpq default
        self._conn = psycopg2.connect(**kwargs)
        self._conn.autocommit = True

    def _execute(self, sql, params=None, fetch=False):
        """Executes one statement; reconnects once if the connection died."""
        for attempt in (1, 2):
            try:
                if self._conn is None or self._conn.closed:
                    self._connect()
                with self._conn.cursor() as cur:
                    cur.execute(sql, params or ())
                    return cur.fetchall() if fetch else None
            except (psycopg2.OperationalError, psycopg2.InterfaceError):
                if attempt == 2:
                    raise
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def ping(self):
        self._execute("SELECT 1")

    def fetch_period_counts(self, date_from=None, date_to=None):
        """Pipeline period stats from data the DB already stores (Q4).

        No schema change: counts over created_at on questions/answers plus
        the live sources total. date_from/date_to are ISO date strings or
        None (all-time). Returns {questions, answers, errors, files}.
        "errors" counts rows flagged 'rejected' in review; "files" is the
        sources total (the run's file unit).
        """
        def _count(table, extra="", params=()):
            sql = f"SELECT COUNT(*) FROM public.{table} WHERE 1=1"
            args = []
            if date_from:
                sql += f" AND created_at::date >= %s::date"
                args.append(str(date_from))
            if date_to:
                sql += f" AND created_at::date <= %s::date"
                args.append(str(date_to))
            if extra:
                sql += " " + extra
                args.extend(params)          # extra's placeholders come last
            try:
                rows = self._execute(sql, tuple(args), fetch=True) or [(0,)]
            except psycopg2.errors.UndefinedColumn as exc:
                # a card that silently reads 0 after a schema change is a
                # silent lie; name the column instead of hiding the fault.
                print(f"[stats] period count on {table} failed: "
                      f"{' '.join(str(exc).split())[:200]}")
                return 0
            except Exception:
                return 0
            return int(rows[0][0] or 0)

        questions = _count("questions")
        answers = _count("answers")
        errors = _count("questions", "AND review_status = 'rejected'")
        files = _count("sources")
        # Question/Answer *file* counts come from sources.type ("سوال"/"پاسخ" —
        # scanner.TYPE_DIRS), so all five period cards are DB-sourced (D3).
        question_files = _count("sources", "AND type = %s", ("سوال",))
        answer_files = _count("sources", "AND type = %s", ("پاسخ",))
        return {"questions": questions, "answers": answers,
                "errors": errors, "files": files, "total": files,
                "question_files": question_files, "answer_files": answer_files,
                "succeeded": questions + answers}

    # ── sources ────────────────────────────────────────────────────
    def upsert_source(self, item: SourceItem, storage_url: str):
        sql = """
        INSERT INTO sources (id, folder_id, file_name, mime_type, file_size_bytes,
                             storage_url, subject, grade, topic, type)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (id) DO UPDATE SET
          folder_id       = EXCLUDED.folder_id,
          file_name       = EXCLUDED.file_name,
          mime_type       = EXCLUDED.mime_type,
          file_size_bytes = EXCLUDED.file_size_bytes,
          storage_url     = EXCLUDED.storage_url,
          subject         = EXCLUDED.subject,
          grade           = EXCLUDED.grade,
          topic           = EXCLUDED.topic,
          type            = EXCLUDED.type
        """
        # folder_id is NOT NULL: a row without a folder (the studio importer's
        # sources carry no rel_path) keeps an empty id rather than failing.
        self._execute(sql, (
            item.source_id, folder_id(item.rel_path) or "", item.file_name,
            item.mime_type, item.file_size, storage_url, item.subject,
            item.grade, item.topic, item.type,
        ))

    # ── questions ──────────────────────────────────────────────────
    def upsert_questions(self, rows):
        # rows keep the OCR key "question_text"; the lessons table names that
        # column "prompt". The old text id goes to legacy_id - `id` is now the
        # BIGSERIAL the DB owns, and the unique key is legacy_id instead.
        sql = """
        INSERT INTO questions (legacy_id, type, prompt, source_id, question_number,
                               options, media_images, subject, topic, grade, corp,
                               year, difficulty, tags, review_status, raw_ocr_text,
                               content_hash, split, duplicate, unreadable, created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                COALESCE(%s, now()))
        ON CONFLICT (legacy_id) DO UPDATE SET
          type            = EXCLUDED.type,
          prompt          = EXCLUDED.prompt,
          source_id       = EXCLUDED.source_id,
          question_number = EXCLUDED.question_number,
          options         = EXCLUDED.options,
          media_images    = EXCLUDED.media_images,
          subject         = EXCLUDED.subject,
          topic           = EXCLUDED.topic,
          grade           = EXCLUDED.grade,
          corp            = EXCLUDED.corp,
          year            = EXCLUDED.year,
          difficulty      = EXCLUDED.difficulty,
          tags            = EXCLUDED.tags,
          review_status   = CASE
            WHEN questions.review_status IN ('approved', 'rejected')
              THEN questions.review_status
            ELSE EXCLUDED.review_status
          END,
          raw_ocr_text    = EXCLUDED.raw_ocr_text,
          content_hash    = EXCLUDED.content_hash,
          split           = EXCLUDED.split,
          duplicate       = EXCLUDED.duplicate,
          unreadable      = EXCLUDED.unreadable
        """
        for r in rows:
            self._execute(sql, (
                r["id"], question_type(r.get("type"), r.get("options")),
                r["question_text"], r["source_id"], r["question_number"],
                r["options"] or "[]", media_images_value(r), r["subject"],
                r["topic"], r["grade"], r["corp"], r["year"], r["difficulty"],
                r["tags"], r["review_status"], r["raw_ocr_text"],
                r.get("content_hash"), r.get("split"),
                bool(r.get("duplicate")), bool(r.get("unreadable")),
                r.get("created_at"),
            ))

    # ── one bucket object's DB context (bucket drawer, item 12) ─────
    def fetch_source_detail(self, source_id):
        """The source row plus its linked questions and answers, for the
        Database tab's bucket drawer. `source_id` is the storage object name
        (storage.list_objects returns those as `name`)."""
        src = self._execute(
            "SELECT id, file_name, mime_type, file_size_bytes, storage_url, "
            "subject, grade, topic, type, folder_id FROM public.sources "
            "WHERE id = %s", (source_id,), fetch=True) or []
        source = None
        if src:
            r = src[0]
            source = {"id": r[0], "file_name": r[1], "mime_type": r[2],
                      "file_size_bytes": r[3], "storage_url": r[4],
                      "subject": r[5],
                      "grade": str(r[6]) if r[6] is not None else None,
                      "topic": r[7], "type": r[8], "folder_id": r[9]}
        qs = self._execute(
            "SELECT id, question_number, prompt AS question_text, review_status "
            "FROM public.questions WHERE source_id = %s "
            "ORDER BY question_number", (source_id,), fetch=True) or []
        ans = self._execute(
            "SELECT id, question_number, answer_explanation, review_status "
            "FROM public.answers WHERE source_id = %s "
            "ORDER BY question_number", (source_id,), fetch=True) or []
        return {
            "source": source,
            "questions": [{"id": r[0], "question_number": r[1],
                           "question_text": r[2], "review_status": r[3]}
                          for r in qs],
            "answers": [{"id": r[0], "question_number": r[1],
                         "answer_explanation": r[2], "review_status": r[3]}
                        for r in ans],
        }

    # ── context for the answer branch ('Fetch Questions' node) ─────
    def fetch_context_questions(self, subject, topic, grade):
        sql = """
        SELECT id, question_number, prompt AS question_text, options, corp, year
        FROM public.questions
        WHERE subject = %s AND topic = %s AND grade = %s
        """
        rows = self._execute(sql, (subject, topic, grade), fetch=True) or []
        return [
            {
                "id": r[0], "question_number": r[1], "question_text": r[2],
                "options": r[3], "corp": r[4], "year": r[5],
            }
            for r in rows
        ]

    # ── answers ────────────────────────────────────────────────────
    def upsert_answers(self, rows):
        # question_id is a bigint in the new model: the row's id (a legacy
        # "<sha1>-q<N>" text id, or the question's own id) is resolved inline
        # and the text value is kept in legacy_question_id so nothing is lost.
        sql = """
        INSERT INTO answers (id, legacy_id, source_id, question_id,
                             legacy_question_id, question_number,
                             answer_explanation, correct_option_label,
                             correct_option_text, assets, media_images, subject,
                             topic, corp, year, difficulty, tags, review_status,
                             raw_ocr_text, content_hash, grade, created_at)
        VALUES (%s,%s,%s,
                (SELECT id FROM public.questions
                  WHERE legacy_id = %s OR id::text = %s),
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                COALESCE(%s, now()))
        ON CONFLICT (id) DO UPDATE SET
          legacy_id            = EXCLUDED.legacy_id,
          source_id            = EXCLUDED.source_id,
          question_id          = COALESCE(EXCLUDED.question_id, answers.question_id),
          legacy_question_id   = EXCLUDED.legacy_question_id,
          question_number      = EXCLUDED.question_number,
          answer_explanation   = EXCLUDED.answer_explanation,
          correct_option_label = EXCLUDED.correct_option_label,
          correct_option_text  = EXCLUDED.correct_option_text,
          assets               = EXCLUDED.assets,
          media_images         = EXCLUDED.media_images,
          subject              = EXCLUDED.subject,
          topic                = EXCLUDED.topic,
          corp                 = EXCLUDED.corp,
          year                 = EXCLUDED.year,
          difficulty           = EXCLUDED.difficulty,
          tags                 = EXCLUDED.tags,
          review_status        = CASE
            WHEN answers.review_status IN ('approved', 'rejected')
              THEN answers.review_status
            ELSE EXCLUDED.review_status
          END,
          raw_ocr_text         = EXCLUDED.raw_ocr_text,
          content_hash         = EXCLUDED.content_hash,
          grade                = EXCLUDED.grade
        """
        for r in rows:
            self._execute(sql, (
                r["id"], r["id"], r["source_id"], r["question_id"],
                r["question_id"], r["question_id"], r["question_number"],
                r["answer_explanation"], r["correct_option_label"],
                r["correct_option_text"], r["assets"] or "[]",
                media_images_value(r), r["subject"], r["topic"], r["corp"],
                r["year"], r["difficulty"], r["tags"], r["review_status"],
                r["raw_ocr_text"], r["content_hash"], r["grade"],
                r.get("created_at"),
            ))

        # ── QA Studio (review tab) ─────────────────────────────────────
    _REVIEW_SELECT = """
        SELECT q.id, q.source_id, q.question_number, q.prompt AS question_text,
               q.options, q.type, q.media_images, q.split, q.duplicate,
               q.unreadable, q.subject, q.topic, q.grade::float AS grade,
               q.corp, q.year, q.difficulty, q.review_status, q.tags,
               a.id AS answer_id, a.answer_explanation, a.correct_option_label,
               a.correct_option_text, a.review_status AS answer_review_status
        FROM public.questions q
        LEFT JOIN public.answers a ON a.question_id = q.id
    """
    _REVIEW_COLS = [
        "id", "source_id", "question_number", "question_text", "options",
        "type", "media_images", "split", "duplicate", "unreadable",
        "subject", "topic", "grade", "corp", "year", "difficulty",
        "review_status", "tags",
        "answer_id", "answer_explanation", "correct_option_label",
        "correct_option_text", "answer_review_status",
    ]

    # loose answer-matching predicate: a row counts as "answered" when an
    # answer links by question_id or by (number, subject, topic, grade).
    _ANSWER_EXISTS = """EXISTS (
            SELECT 1 FROM public.answers a2 WHERE (
                a2.question_id = q.id
                OR (
                    a2.question_id IS NULL
                    AND a2.question_number = q.question_number
                    AND a2.subject = q.subject
                    AND a2.topic = q.topic
                    AND a2.grade = q.grade
                )
            )
        )"""

    # a question counts as "pictured" when its media_images array is non-empty
    _PICTURE_EXISTS = """(
            jsonb_typeof(q.media_images) = 'array'
            AND jsonb_array_length(q.media_images) > 0
        )"""

    @staticmethod
    def _multi(value):
        """Coerce a filter param (str, csv str or list) into a clean list."""
        if value is None:
            return None
        if isinstance(value, str):
            value = value.split(",")
        elif not isinstance(value, (list, tuple)):
            value = [value]
        vals = [str(v).strip() for v in value if v is not None]
        vals = [v for v in vals if v not in ("", "all")]
        return vals or None

    def fetch_review_rows(self, subject=None, status=None, grade=None,
                          mode="both", limit=120, topic=None, corp=None,
                          difficulty=None, date_from=None, date_to=None,
                          has_answer=None, has_picture=None):
        sql = """
            SELECT q.id, q.source_id, q.question_number, q.prompt AS question_text,
                   q.options, q.type, q.media_images, q.split, q.duplicate,
                   q.unreadable, q.subject, q.topic, q.grade,
                   q.corp, q.year, q.difficulty, q.review_status, q.tags,
                   q.raw_ocr_text,
                   to_char(q.created_at, 'YYYY-MM-DD"T"HH24:MI:SS') AS created_at,
                   a.id AS answer_id, a.answer_explanation,
                   a.correct_option_label, a.correct_option_text
            FROM public.questions q
            LEFT JOIN public.answers a ON (
                a.question_id = q.id
                OR (
                    a.question_id IS NULL
                    AND a.question_number = q.question_number
                    AND a.subject = q.subject
                    AND a.topic = q.topic
                    AND a.grade = q.grade
                )
            )
            WHERE 1=1
        """
        params = []
        # Each facet accepts a single value or a multi-select list.
        multi = {
            "q.subject": self._multi(subject),
            "q.review_status": self._multi(status),
            "q.grade::text": self._multi(grade),
            "q.topic": self._multi(topic),
            "q.corp": self._multi(corp),
            "q.difficulty": self._multi(difficulty),
        }
        for col, vals in multi.items():
            if vals:
                sql += f" AND {col} = ANY(%s)"
                params.append(vals)
        if date_from:
            sql += " AND q.created_at::date >= %s::date"
            params.append(str(date_from))
        if date_to:
            sql += " AND q.created_at::date <= %s::date"
            params.append(str(date_to))
        if has_answer in ("1", "0"):
            sql += (" AND " if has_answer == "1" else " AND NOT ") + self._ANSWER_EXISTS
        if has_picture in ("1", "0"):
            sql += (" AND " if has_picture == "1" else " AND NOT ") + self._PICTURE_EXISTS
        if mode == "answers":
            sql += " AND a.id IS NOT NULL"
        sql += " ORDER BY q.id DESC LIMIT %s"
        params.append(int(limit))

        try:
            rows = self._execute(sql, tuple(params), fetch=True) or []
        except Exception as e:
            print(f"\n[!!! DB ERROR !!!] {e}\n")
            raise

        cols = [
            "id", "source_id", "question_number", "question_text", "options",
            "type", "media_images", "split", "duplicate", "unreadable",
            "subject", "topic", "grade", "corp", "year", "difficulty",
            "review_status", "tags", "raw_ocr_text",
            "created_at",
            "answer_id", "answer_explanation", "correct_option_label", "correct_option_text"
        ]
        result = []
        seen_ids = set()
        for row in rows:
            record = dict(zip(cols, row))
            # Deduplicate: the OR join can produce multiple rows per question
            key = (record["id"], record.get("answer_id"))
            if key in seen_ids:
                continue
            seen_ids.add(key)
            if record.get("options") is None:
                record["options"] = "[]"
            if record.get("grade") is not None:
                record["grade"] = str(record["grade"])
            result.append(record)
        return result

    def fetch_review_meta(self):
        subjects = self._execute(
            "SELECT DISTINCT subject FROM public.questions "
            "WHERE subject IS NOT NULL ORDER BY subject", fetch=True) or []

        topics = self._execute(
            "SELECT DISTINCT topic FROM public.questions "
            "WHERE topic IS NOT NULL ORDER BY topic", fetch=True) or []

        corps = self._execute(
            "SELECT DISTINCT corp FROM public.questions "
            "WHERE corp IS NOT NULL AND corp <> '' ORDER BY corp", fetch=True) or []

        difficulties = self._execute(
            "SELECT DISTINCT difficulty FROM public.questions "
            "WHERE difficulty IS NOT NULL AND difficulty <> '' "
            "ORDER BY difficulty", fetch=True) or []

        # Cast grade to text to avoid float conversion errors
        grades = self._execute(
            "SELECT DISTINCT grade::text FROM public.questions "
            "WHERE grade IS NOT NULL ORDER BY grade::text", fetch=True) or []

        counts = self._execute(
            "SELECT review_status, COUNT(*) FROM public.questions "
            "GROUP BY review_status", fetch=True) or []

        count_map = {"pending": 0, "approved": 0, "rejected": 0}
        for status_val, n in counts:
            if status_val in count_map:
                count_map[status_val] = n
        count_map["total"] = sum(count_map.values())

        by_subject = self._execute(
            "SELECT subject, review_status, COUNT(*) FROM public.questions "
            "WHERE subject IS NOT NULL GROUP BY subject, review_status",
            fetch=True) or []
        subj_map = {}
        for subj, status_val, n in by_subject:
            entry = subj_map.setdefault(subj, {"subject": subj, "total": 0,
                                               "pending": 0, "approved": 0,
                                               "rejected": 0})
            entry["total"] += n
            if status_val in ("pending", "approved", "rejected"):
                entry[status_val] = n

        # Answer × picture cross-tab in one pass: gives the facet totals plus
        # the 4-cell matrix used by the stats panel.
        mx = self._execute(
            f"SELECT COUNT(*) FILTER (WHERE {self._ANSWER_EXISTS} "
            f"AND {self._PICTURE_EXISTS}), "
            f"COUNT(*) FILTER (WHERE {self._ANSWER_EXISTS} "
            f"AND NOT {self._PICTURE_EXISTS}), "
            f"COUNT(*) FILTER (WHERE NOT {self._ANSWER_EXISTS} "
            f"AND {self._PICTURE_EXISTS}), "
            f"COUNT(*) FILTER (WHERE NOT {self._ANSWER_EXISTS} "
            f"AND NOT {self._PICTURE_EXISTS}), COUNT(*) "
            "FROM public.questions q", fetch=True)
        (ap_both, ap_only_ans, ap_only_pic, ap_neither, q_total) = \
            mx[0] if mx else (0, 0, 0, 0, 0)
        with_answer = ap_both + ap_only_ans
        with_picture = ap_both + ap_only_pic

        return {
            "subjects": [r[0] for r in subjects],
            "grades": [r[0] for r in grades],
            "counts": count_map,
            "by_subject": sorted(subj_map.values(),
                                 key=lambda s: -s["total"]),
            "answers": {"with": with_answer, "without": q_total - with_answer},
            "pictures": {"with": with_picture,
                         "without": q_total - with_picture},
            "matrix": {"both": ap_both, "answer_only": ap_only_ans,
                       "picture_only": ap_only_pic, "neither": ap_neither},
            # option lists for the extra filters
            "chapters": [r[0] for r in topics],
            "corps": [r[0] for r in corps],
            "difficulties": [r[0] for r in difficulties],
        }

    def set_review_status(self, table, row_id, status):
        if table not in ("questions", "answers"):
            raise ValueError("Invalid table.")
        if status not in ("pending", "approved", "rejected"):
            raise ValueError("Invalid status.")
        self._execute(
            f"UPDATE public.{table} SET review_status = %s WHERE id = %s",
            (status, row_id),
        )

    # ── Revision agent ───────────────────────────────────────────
    # diagram_bbox/diagram_url stay in the payload: the revision lane speaks
    # those keys and tests pin them. They are now views of media_images, whose
    # raw text rides along so an update patches the array it read.
    _Q_REVISION_SELECT = """
        SELECT q.id, q.source_id, q.question_number, q.prompt AS question_text,
               q.options, q.difficulty, q.raw_ocr_text,
               COALESCE(q.media_images -> 0 -> 'bbox', 'null'::jsonb)::text
                   AS diagram_bbox,
               q.media_images -> 0 ->> 'object' AS diagram_url,
               q.media_images::text AS media_images, q.tags, q.corp, q.year,
               COALESCE(q.subject, s.subject) AS subject,
               COALESCE(q.topic, s.topic)     AS topic,
               COALESCE(q.grade, s.grade)     AS grade,
               s.storage_url AS source_storage_url,
               q.correct_option_label, q.correct_option_text
        FROM public.questions q
        LEFT JOIN public.sources s ON s.id = q.source_id
    """
    _Q_REVISION_COLS = [
        "id", "source_id", "question_number", "question_text", "options",
        "difficulty", "raw_ocr_text", "diagram_bbox", "diagram_url",
        "media_images", "tags", "corp", "year", "subject", "topic", "grade",
        "source_storage_url", "correct_option_label", "correct_option_text",
    ]

    _A_REVISION_SELECT = """
        SELECT id, source_id, question_id, question_number, answer_explanation,
               correct_option_label, correct_option_text, subject, topic, grade,
               corp, year, difficulty, tags, raw_ocr_text, revision_status,
               needs_review
        FROM public.answers
    """
    _A_REVISION_COLS = [
        "id", "source_id", "question_id", "question_number", "answer_explanation",
        "correct_option_label", "correct_option_text", "subject", "topic", "grade",
        "corp", "year", "difficulty", "tags", "raw_ocr_text", "revision_status",
        "needs_review",
    ]

    def fetch_pending_question_ids(self):
        """Snapshot of questions the scan still owns (pending/never revised)."""
        rows = self._execute(
            "SELECT id FROM public.questions "
            "WHERE revision_status IS NULL OR revision_status = 'pending' "
            "ORDER BY created_at ASC", fetch=True) or []
        return [r[0] for r in rows]

    def fetch_questions_by_ids(self, ids):
        if not ids:
            return []
        rows = self._execute(
            self._Q_REVISION_SELECT + " WHERE q.id = ANY(%s)",
            (list(ids),), fetch=True) or []
        return [dict(zip(self._Q_REVISION_COLS, r)) for r in rows]

    def fetch_questions_matching_numbers(self, numbers, exclude_ids=()):
        """Context questions for the answer-only sweep (any revision status)."""
        if not numbers:
            return []
        sql = self._Q_REVISION_SELECT + " WHERE q.question_number::text = ANY(%s)"
        params = [list(numbers)]
        if exclude_ids:
            sql += " AND NOT (q.id = ANY(%s))"
            params.append(list(exclude_ids))
        rows = self._execute(sql, tuple(params), fetch=True) or []
        return [dict(zip(self._Q_REVISION_COLS, r)) for r in rows]

    def fetch_answers_for_linking(self, numbers, question_ids):
        """Candidate answers for a question chunk: everything still unlinked
        whose question_number matches the chunk, plus answers already linked
        to a question in the chunk."""
        clauses, params = [], []
        if numbers:
            clauses.append("(question_id IS NULL AND question_number::text = ANY(%s))")
            params.append(list(numbers))
        if question_ids:
            clauses.append("question_id = ANY(%s)")
            params.append(list(question_ids))
        if not clauses:
            return []
        rows = self._execute(self._A_REVISION_SELECT + " WHERE " + " OR ".join(clauses),
                             tuple(params), fetch=True) or []
        return [dict(zip(self._A_REVISION_COLS, r)) for r in rows]

    def fetch_pending_answer_ids(self):
        rows = self._execute(
            "SELECT id FROM public.answers "
            "WHERE revision_status IS NULL OR revision_status = 'pending' "
            "   OR question_id IS NULL "
            "ORDER BY created_at ASC", fetch=True) or []
        return [r[0] for r in rows]

    def fetch_answers_by_ids(self, ids):
        if not ids:
            return []
        rows = self._execute(
            self._A_REVISION_SELECT + " WHERE id = ANY(%s)",
            (list(ids),), fetch=True) or []
        return [dict(zip(self._A_REVISION_COLS, r)) for r in rows]

    def apply_question_updates(self, updates):
        # status columns are frozen while a human holds the row ('approved')
        sql = """
            UPDATE public.questions SET
              question_number = %s, prompt = %s, options = %s,
              correct_option_label = %s, correct_option_text = %s,
              difficulty = %s, tags = %s, media_images = %s,
              subject = %s, topic = %s, grade = %s, corp = %s, year = %s,
              needs_review      = CASE WHEN revision_status = 'approved'
                                       THEN needs_review ELSE %s END,
              revision_status   = CASE WHEN revision_status = 'approved'
                                       THEN revision_status ELSE %s END,
              revision_notes    = CASE WHEN revision_status = 'approved'
                                       THEN revision_notes ELSE %s END,
              revised_at        = CASE WHEN revision_status = 'approved'
                                       THEN revised_at ELSE %s END
            WHERE id = %s
        """
        for u in updates:
            self._execute(sql, (
                u.get("question_number"), u.get("question_text"), u.get("options"),
                u.get("correct_option_label"), u.get("correct_option_text"),
                u.get("difficulty"), u.get("tags"), media_images_update(u),
                u.get("subject"), u.get("topic"), u.get("grade"),
                u.get("corp"), u.get("year"),
                bool(u.get("needs_review")), u.get("revision_status"),
                u.get("revision_notes"), u.get("revised_at"), u.get("id"),
            ))

    def apply_answer_updates(self, updates):
        sql = """
            UPDATE public.answers SET
              question_id = %s, question_number = %s, answer_explanation = %s,
              correct_option_label = %s, correct_option_text = %s,
              subject = %s, topic = %s, grade = %s, corp = %s, year = %s,
              difficulty = %s, tags = %s,
              needs_review      = CASE WHEN revision_status = 'approved'
                                       THEN needs_review ELSE %s END,
              revision_status   = CASE WHEN revision_status = 'approved'
                                       THEN revision_status ELSE %s END,
              revision_notes    = CASE WHEN revision_status = 'approved'
                                       THEN revision_notes ELSE %s END,
              revised_at        = CASE WHEN revision_status = 'approved'
                                       THEN revised_at ELSE %s END
            WHERE id = %s
        """
        for u in updates:
            self._execute(sql, (
                u.get("question_id"), u.get("question_number"),
                u.get("answer_explanation"),
                u.get("correct_option_label"), u.get("correct_option_text"),
                u.get("subject"), u.get("topic"), u.get("grade"),
                u.get("corp"), u.get("year"), u.get("difficulty"), u.get("tags"),
                bool(u.get("needs_review")), u.get("revision_status"),
                u.get("revision_notes"), u.get("revised_at"), u.get("id"),
            ))

    def insert_report_rows(self, rows):
        # revision_reports is retired with the revision engine (no UI, no
        # endpoints). The method stays so the dormant pipeline/revision/ code
        # still imports; calling it is a programming error, not a DB fault.
        raise RuntimeError(
            "revision_reports is retired (see pipeline/studio/"
            "retire_revision.sql); re-create the table to revive "
            "insert_report_rows")

    def fetch_revision_summary(self):
        rows = self._execute(
            "SELECT COALESCE(revision_status, 'pending'), COUNT(*) "
            "FROM public.questions GROUP BY 1", fetch=True) or []
        counts = {r[0]: r[1] for r in rows}
        nr = self._execute(
            "SELECT COUNT(*) FROM public.questions WHERE needs_review = TRUE",
            fetch=True)
        arows = self._execute(
            "SELECT COALESCE(revision_status, 'pending'), COUNT(*) "
            "FROM public.answers GROUP BY 1", fetch=True) or []
        anr = self._execute(
            "SELECT COUNT(*) FROM public.answers WHERE needs_review = TRUE",
            fetch=True)
        return {"counts": counts,
                "needs_review": nr[0][0] if nr else 0,
                "answer_counts": {r[0]: r[1] for r in arows},
                "answer_needs_review": anr[0][0] if anr else 0}

    def fetch_revision_items(self, view="flagged", limit=200, entity="question"):
        if entity == "answer":
            where = {
                "flagged":  "WHERE a.needs_review = TRUE OR a.revision_status = 'needs_human'",
                "revised":  "WHERE a.revision_status = 'revised'",
                "approved": "WHERE a.revision_status = 'approved'",
                "all":      "WHERE a.revision_status IS NOT NULL",
            }.get(view, "WHERE a.needs_review = TRUE OR a.revision_status = 'needs_human'")
            sql = f"""
                SELECT a.id, a.question_id, a.question_number, a.answer_explanation,
                       a.correct_option_label, a.correct_option_text, a.subject,
                       a.topic, a.grade::text AS grade, a.difficulty, a.corp, a.year,
                       a.revision_status, a.needs_review, a.revision_notes,
                       a.revised_at, q.prompt AS question_text,
                       q.options AS options,
                       q.correct_option_label AS q_correct_option_label
                FROM public.answers a
                LEFT JOIN public.questions q ON q.id = a.question_id
                {where}
                ORDER BY a.revised_at DESC NULLS LAST
                LIMIT %s
            """
            cols = [
                "id", "question_id", "question_number", "answer_explanation",
                "correct_option_label", "correct_option_text", "subject", "topic",
                "grade", "difficulty", "corp", "year", "revision_status",
                "needs_review", "revision_notes", "revised_at", "question_text",
                "options", "q_correct_option_label",
            ]
        else:
            where = {
                "flagged":  "WHERE q.needs_review = TRUE OR q.revision_status = 'needs_human'",
                "revised":  "WHERE q.revision_status = 'revised'",
                "approved": "WHERE q.revision_status = 'approved'",
                "all":      "WHERE q.revision_status IS NOT NULL",
            }.get(view, "WHERE q.needs_review = TRUE OR q.revision_status = 'needs_human'")
            sql = f"""
                SELECT q.id, q.question_number, q.prompt AS question_text,
                       q.options, q.subject,
                       q.topic, q.grade::text AS grade, q.difficulty, q.corp, q.year,
                       q.revision_status, q.needs_review, q.revision_notes,
                       q.revised_at, q.correct_option_label,
                       la.id AS answer_id, la.answer_explanation,
                       la.correct_option_label AS answer_correct_option_label,
                       la.correct_option_text  AS answer_correct_option_text,
                       la.revision_status AS answer_revision_status,
                       la.needs_review    AS answer_needs_review
                FROM public.questions q
                LEFT JOIN LATERAL (
                    SELECT a.id, a.answer_explanation, a.correct_option_label,
                           a.correct_option_text, a.revision_status, a.needs_review
                    FROM public.answers a
                    WHERE a.question_id = q.id
                    ORDER BY a.id LIMIT 1
                ) la ON TRUE
                {where}
                ORDER BY q.revised_at DESC NULLS LAST
                LIMIT %s
            """
            cols = [
                "id", "question_number", "question_text", "options", "subject", "topic",
                "grade", "difficulty", "corp", "year", "revision_status", "needs_review",
                "revision_notes", "revised_at", "correct_option_label",
                "answer_id", "answer_explanation", "answer_correct_option_label",
                "answer_correct_option_text", "answer_revision_status",
                "answer_needs_review",
            ]
        rows = self._execute(sql, (int(limit),), fetch=True) or []
        return [dict(zip(cols, row)) for row in rows]

    def fetch_revision_reports(self, entity_ids, entity="question"):
        # revision_reports is retired (see insert_report_rows).
        if not entity_ids:
            return {}
        raise RuntimeError(
            "revision_reports is retired (see pipeline/studio/"
            "retire_revision.sql); re-create the table to revive "
            "fetch_revision_reports")

    def resolve_revision(self, row_id, action, entity="question"):
        table = {"question": "questions", "answer": "answers"}.get(entity)
        if table is None:
            raise ValueError("Invalid entity.")
        if action == "approve":
            self._execute(
                f"UPDATE public.{table} SET revision_status = 'approved', "
                "needs_review = FALSE WHERE id = %s", (row_id,))
        elif action == "reopen":
            self._execute(
                f"UPDATE public.{table} SET revision_status = 'pending', "
                "needs_review = FALSE WHERE id = %s", (row_id,))
        else:
            raise ValueError("Invalid action.")

    def update_media_bbox(self, question_id, index, bbox):
        """Saves a manually corrected bbox [ymin, xmin, ymax, xmax] (0-1000)
        onto one entry of the question's media_images array."""
        rows = self._execute(
            "SELECT media_images FROM public.questions WHERE id = %s",
            (question_id,), fetch=True) or []
        media = set_media_bbox(rows[0][0] if rows else [], int(index), bbox)
        self._execute(
            "UPDATE public.questions SET media_images = %s WHERE id = %s",
            (json.dumps(media, ensure_ascii=False), question_id))

    def fetch_media_row(self, question_id):
        """One question's picture context for the QBank crop editor.

        Read-only: the stored media array, the legacy id (a crop's object name)
        and the page the picture was cut from - `sources.storage_url`, which is
        still how a legacy row points at its page JPEG in the bucket.
        """
        rows = self._execute(
            "SELECT q.id, q.legacy_id, q.source_id, q.media_images, "
            "       s.storage_url, s.file_name "
            "  FROM public.questions q "
            "  LEFT JOIN public.sources s ON s.id = q.source_id "
            " WHERE q.id = %s", (question_id,), fetch=True) or []
        if not rows:
            return None
        qid, legacy_id, source_id, media, source_url, file_name = rows[0]
        return {"id": str(qid), "legacy_id": legacy_id,
                "source_id": source_id, "media": _json_list(media),
                "source_url": source_url, "source_file": file_name}

    def clear_zero_bboxes(self, limit=100):
        """Backfill: drop the all-zero placeholder bbox ([0,0,0,0]) Gemini used
        to emit for figure-less questions from the most recent `limit` rows
        that still carry it, so those pictures keep their whole-source entry.
        Re-runnable - once clean it updates 0 rows."""
        sql = """
        WITH z AS (
            SELECT q.id FROM public.questions q
            WHERE EXISTS (
                SELECT 1 FROM jsonb_array_elements(q.media_images) e
                WHERE replace((e -> 'bbox')::text, ' ', '') = '[0,0,0,0]'
            )
            ORDER BY q.created_at DESC
            LIMIT %s
        )
        UPDATE public.questions q
           SET media_images = COALESCE((
                   SELECT jsonb_agg(
                              CASE WHEN replace((e.value -> 'bbox')::text, ' ', '')
                                        IN ('null', '[0,0,0,0]')
                                   THEN jsonb_set(e.value, '{bbox}', 'null'::jsonb)
                                   ELSE e.value END
                              ORDER BY e.ordinality)
                   FROM jsonb_array_elements(q.media_images)
                        WITH ORDINALITY AS e(value, ordinality)
               ), '[]'::jsonb)
          FROM z
         WHERE q.id = z.id
        RETURNING q.id
        """
        rows = self._execute(sql, (int(limit),), fetch=True) or []
        return len(rows)
