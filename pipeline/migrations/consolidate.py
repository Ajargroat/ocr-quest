"""One-off: rebuild the flat pipeline core from the lessons-model database.

The app's schema is ``sources`` / ``questions`` / ``answers`` /
``revision_reports`` / ``revision_summary`` (see pipeline/db.py and
pipeline/backups.TABLES). A database built from the root PDF's lessons model
carries nine extra tables that duplicate those rows 1:1 plus a Sapienstech
mirror nothing reads. This runner proves the duplication, then drops the
lessons scaffolding - all in ONE transaction, with the drop at the end.

Usage (dry run is the default; nothing is written without --apply)::

    .venv/Scripts/python -m pipeline.migrations.consolidate
    .venv/Scripts/python -m pipeline.migrations.consolidate --apply

Safety rules this runner enforces:

* A complete backup set must exist under ``backups/`` (manifest + every table
  file + the source-tree snapshot) before the connection opens. Without it the
  run refuses.
* The whole migration is one transaction with autocommit OFF; any failed check
  rolls everything back, so the lessons tables cannot be dropped half-way.
* The drops are the last statements. Before them, every parity check must
  pass, and every check is read-only.

Nothing here is scheduled or imported by the app; it is a one-off operator
command like the retired ``migrate_lessons`` module.
"""
import argparse
import json
import os
import sys
import time

import psycopg2

BACKUP_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "backups")

SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "consolidate.sql")
RETIRE_SQL_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "retire_revision.sql")

# What the flat pipeline core owns after the run. The revision engine is
# retired (no UI, no endpoints), so its two tables are not part of the core;
# the questions/answers revision_* columns stay dormant for a revival.
CORE_TABLES = ("sources", "questions", "answers")
# The lessons scaffolding this run removes (children before parents).
LESSON_TABLES = ("lesson_progress", "attempts", "lesson_blocks",
                 "short_answer_keys", "question_options", "media_assets",
                 "lessons", "chapters", "courses")
MIRROR_TABLES = ("test_questions", "questions_sapienstech", "students",
                 "tests", "users", "domains", "tenants")
# The retired revision engine's storage.
REVISION_TABLES = ("revision_reports", "revision_summary")


def out(line=""):
    """cp1256-safe console line (the app's console is not UTF-8)."""
    print(str(line).encode("ascii", "replace").decode("ascii"))


# -- backup gate ---------------------------------------------------------

