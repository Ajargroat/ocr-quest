"""Weekly snapshots of the five pipeline tables, stored inside the project.

Each run writes ``backups/<UTC-stamp>/<table>.json`` + ``<table>.sql`` plus a
``manifest.json`` for the Data Console tab, and rotates away all but the most
recent KEEP sets. A single-row ``row_graveyard.jsonl`` collects every row the
Data Console deletes, so even manual deletions stay restorable.

Scheduling is intentionally dumb: the FastAPI lifespan asks ``due()`` every few
hours and runs ``run_backup()`` when the last weekly manifest is older than
BACKUP_INTERVAL_DAYS (default 7). That way restarts never spam extra backups
and the cadence survives server reloads.
"""
import json
import os
import re
import shutil
from datetime import datetime, timezone

import psycopg2

BACKUP_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backups")
GRAVEYARD_FILE = os.path.join(BACKUP_ROOT, "row_graveyard.jsonl")

# The five tables the Data Console owns. revision_summary has no PK and uses
# last_at instead of created_at — ordering keys are kept here for both jobs.
TABLES = ("sources", "questions", "answers", "revision_reports", "revision_summary")
TS_COL = {t: "created_at" for t in TABLES}
TS_COL["revision_summary"] = "last_at"

SET_NAME_RE = re.compile(r"^\d{8}-\d{6}Z$")
TABLE_RE = re.compile(r"^[a-z_]+$")


def _env_int(var, default):
    try:
        return max(1, int(os.getenv(var, str(default))))
    except ValueError:
        return default


def interval_days():
    return _env_int("BACKUP_INTERVAL_DAYS", 7)


def keep_sets():
    return _env_int("BACKUP_KEEP", 12)


def _connect(cfg):
    kwargs = dict(host=cfg.postgres_host, port=cfg.postgres_port,
                  dbname=cfg.postgres_db, user=cfg.postgres_user,
                  password=cfg.postgres_password, connect_timeout=10)
    if cfg.postgres_sslmode:
        kwargs["sslmode"] = cfg.postgres_sslmode   # "" → libpq default
    conn = psycopg2.connect(**kwargs)
    conn.autocommit = True
    return conn


def _jsonable(v):
    from decimal import Decimal
    if isinstance(v, (datetime,)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return str(v)


def _table_columns(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,),
    )
    return [r[0] for r in cur.fetchall()]


def run_backup(cfg, trigger="weekly"):
    """Dump every table to a fresh dated set; returns the manifest dict."""
    started = datetime.now(timezone.utc)
    name = started.strftime("%Y%m%d-%H%M%S") + "Z"
    out_dir = os.path.join(BACKUP_ROOT, name)
    os.makedirs(out_dir, exist_ok=True)

    manifest = {
        "name": name,
        "created_utc": started.isoformat(),
        "trigger": trigger,
        "interval_days": interval_days(),
        "tables": {},
        "total_rows": 0,
        "total_bytes": 0,
    }
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW server_version")
            vrow = cur.fetchone()
            manifest["server_version"] = vrow[0] if vrow else "unknown"
            for table in TABLES:
                cols = _table_columns(cur, table)
                ts = TS_COL.get(table)
                order = f" ORDER BY {ts} DESC NULLS LAST" if ts in cols else ""
                cur.execute(f"SELECT {', '.join(cols)} FROM public.{table}{order}")
                rows = cur.fetchall()

                # JSON snapshot — the machine-restorable, diff-friendly copy.
                payload = [dict(zip(cols, (_jsonable(v) for v in row))) for row in rows]
                json_path = os.path.join(out_dir, table + ".json")
                with open(json_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False, indent=1)

                # SQL dump — mogrify keeps quoting/escaping exactly right, so
                # the file can be replayed with psql against any Postgres.
                col_list = ", ".join('"%s"' % c for c in cols)
                template = "INSERT INTO public.%s (%s) VALUES (%s);" % (
                    table, col_list, ", ".join(["%s"] * len(cols)))
                sql_path = os.path.join(out_dir, table + ".sql")
                with open(sql_path, "w", encoding="utf-8") as fh:
                    fh.write("-- %s · %s · %d rows · trigger=%s\n" % (
                        name, table, len(rows), trigger))
                    for row in rows:
                        fh.write(cur.mogrify(template, row).decode("utf-8") + "\n")

                manifest["tables"][table] = {
                    "rows": len(rows),
                    "sql_bytes": os.path.getsize(sql_path),
                    "json_bytes": os.path.getsize(json_path),
                }
                manifest["total_rows"] += len(rows)
                manifest["total_bytes"] += manifest["tables"][table]["sql_bytes"] \
                    + manifest["tables"][table]["json_bytes"]
    finally:
        conn.close()

    manifest["duration_s"] = round(
        (datetime.now(timezone.utc) - started).total_seconds(), 2)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=1)

    rotate()
    return manifest


def list_sets():
    """All backup sets, newest first, from their manifest.json files."""
    if not os.path.isdir(BACKUP_ROOT):
        return []
    sets = []
    for entry in sorted(os.listdir(BACKUP_ROOT), reverse=True):
        if not SET_NAME_RE.match(entry):
            continue
        mpath = os.path.join(BACKUP_ROOT, entry, "manifest.json")
        try:
            with open(mpath, encoding="utf-8") as fh:
                manifest = json.load(fh)
            manifest["dir"] = entry
            sets.append(manifest)
        except (OSError, ValueError):
            continue
    return sets


def latest_set():
    sets = list_sets()
    return sets[0] if sets else None


def due():
    """True when no backup exists yet or the newest one is older than the
    weekly interval."""
    last = latest_set()
    if last is None:
        return True
    try:
        created = datetime.fromisoformat(last["created_utc"])
    except (KeyError, ValueError):
        return True
    age_days = (datetime.now(timezone.utc) - created).total_seconds() / 86400
    return age_days >= interval_days()


def rotate():
    """Delete all but the newest KEEP backup sets."""
    sets = list_sets()
    for stale in sets[keep_sets():]:
        path = os.path.join(BACKUP_ROOT, stale["dir"])
        shutil.rmtree(path, ignore_errors=True)
    return len(list_sets())


def backup_file_path(set_name, table, fmt):
    """Resolve a downloadable dump file, refusing anything outside backups/."""
    if not SET_NAME_RE.match(set_name or "") or not TABLE_RE.match(table or "") \
            or fmt not in ("json", "sql"):
        raise ValueError("Unknown backup file.")
    path = os.path.normpath(os.path.join(BACKUP_ROOT, set_name, "%s.%s" % (table, fmt)))
    if not path.startswith(os.path.normpath(BACKUP_ROOT) + os.sep) or not os.path.isfile(path):
        raise ValueError("Unknown backup file.")
    return path


def stash_graveyard(table, row):
    """Append one deleted row to the graveyard so deletions are restorable."""
    os.makedirs(BACKUP_ROOT, exist_ok=True)
    entry = {"deleted_utc": datetime.now(timezone.utc).isoformat(),
             "table": table, "row": row}
    with open(GRAVEYARD_FILE, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False, default=_jsonable) + "\n")
