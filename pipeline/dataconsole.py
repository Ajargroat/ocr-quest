"""Data Console backend — schema-aware browsing, safe row actions and a
guarded SQL editor over the five pipeline tables (see pipeline.backups.TABLES).

Safety model
------------
* Table names, column names and the ORDER BY key all come from
  information_schema and are matched against a fixed allowlist before being
  interpolated, so no raw user string ever becomes an identifier.
* The SQL editor accepts exactly ONE statement whose first verb is
  SELECT / WITH / INSERT / UPDATE / DELETE and which references only the five
  known tables (plus read-only information_schema). UPDATE/DELETE additionally
  require a WHERE clause. Comments and string literals are stripped before the
  guard inspects the text, so keywords hidden in literals cannot smuggle
  anything through — and anything resembling DDL/DCL/transaction control or an
  RPC/file-access function (drop, truncate, alter, grant, copy, pg_*, setval,
  dblink, dollar-quotes, stacked statements…) is refused outright.
* Editor statements run on their own connection under a statement_timeout, so
  a runaway query can neither wedge the dashboard nor poison the pipeline's
  shared connection.
* Row deletions snapshot the row into backups/row_graveyard.jsonl first, which
  makes every console deletion restorable.
"""
import re
import time
import uuid
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal

from .backups import TABLES, TS_COL, stash_graveyard

MAX_LIMIT = 500
MAX_RESULT_ROWS = 1000
STATEMENT_TIMEOUT = "20s"

# Identifier shape we will ever accept from the schema (defence in depth).
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")

# Verbs/objects we never let through the editor, matched on the stripped text.
_DANGEROUS = re.compile(
    r"\b(drop|truncate|alter|create|replace|grant|revoke|copy|vacuum|cluster|"
    r"reindex|refresh|reassign|lock|notify|listen|unlisten|execute|exec|prepare|"
    r"deallocate|call|do|reset|begin|start|commit|rollback|abort|savepoint|"
    r"release|checkpoint|pg_[a-z_]+|lo_import|lo_export|dblink|setval|nextval|"
    r"currval|query_to_xml|xpath|current_setting)\b", re.I)
_ALLOWED_FIRST = re.compile(r"^(select|with|insert|update|delete)\b", re.I)
_TABLE_REF = re.compile(
    r"\b(?:from|join|update|insert\s+into|delete\s+from)\s+"
    r"([a-z_][\w]*(?:\.[a-z_][\w]*)?)", re.I)
_CTE_NAME = re.compile(r"([a-z_]\w*)\s+as\s*\(", re.I)


def _scrub(text):
    """Single-pass removal of comments and string literals.

    Returns a copy safe for keyword/semicolon/table analysis: literals become
    the bare token LIT (no quotes, no semicolons, no keywords), and an
    unterminated literal or comment raises GuardError instead of falling back
    to regex substitution (order-insensitive: -- inside '…' and '…' inside
    -- both handled)."""
    out = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            j = i + 1
            closed = False
            while j < n:
                if text[j] == "'":
                    if j + 1 < n and text[j + 1] == "'":
                        j += 2
                        continue
                    closed = True
                    break
                j += 1
            if not closed:
                raise GuardError("Unterminated string literal.")
            out.append(" LIT ")
            i = j + 1
        elif c == "-" and text[i:i + 2] == "--":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            if j < 0:
                raise GuardError("Unterminated block comment.")
            out.append(" ")
            i = j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)
# Columns whose contents are huge OCR dumps: scanning them for a text search
# is slow and never useful, so the global search skips them.
_SKIP_SEARCH = {"raw_ocr_text"}

# Audit trail for the console's own writes — tiny, append-only, project-local.


class GuardError(ValueError):
    """Raised for anything the safety guard refuses (mapped to HTTP 400)."""


def _jsonable(v):
    if isinstance(v, (datetime, date, dtime)):
        return v.isoformat()
    if isinstance(v, timedelta):
        return str(v)
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, list):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    return v