def backup_sets(root=None):
    """Every complete backup set, newest name first."""
    root = root or BACKUP_ROOT
    sets = []
    if not os.path.isdir(root):
        return sets
    for name in sorted(os.listdir(root), reverse=True):
        path = os.path.join(root, name)
        manifest_path = os.path.join(path, "manifest.json")
        if not os.path.isfile(manifest_path):
            continue          # not a set the backup job would list
        try:
            with open(manifest_path, encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            continue
        sets.append({"dir": name, "path": path, "manifest": manifest})
    return sets


def complete_backup(root=None):
    """The newest backup set that is provably complete, or None.

    Complete means: a manifest, and for every table it lists both the JSON and
    the SQL dump on disk. The source-tree snapshot is required when the
    manifest claims one.
    """
    for item in backup_sets(root):
        tables = item["manifest"].get("tables") or {}
        if not tables:
            continue
        missing = [t for t in tables
                   if not (os.path.isfile(os.path.join(item["path"], t + ".json"))
                           and os.path.isfile(os.path.join(item["path"], t + ".sql")))]
        if missing:
            continue
        sources = item["manifest"].get("sources") or {}
        if sources.get("files"):
            snap = os.path.join(item["path"], sources.get("dir") or "sources")
            if not os.path.isdir(snap):
                continue
        return item
    return None


# -- measurement ---------------------------------------------------------

def table_exists(cur, table):
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", ("public." + table,))
    return bool(cur.fetchone()[0])


def count(cur, table):
    """Row count of an existing table; 0 when it is absent."""
    if not table_exists(cur, table):
        return 0
    cur.execute('SELECT count(*) FROM public."%s"' % table)
    return int(cur.fetchone()[0])


def measure(cur):
    """The pre-flight snapshot: row counts plus the duplication proofs."""
    facts = {"tables": {}}
    for table in CORE_TABLES + LESSON_TABLES + MIRROR_TABLES:
        facts["tables"][table] = count(cur, table)
    tables = facts["tables"]
    facts["lessons_present"] = table_exists(cur, "lessons")
    facts["mirror_present"] = table_exists(cur, "tenants")

    checks = []
    # The lessons tree duplicates the source tree (one lesson per source, one
    # block per question). Prove it holds no payload of its own before it goes:
    # its course/chapter slugs are the source's subject/topic/grade.
    if facts["lessons_present"]:
        cur.execute("""SELECT count(*) FROM public.lessons l
                         LEFT JOIN public.chapters c ON c.id = l.chapter_id
                         LEFT JOIN public.courses co ON co.id = c.course_id
                        WHERE c.id IS NULL OR co.id IS NULL""")
        checks.append(("lessons have a chapter + course", cur.fetchone()[0] == 0))
        # the tree is reconstructible from sources (subject/topic/grade): a
        # course slug is "<folder subject code>-<grade>" (e.g. 'zist-10')
        cur.execute("""SELECT count(*) FROM public.courses co
                        WHERE NOT EXISTS (
                          SELECT 1 FROM public.sources s
                           WHERE s.subject = split_part(co.slug, '-', 1)
                             AND (s.grade IS NULL OR co.grade IS NULL
                                  OR s.grade = co.grade))""")
        checks.append(("every course maps to a subject in sources",
                       cur.fetchone()[0] == 0))
    if table_exists(cur, "lesson_blocks"):
        cur.execute("""SELECT count(*) FROM public.lesson_blocks lb
                         LEFT JOIN public.questions q ON q.id = lb.question_id
                        WHERE lb.question_id IS NOT NULL AND q.id IS NULL""")
        checks.append(("lesson_blocks all point at a question",
                       cur.fetchone()[0] == 0))
        cur.execute("""SELECT count(*) FROM public.lesson_blocks
                        WHERE type <> 'question'""")
        checks.append(("lesson_blocks are question blocks only",
                       cur.fetchone()[0] == 0))
        checks.append(("one block per question",
                       tables["lesson_blocks"] == tables["questions"]))
    if table_exists(cur, "question_options"):
        # options are complete in questions.options - the only copy the app reads
        cur.execute("""SELECT count(*) FROM public.questions q
                        WHERE (q.options IS NULL OR q.options IN ('[]', ''))
                          AND EXISTS (SELECT 1 FROM public.question_options o
                                       WHERE o.question_id = q.id)""")
        checks.append(("no question carries options only in question_options",
                       cur.fetchone()[0] == 0))
        # nothing marks an option correct, so the flag cannot be lost
        cur.execute("""SELECT count(*) FROM public.question_options
                        WHERE is_correct""")
        correct_rows = cur.fetchone()[0]
        checks.append(("question_options.is_correct carries no data",
                       correct_rows == 0))
        cur.execute("""SELECT count(*) FROM public.questions
                        WHERE correct_option_label IS NOT NULL
                          AND correct_option_label <> ''""")
        facts["correct_labels"] = cur.fetchone()[0]
        checks.append(("no question has a correct_option_label",
                       facts["correct_labels"] == 0))
    if table_exists(cur, "short_answer_keys"):
        # A key duplicates the paired answer: same text as the answer row that
        # already lives in `answers.answer_explanation`, which the
        # importer writes. Only a key WITHOUT that copy would be lost by the
        # drop - that is the case this refuses.
        cur.execute("""SELECT count(*) FROM public.short_answer_keys k
                        WHERE NOT EXISTS (
                          SELECT 1 FROM public.answers a
                           WHERE a.question_id = k.question_id
                             AND a.answer_explanation = k.model_answer)""")
        orphans = int(cur.fetchone()[0])
        facts["keys_without_an_answer_copy"] = orphans
        checks.append(("every short_answer_key is kept in answers.answer_explanation",
                       orphans == 0))
    if table_exists(cur, "media_assets"):
        checks.append(("media_assets is empty", tables["media_assets"] == 0))
    if table_exists(cur, "attempts"):
        checks.append(("attempts is empty", tables["attempts"] == 0))
    if table_exists(cur, "lesson_progress"):
        checks.append(("lesson_progress is empty", tables["lesson_progress"] == 0))

    # pictures are row-independent: media_images holds the object name
    cur.execute("""SELECT count(*) FROM public.questions
                    WHERE jsonb_typeof(media_images) = 'array'
                      AND jsonb_array_length(media_images) > 0""")
    facts["questions_with_pictures"] = int(cur.fetchone()[0])
    cur.execute("""SELECT count(*) FROM public.questions
                    WHERE jsonb_typeof(media_images) = 'array'
                      AND jsonb_array_length(media_images) > 0
                      AND coalesce(media_images -> 0 ->> 'object', '') = ''""")
    checks.append(("every picture names a bucket object", cur.fetchone()[0] == 0))
    cur.execute("SELECT count(*) FROM public.questions WHERE legacy_id IS NULL")
    checks.append(("every question keeps its legacy_id (the crop name)",
                   cur.fetchone()[0] == 0))

    # unpaired answers still match by (number, subject, topic, grade)
    cur.execute("""SELECT count(*) FROM public.answers
                    WHERE question_id IS NULL""")
    facts["unpaired_answers"] = int(cur.fetchone()[0])
    cur.execute("""SELECT count(*) FROM public.answers a
                    WHERE a.question_id IS NULL
                      AND (a.subject IS NULL OR a.topic IS NULL OR a.grade IS NULL)
                      AND EXISTS (SELECT 1 FROM public.answers b
                                   WHERE b.question_id IS NOT NULL
                                     AND b.question_number = a.question_number)""")
    checks.append(("every answer has an identity to match on",
                   cur.fetchone()[0] == 0))

    facts["checks"] = checks
    return facts


def report(facts):
    out("  tables:")
    for table, rows in facts["tables"].items():
        if table in REVISION_TABLES:
            mark = "retire"
        elif table in LESSON_TABLES + MIRROR_TABLES:
            mark = "drop  "
        else:
            mark = "keep  "
        if rows or mark != "drop  ":
            out("    %s %-20s %8d" % (mark, table, rows))
    out("  questions with pictures: %d (crop names come from legacy_id)"
        % facts["questions_with_pictures"])
    out("  unpaired answers: %d" % facts["unpaired_answers"])
    out("  checks:")
    for label, ok in facts["checks"]:
        out("    [%s] %s" % ("ok" if ok else "FAIL", label))
    return all(ok for _, ok in facts["checks"])


def parity_payload(facts):
    """The counts consolidate.sql re-checks inside the transaction."""
    t = facts["tables"]
    payload = {
        "sources": t["sources"],
        "questions": t["questions"],
        "answers": t["answers"],
        "lessons": t["lessons"],
        "lesson_blocks": t["lesson_blocks"],
    }
    return {k: v for k, v in payload.items() if v}


def run_sql(conn, facts):
    """Apply consolidate.sql in the caller's transaction (never commits).

    The script's one placeholder is a psycopg2 ``%s`` (pyformat), so the
    params must be passed positionally - psycopg2 rejects a dict here.
    """
    with open(SQL_PATH, encoding="utf-8") as fh:
        script = fh.read()
    with conn.cursor() as cur:
        cur.execute(script, (json.dumps(parity_payload(facts)),))


def retire_revision(conn):
    """Apply retire_revision.sql in the caller's transaction (never commits).

    The script takes no parameters, so no ``%`` substitution happens at all -
    its NOTICE text may use a bare ``%`` safely.
    """
    with open(RETIRE_SQL_PATH, encoding="utf-8") as fh:
        script = fh.read()
    with conn.cursor() as cur:
        cur.execute(script)


def verify(conn):
    """Post-apply read: only the three core tables remain, with their rows."""
    with conn.cursor() as cur:
        cur.execute("""SELECT table_name FROM information_schema.tables
                        WHERE table_schema = 'public' ORDER BY 1""")
        left = [r[0] for r in cur.fetchall()]
        rows = {t: count(cur, t) for t in CORE_TABLES}
    return left, rows


# -- entry point ---------------------------------------------------------

def connect(cfg, attempts=6, delay=4):
    """Open the migration connection, retrying the pooler's transient drops.

    The Supabase pooler intermittently answers with "SSL error: unexpected eof
    while reading"; that is a refused handshake, so nothing ran and a retry is
    safe. autocommit stays OFF - the whole change is one transaction.
    """
    last = None
    for attempt in range(attempts):
        try:
            conn = psycopg2.connect(
                host=cfg.postgres_host, port=cfg.postgres_port,
                dbname=cfg.postgres_db, user=cfg.postgres_user,
                password=cfg.postgres_password,
                sslmode=cfg.postgres_sslmode or "prefer", connect_timeout=20)
            conn.autocommit = False
            return conn
        except psycopg2.OperationalError as exc:
            last = exc
            out("  connect attempt %d/%d failed: %s"
                % (attempt + 1, attempts, " ".join(str(exc).split())[:120]))
            time.sleep(delay)
    raise SystemExit("could not connect after %d attempts: %s" % (attempts, last))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true",
                        help="commit the change (default: dry run)")
    parser.add_argument("--retire-revision", action="store_true",
                        help="instead of the lessons merge, only drop the "
                             "retired revision tables")
    parser.add_argument("--backup-root", default=None,
                        help="override where backup sets are looked for")
    args = parser.parse_args(argv)

    out("retire the revision tables" if args.retire_revision
        else "flat pipeline core consolidation")
    backup = complete_backup(args.backup_root)
    if backup is None:
        out("REFUSED: no complete backup set found under backups/")
        out("         take one first (Database tab -> Backups -> Run),")
        out("         then run this again.")
        return 2
    out("undo point: backups/%s (%d tables, %d rows)"
        % (backup["dir"], len(backup["manifest"].get("tables") or {}),
           backup["manifest"].get("total_rows") or 0))

    from pipeline.config import load_config
    cfg = load_config()
    conn = connect(cfg)
    try:
        if args.retire_revision:
            return _retire(conn, args)
        with conn.cursor() as cur:
            facts = measure(cur)
        out("before:")
        ok = report(facts)
        if not ok:
            out("REFUSED: a duplication check failed; nothing was changed.")
            conn.rollback()
            return 3
        if not args.apply:
            conn.rollback()
            out("dry run - nothing changed. re-run with --apply to commit.")
            return 0

        out("applying (one transaction) ...")
        try:
            run_sql(conn, facts)
            retire_revision(conn)
            left, rows = verify(conn)
        except Exception as exc:                  # any failed check rolls back
            conn.rollback()
            out("ROLLED BACK: %s" % " ".join(str(exc).split())[:400])
            return 4
        conn.commit()
        out("committed.")
        out("tables now: %s" % ", ".join(left))
        for table in CORE_TABLES:
            out("  %-18s %8d" % (table, rows[table]))
        extra = [t for t in left if t not in CORE_TABLES]
        if extra:
            out("note: already present: %s" % ", ".join(extra))
        return 0
    finally:
        conn.close()


def _retire(conn, args):
    """The --retire-revision path: report the two tables, then drop them."""
    with conn.cursor() as cur:
        before = {t: count(cur, t) for t in REVISION_TABLES}
    for table in REVISION_TABLES:
        out("  retire %-20s %8d rows" % (table, before[table]))
    if not any(before.values()):
        out("note: both tables are empty or absent; the drop is still safe.")
    if not args.apply:
        conn.rollback()
        out("dry run - nothing changed. re-run with --apply to commit.")
        return 0
    out("applying (one transaction) ...")
    try:
        retire_revision(conn)
        left, rows = verify(conn)
    except Exception as exc:
        conn.rollback()
        out("ROLLED BACK: %s" % " ".join(str(exc).split())[:400])
        return 4
    conn.commit()
    out("committed.")
    out("tables now: %s" % ", ".join(left))
    for table in CORE_TABLES:
        out("  %-18s %8d" % (table, rows[table]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