def quote_ident(name):
    """Wrap a schema-validated identifier. Non-matching names never get here,
    but refuse loudly rather than build injectable SQL."""
    if not _IDENT.match(name or ""):
        raise GuardError(f"Refusing unexpected identifier '{name}'.")
    return '"' + name + '"'


def guard_sql(raw):
    """Validate one editor statement.

    Returns ``(kind, sql)`` where kind is ``'read'`` (returns rows) or
    ``'write'`` (returns an affected-row count). Raises GuardError otherwise.
    """
    text = (raw or "").strip()
    if not text:
        raise GuardError("Empty statement.")
    if "$" in text:
        raise GuardError("Dollar-quoted strings are not allowed in the editor.")

    # Inspect a de-commented, de-literalised copy so that keywords inside
    # strings can neither bypass nor trip the checks, while we still execute
    # the original text.
    probe = _scrub(text)

    body = probe.strip().rstrip(";").strip()
    if not body:
        raise GuardError("Empty statement.")
    if ";" in body:
        raise GuardError("Only one statement per run — remove the extra ';'.")

    m = _ALLOWED_FIRST.match(body)
    if not m:
        raise GuardError("Only SELECT, WITH, INSERT, UPDATE and DELETE "
                         "statements are allowed here.")
    verb = m.group(1).lower()

    if _DANGEROUS.search(body):
        raise GuardError("This statement uses a blocked keyword or function.")
    if verb in ("select", "with") and re.search(r"\binto\b", body, re.I):
        raise GuardError("SELECT … INTO creates tables and is blocked.")
    if verb in ("update", "delete") and not re.search(r"\bwhere\b", body, re.I):
        raise GuardError("UPDATE/DELETE without a WHERE clause is blocked — "
                         "that would rewrite or remove every row.")

    cte_names = {n.lower() for n in _CTE_NAME.findall(body)}
    for ref in _TABLE_REF.findall(body):
        parts = [p.lower() for p in ref.split(".")]
        schema, table = (parts[0], parts[1]) if len(parts) == 2 else ("public", parts[0])
        if table in cte_names:
            continue
        if schema == "information_schema":
            continue
        if schema != "public" or table not in TABLES:
            raise GuardError(
                f"Only the five pipeline tables are reachable here "
                f"(blocked: {ref}).")
    return ("read" if verb in ("select", "with") else "write"), text


class DataConsole:
    """Read + safely-bounded write access for the Database tab."""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db
        self._schema = {}          # table -> {"cols": [...], "pk": [...]}
        self._editor_conn = None

    # ── connection for editor statements ───────────────────────────
    def _editor_execute(self, sql_text, params=None, fetch=False):
        """Runs a guarded statement on a dedicated, timeout-bounded
        connection. Autocommit stays on: the guard forbids transaction
        control, and each accepted statement is meant to land immediately."""
        import psycopg2

        conn = self._editor_conn
        if conn is None or conn.closed:
            kwargs = dict(host=self.cfg.postgres_host,
                          port=self.cfg.postgres_port,
                          dbname=self.cfg.postgres_db,
                          user=self.cfg.postgres_user,
                          password=self.cfg.postgres_password,
                          connect_timeout=10,
                          application_name="konkour-data-console")
            if self.cfg.postgres_sslmode:
                kwargs["sslmode"] = self.cfg.postgres_sslmode  # "" → default
            conn = psycopg2.connect(**kwargs)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET statement_timeout = %s", (STATEMENT_TIMEOUT,))
            self._editor_conn = conn
        try:
            with conn.cursor() as cur:
                cur.execute(sql_text, params or ())
                cols = [d[0] for d in cur.description] if cur.description else None
                rows = cur.fetchall() if (fetch and cols) else None
                return rows, cols, cur.rowcount
        except psycopg2.Error:
            # A guard-approved statement can still fail (bad column, FK
            # violation). Roll the transaction back so the shared connection
            # is left clean for the next run, then let the caller report it.
            try:
                conn.rollback()
            except Exception:
                pass
            raise

    # ── schema cache ───────────────────────────────────────────────
    def schema(self, table):
        if table not in TABLES:
            raise GuardError(f"Unknown table '{table}'.")
        if table not in self._schema:
            cols = self.db._execute(
                "SELECT column_name, data_type, is_nullable, "
                "       COALESCE(column_default, '') "
                "FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = %s "
                "ORDER BY ordinal_position", (table,), fetch=True) or []
            pk = self.db._execute(
                "SELECT kcu.column_name FROM information_schema.table_constraints tc "
                "JOIN information_schema.key_column_usage kcu "
                "  ON tc.constraint_name = kcu.constraint_name "
                " AND tc.table_schema = kcu.table_schema "
                "WHERE tc.table_schema = 'public' AND tc.table_name = %s "
                "AND tc.constraint_type = 'PRIMARY KEY'", (table,), fetch=True) or []
            self._schema[table] = {
                "cols": [{"name": c[0], "type": c[1], "null": c[2] == "YES",
                          "default": c[3] or None} for c in cols],
                "pk": [p[0] for p in pk],
            }
        return self._schema[table]

    def _column_names(self, table):
        return [c["name"] for c in self.schema(table)["cols"]]

    def _pk(self, table):
        meta = self.schema(table)
        if len(meta["pk"]) != 1:
            raise GuardError(
                f"{table} has no single primary key, so row actions are "
                f"disabled there — use the SQL editor instead.")
        return meta["pk"][0]

    # ── reads ──────────────────────────────────────────────────────
    def overview(self):
        """Row counts, column lists and newest-entry time for all five tables,
        which is what the tab's cards and table switcher render."""
        tables = []
        for table in TABLES:
            meta = self.schema(table)
            colnames = [c["name"] for c in meta["cols"]]
            ts = TS_COL.get(table) if TS_COL.get(table) in colnames else None
            counts = self.db._execute(
                "SELECT COUNT(*), max(%s) FROM public.%s"
                % (quote_ident(ts) if ts else "NULL", table), fetch=True)
            latest = counts[0][1] if counts else None
            tables.append({
                "name": table,
                "rows": counts[0][0] if counts else 0,
                "columns": meta["cols"],
                "pk": meta["pk"],
                "ts_column": ts,
                "latest": _jsonable(latest),
                "row_actions": len(meta["pk"]) == 1,
            })
        return {"tables": tables, "allowed_tables": list(TABLES),
                "statement_timeout": STATEMENT_TIMEOUT}

    def rows(self, table, limit=100, offset=0, q="", sort="", order="desc"):
        """One page of a table. `q` is a whole-row text search; `sort` must be
        a real column of that table, so it can never become injected SQL."""
        meta = self.schema(table)
        colnames = [c["name"] for c in meta["cols"]]
        limit = max(1, min(int(limit or 100), MAX_LIMIT))
        offset = max(0, int(offset or 0))
        ts = TS_COL.get(table) if TS_COL.get(table) in colnames else None

        params = []
        where = ""
        if q:
            search_cols = [c for c in colnames if c not in _SKIP_SEARCH]
            where = "WHERE (" + " OR ".join(
                "%s::text ILIKE %%s" % quote_ident(c) for c in search_cols) + ")"
            params = ["%" + q + "%"] * len(search_cols)

        sort_col = sort if sort in colnames else ts
        direction = "ASC" if str(order).lower() == "asc" else "DESC"
        order_sql = ("ORDER BY %s %s NULLS LAST"
                     % (quote_ident(sort_col), direction)) if sort_col else ""

        select_list = ", ".join(quote_ident(c) for c in colnames)
        page = self.db._execute(
            "SELECT %s, COUNT(*) OVER() AS __total__ FROM public.%s "
            "%s %s LIMIT %%s OFFSET %%s"
            % (select_list, table, where, order_sql),
            params + [limit, offset], fetch=True) or []

        total = int(page[0][-1]) if page else 0
        out = [dict(zip(colnames, (_jsonable(v) for v in row[:len(colnames)])))
               for row in page]
        return {"table": table, "rows": out, "total": total,
                "columns": meta["cols"], "pk": meta["pk"],
                "limit": limit, "offset": offset, "searchable": bool(q)}

    def row_by_id(self, table, row_id):
        pk_col = self._pk(table)
        colnames = self._column_names(table)
        select_list = ", ".join(quote_ident(c) for c in colnames)
        found = self.db._execute(
            "SELECT %s FROM public.%s WHERE %s = %%s LIMIT 1"
            % (select_list, table, quote_ident(pk_col)), (row_id,), fetch=True)
        if not found:
            raise GuardError(f"No {table} row with {pk_col} = {row_id}.")
        return dict(zip(colnames, (_jsonable(v) for v in found[0])))

    # ── row actions ────────────────────────────────────────────────
    def set_cell(self, table, row_id, column, value):
        """Single-cell edit — the narrowest possible write the console offers.
        Idempotent: no-op when the value already matches."""
        colnames = self._column_names(table)
        if column not in colnames:
            raise GuardError(f"Unknown column '{column}' for {table}.")
        pk_col = self._pk(table)
        if column == pk_col:
            raise GuardError("The primary key cannot be edited from the console.")
        before = self.row_by_id(table, row_id)
        if before.get(column) == value:
            return {"ok": True, "changed": False}
        self.db._execute(
            "UPDATE public.%s SET %s = %%s WHERE %s = %%s"
            % (table, quote_ident(column), quote_ident(pk_col)),
            (value, row_id))
        return {"ok": True, "changed": True,
                "field": column, "before": _jsonable(before.get(column))}

    def insert_row(self, table, values):
        colnames = self._column_names(table)
        clean = {k: v for k, v in (values or {}).items()
                 if k in colnames and k not in _SKIP_SEARCH
                 and k not in ("created_at", "last_at") and v != ""}
        if not clean:
            raise GuardError("Nothing to insert — fill in at least one column.")
        cols = list(clean)
        select_list = ", ".join(quote_ident(c) for c in cols)
        self.db._execute(
            "INSERT INTO public.%s (%s) VALUES (%s)"
            % (table, select_list, ", ".join(["%s"] * len(cols))),
            [clean[c] for c in cols])
        return {"ok": True, "columns": cols}

    def delete_row(self, table, row_id):
        """Delete ONE row, after its full contents land in the graveyard.
        Whole-table deletes are impossible here by construction."""
        pk_col = self._pk(table)
        snapshot = self.row_by_id(table, row_id)
        stash_graveyard(table, snapshot)
        self.db._execute(
            "DELETE FROM public.%s WHERE %s = %%s"
            % (table, quote_ident(pk_col)), (row_id,))
        return {"ok": True, "table": table, "id": row_id}

    # ── SQL editor ─────────────────────────────────────────────────
    def run_sql(self, raw):
        kind, sql = guard_sql(raw)
        started = time.time()
        try:
            fetched, columns, rowcount = self._editor_execute(
                sql, fetch=(kind == "read"))
        except Exception as exc:
            return {"ok": False, "kind": kind,
                    "error": " ".join(str(exc).split())[:600]}
        elapsed = round((time.time() - started) * 1000, 1)
        if kind == "read":
            columns = columns or []
            rows = [dict(zip(columns, [_jsonable(v) for v in r]))
                    for r in (fetched or [])[:MAX_RESULT_ROWS]]
            return {"ok": True, "kind": "read", "columns": columns,
                    "rows": rows, "elapsed_ms": elapsed,
                    "truncated": bool(fetched and len(fetched) >= MAX_RESULT_ROWS)}
        return {"ok": True, "kind": "write", "affected": rowcount,
                "elapsed_ms": elapsed}
