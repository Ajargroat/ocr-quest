"""Entry point: FastAPI server + WebSocket live events + dashboard UI."""
import logging
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
import asyncio
import os
import time
from contextlib import asynccontextmanager

import requests as http_requests
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
from pipeline import envfile
from pipeline import vision
from pipeline import backups
from pipeline import storage
from pipeline import gemini as gemini_mod
from pipeline.dataconsole import DataConsole, GuardError
from pipeline.revision import runner as revision_runner
from pipeline.config import load_config, ENV_PATH, seed_proxy_profile, save_proxy_store
from pipeline.db import Database
from pipeline.gemini_router import router, usage_chart
from pipeline.gemini_router import mask as mask_key
from pipeline.runner import Hub, start_run
from dotenv import load_dotenv

cfg = load_config()
db = Database(cfg)
console = DataConsole(cfg, db)
hub = Hub()


def _router_usage_push(key, model, usage):
    """Router callback: 0/20 bars update on EVERY call (criterion 10)."""
    try:
        hub.emit({"type": "usage", "key": mask_key(key),
                  "model": model, "usage": usage})
    except Exception:
        pass


router.on_usage = _router_usage_push


async def _backup_loop():
    """Weekly database snapshots (backups/ inside the project). Checked every
    6 hours against the newest manifest, so restarts never double-fire and a
    missed week self-heals on the next check."""
    while True:
        try:
            if await asyncio.to_thread(backups.due):
                manifest = await asyncio.to_thread(backups.run_backup, cfg, "daily")
                print(f"[backups] snapshot {manifest['name']} - "
                      f"{manifest['total_rows']} rows written")
        except Exception as exc:
            print(f"[backups] snapshot failed: {exc}")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_app):
    hub.attach_loop(asyncio.get_running_loop())
    # Q5 one-time upgrade: a legacy single proxy URL becomes one profile
    # before the old lane is dropped — never overwrites an existing store.
    seeded = seed_proxy_profile()
    if seeded:
        save_proxy_store(seeded, "")
    backup_task = asyncio.create_task(_backup_loop())
    if getattr(cfg, "converter_enabled", True):
        from pipeline import converter
        await asyncio.to_thread(converter.start, cfg)
        print("[converter] embedded watcher started")
    else:
        print("[converter] disabled via CONVERTER_ENABLED=0")
    try:
        yield
    finally:
        backup_task.cancel()
        try:
            from pipeline import converter
            converter.stop()
        except Exception:
            pass
    # Shutdown: wake every blocked WebSocket handler so uvicorn doesn't hang
    # waiting for connections to finish (which required a second Ctrl+C).
    for queue in list(hub.clients):
        queue.put_nowait(None)


app = FastAPI(title="Konkour OCR Pipeline", lifespan=lifespan)
UI_DIR = os.path.join(os.path.dirname(__file__), "ui")
app.mount("/static", StaticFiles(directory=UI_DIR), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(os.path.join(os.path.dirname(__file__), "ui", "index.html"))


@app.get("/api/state")
async def state():
    return JSONResponse(hub.snapshot())


class RunOptions(BaseModel):
    """Body of POST /api/run — page-level, never persisted (PREFERENCE §3).

    `mode` is "one" (one-by-one, today's serial loop) or "parallel".
    `enabled_keys` is the Credentials tab's session on/off selection, sent as
    pool INDICES (row i ↔ gemini_key_pool[i], the same contract /api/credentials
    uses at :717); None means every key is on."""
    mode: str = "one"
    enabled_keys: list[int] | None = None


@app.post("/api/run")
async def run(opts: RunOptions | None = None):
    mode = "parallel" if (opts and opts.mode == "parallel") else "serial"
    enabled = None
    if mode == "parallel" and opts and opts.enabled_keys:
        pool = list(cfg.gemini_key_pool)
        enabled = [pool[i] for i in opts.enabled_keys
                   if isinstance(i, int) and 0 <= i < len(pool)]
        if not enabled:
            enabled = None          # nothing usable selected -> every key
    started = start_run(hub, cfg, db, mode=mode, enabled_keys=enabled)
    return JSONResponse({"started": started}, status_code=200 if started else 409)

@app.post("/api/stop")
async def stop():
    """Ask the running pipeline scan to halt after its current file."""
    stopping = hub.request_stop()
    return JSONResponse({"stopping": stopping},
                        status_code=200 if stopping else 409)


# ════════════════ UPLOADS · in-app PDF queue (workstream B) ════════════════

@app.post("/api/uploads")
async def upload_pdf(file: UploadFile = File(...), dest: str = Form("")):
    """Enqueue one PDF into the watched tree (criterion 2).

    The file streams to disk chunked (never whole-file in RAM); the
    embedded converter watcher extracts its pages and the normal
    scanner/runner pipeline processes them — sequential by construction.
    """
    from pipeline import uploads
    try:
        item = await asyncio.to_thread(
            uploads.enqueue, cfg, file.file,
            file.filename or "upload.pdf", dest or "")
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except OSError as exc:
        return JSONResponse({"ok": False, "error": f"Cannot store upload: {exc}"},
                            status_code=500)
    hub.emit({"type": "upload", "id": item["id"], "status": "queued",
              "filename": item["filename"], "dest": item["dest_rel"]})
    return JSONResponse({"ok": True, **item})


@app.get("/api/uploads")
async def upload_list():
    from pipeline import uploads
    return JSONResponse({"items": uploads.list_items()})


@app.post("/api/uploads/{uid}/cancel")
async def upload_cancel(uid: str):
    """Remove a pending upload / cancel a queued-or-failed one (Q9)."""
    from pipeline import uploads
    item = uploads.cancel(uid)
    if item is None:
        return JSONResponse({"ok": False, "error": "Unknown, in-flight or finished upload."},
                            status_code=404)
    hub.emit({"type": "upload", "id": uid, "status": "cancelled"})
    return JSONResponse({"ok": True, **item})


# ════════════════ STATS · pipeline period panel (criterion 11) ════════════════

_STATS_CACHE = {"at": 0.0, "period": "", "payload": None}


@app.get("/api/stats")
async def period_stats(period: str = "all"):
    """Period stats from data the DB already stores — no schema change (Q4).

    Preset chips only (Q11): 24h | 7d | 30d | all. Cached 60s per period.
    """
    from datetime import date, timedelta
    period = (period or "all").strip()
    if period not in ("24h", "7d", "30d", "all"):
        return JSONResponse({"ok": False, "error": "period must be 24h, 7d, 30d or all."},
                            status_code=400)
    now = time.time()
    if _STATS_CACHE["payload"] is not None and _STATS_CACHE["period"] == period \
            and now - _STATS_CACHE["at"] < 60:
        return JSONResponse(_STATS_CACHE["payload"])
    date_to = date.today().isoformat()
    days = {"24h": 1, "7d": 7, "30d": 30}.get(period)
    date_from = (date.today() - timedelta(days=days - 1)).isoformat() if days else None
    try:
        counts = await asyncio.to_thread(db.fetch_period_counts, date_from, date_to)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": f"Stats unavailable: {exc}"},
                            status_code=502)
    payload = {"ok": True, "period": period, **counts}
    _STATS_CACHE.update({"at": now, "period": period, "payload": payload})
    return JSONResponse(payload)


@app.get("/api/usage")
async def usage_report(period: str = "all", group: str = "model", y: str = "calls"):
    """Per-call usage history for the credentials reports (Q4/Q11)."""
    report = router.usage_report(period if period in ("24h", "7d", "30d", "all") else "all")
    report["ok"] = True
    report["chart"] = usage_chart(report.get("calls") or [], group=group, y=y)
    return JSONResponse(report)

class StatusUpdate(BaseModel):
    table: str
    id: str
    status: str


@app.get("/api/review/meta")
async def review_meta():
    """Subjects, grades and status counters for the QA Studio filters."""
    return JSONResponse(db.fetch_review_meta())


@app.get("/api/review/rows")
async def review_rows(subject: str = "", status: str = "", grade: str = "",
                      mode: str = "both", limit: int = 120, topic: str = "",
                      corp: str = "", difficulty: str = "",
                      date_from: str = "", date_to: str = "",
                      has_answer: str = "", has_picture: str = ""):
    try:
        rows = db.fetch_review_rows(
            subject or None, status or None, grade or None, mode, limit,
            topic=topic or None, corp=corp or None,
            difficulty=difficulty or None,
            date_from=date_from or None, date_to=date_to or None,
            has_answer=has_answer or None, has_picture=has_picture or None,
        )
        return JSONResponse({"rows": rows})
    except Exception as e:
        import traceback
        traceback.print_exc()  # Prints to PowerShell
        return JSONResponse({"rows": [], "error": str(e)}, status_code=500)


@app.post("/api/review/status")
async def review_status(body: StatusUpdate):
    """Approve / reject / reset one question or answer row."""
    try:
        db.set_review_status(body.table, body.id, body.status)
        return JSONResponse({"ok": True})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    queue = asyncio.Queue()
    hub.clients.add(queue)
    try:
        await websocket.send_json({"type": "snapshot", **hub.snapshot()})
        while True:
            event = await queue.get()
            if event is None:          # shutdown sentinel from lifespan
                break
            await websocket.send_json(event)
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        hub.clients.discard(queue)
        try:
            await websocket.close()
        except (Exception, asyncio.CancelledError):
            pass

@app.get("/api/proxy/image/{source_id}")
async def proxy_image(source_id: str):
    """Proxies Supabase storage images through the backend using the service key."""
    url = f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}/{source_id}"
    headers = {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
    }
    try:
        resp = http_requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return JSONResponse({"error": "Image not found"}, status_code=404)
        content_type = resp.headers.get("content-type", "image/jpeg")
        from fastapi.responses import Response
        return Response(content=resp.content, media_type=content_type)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/db/bucket")
async def db_bucket(prefix: str = "", limit: int = 100, offset: int = 0):
    """Read-only list of the configured Supabase bucket's objects (item 3.4).

    The Database tab's Bucket panel browses these; each row's `name` is the
    same value /api/proxy/image/{name} fetches. No mutation, no bucket picker.
    """
    try:
        rows = await asyncio.to_thread(storage.list_objects, cfg, prefix,
                                       limit, offset)
        return JSONResponse({"ok": True, "objects": rows,
                             "bucket": cfg.supabase_bucket})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


@app.get("/api/db/source")
async def db_source(source_id: str):
    """One bucket object's DB context for the bucket drawer (item 12):
    the source row + its linked questions and answers, tagged by type.
    Read-only; the image itself comes from /api/proxy/image/{source_id}."""
    try:
        return JSONResponse(await asyncio.to_thread(
            db.fetch_source_detail, source_id))
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


class RevisionResolve(BaseModel):
    id: str
    action: str
    entity: str = "question"  # 'question' | 'answer'


@app.post("/api/revision/run")
async def revision_run():
    started = revision_runner.start(hub, cfg, db)
    return JSONResponse({"started": started}, status_code=200 if started else 409)

@app.post("/api/revision/stop")
async def revision_stop():
    """Halt the revision scan after the current chunk (AI-skipped rows stay
    pending and are retried by the next run)."""
    stopping = revision_runner.request_stop()
    return JSONResponse({"stopping": stopping},
                        status_code=200 if stopping else 409)

@app.get("/api/revision/state")
async def revision_state():
    st = revision_runner.get_state()
    st["logs"] = hub.rev_tail(160)
    return JSONResponse(st)


@app.get("/api/revision/summary")
async def revision_summary():
    return JSONResponse(db.fetch_revision_summary())


@app.get("/api/revision/items")
async def revision_items(view: str = "flagged", limit: int = 200, entity: str = "question"):
    items = db.fetch_revision_items(view, limit, entity)
    reports = db.fetch_revision_reports([it["id"] for it in items], entity)
    for it in items:
        it["reports"] = reports.get(it["id"], [])
        if isinstance(it.get("revision_notes"), str):
            try:
                it["revision_notes"] = json.loads(it["revision_notes"])
            except Exception:
                pass
    return JSONResponse({"items": items})


@app.post("/api/revision/resolve")
async def revision_resolve(body: RevisionResolve):
    try:
        db.resolve_revision(body.id, body.action, body.entity)
        return JSONResponse({"ok": True})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

class BboxUpdate(BaseModel):
    id: str
    bbox: list  # [ymin, xmin, ymax, xmax], each 0..1000


class CellUpdate(BaseModel):
    table: str
    id: str
    column: str
    value: str | None = None


class RowInsert(BaseModel):
    table: str
    values: dict


class RowDelete(BaseModel):
    table: str
    id: str


class SqlRun(BaseModel):
    sql: str


def _console_guard(exc: Exception):
    """Map DataConsole failures to clean HTTP errors."""
    if isinstance(exc, GuardError):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    import traceback
    traceback.print_exc()
    return JSONResponse({"ok": False, "error": " ".join(str(exc).split())[:600]},
                        status_code=500)


@app.get("/api/db/overview")
async def db_overview():
    try:
        return JSONResponse(await asyncio.to_thread(console.overview))
    except Exception as exc:
        return _console_guard(exc)


@app.get("/api/db/rows")
async def db_rows(table: str, limit: int = 100, offset: int = 0,
                  q: str = "", sort: str = "", order: str = "desc"):
    try:
        return JSONResponse(await asyncio.to_thread(
            console.rows, table, limit, offset, q, sort, order))
    except Exception as exc:
        return _console_guard(exc)


@app.post("/api/db/cell")
async def db_cell(body: CellUpdate):
    try:
        return JSONResponse(await asyncio.to_thread(
            console.set_cell, body.table, body.id, body.column, body.value))
    except Exception as exc:
        return _console_guard(exc)


@app.post("/api/db/insert")
async def db_insert(body: RowInsert):
    try:
        return JSONResponse(await asyncio.to_thread(
            console.insert_row, body.table, body.values))
    except Exception as exc:
        return _console_guard(exc)


@app.post("/api/db/delete")
async def db_delete(body: RowDelete):
    try:
        return JSONResponse(await asyncio.to_thread(
            console.delete_row, body.table, body.id))
    except Exception as exc:
        return _console_guard(exc)


@app.post("/api/db/sql")
async def db_sql(body: SqlRun):
    try:
        result = await asyncio.to_thread(console.run_sql, body.sql)
        return JSONResponse(result)
    except Exception as exc:
        return _console_guard(exc)


@app.get("/api/db/backups")
async def db_backups():
    try:
        return JSONResponse({
            "backups": await asyncio.to_thread(backups.list_sets),
            "interval_days": backups.interval_days(),
            "keep": backups.keep_sets(),
            "next_due": await asyncio.to_thread(_next_backup_due),
        })
    except Exception as exc:
        return _console_guard(exc)


def _next_backup_due():
    """Best-effort 'due in X days' readout for the Backups panel."""
    from datetime import datetime, timezone
    last = backups.latest_set()
    if last is None:
        return "now"
    try:
        created = datetime.fromisoformat(last["created_utc"])
    except (KeyError, ValueError):
        return "now"
    days_left = backups.interval_days() - (datetime.now(timezone.utc) - created).total_seconds() / 86400
    return f"{max(0, days_left):.1f}d"


@app.post("/api/db/backup/run")
async def db_backup_run():
    try:
        manifest = await asyncio.to_thread(backups.run_backup, cfg, "manual")
        return JSONResponse({"ok": True, "manifest": manifest})
    except Exception as exc:
        return _console_guard(exc)


@app.get("/api/db/backup/download")
async def db_backup_download(set: str, table: str, fmt: str = "sql"):
    try:
        path = backups.backup_file_path(set, table, fmt)
        return FileResponse(path, filename=os.path.basename(path))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ════════════════ CREDENTIALS · Gemini key pool + Supabase ════════════════
# The full secrets only ever live in .env; every response here is masked so
# the dashboard (and its network traffic) never carries a usable key.
KEEP = "__KEEP__"   # UI sentinel: 'leave stored value alone'

class CredentialSave(BaseModel):
    gemini_keys: list[str] = []      # real key text, or "__KEEP__"/"__KEEP__:<i>"
    gemini_names: list[str] = []     # optional labels, same order
    model_ladder: list[str] = []     # ordered Gemini models (oldest first)
    supabase_url: str = KEEP
    supabase_key: str = KEEP         # service key
    supabase_bucket: str = KEEP
    # Local/other Postgres, beside Supabase (TASK 6).
    postgres_host: str = KEEP
    postgres_port: str = KEEP
    postgres_db: str = KEEP
    postgres_user: str = KEEP
    postgres_password: str = KEEP
    postgres_sslmode: str = KEEP
    # One custom provider kind, per section (TASK 2/3): label, base URL,
    # api_key, model. api_key is KEEP, "KEEP:<i>" or a new literal — the
    # same sentinel rule the Gemini key rows follow below.
    extraction_providers: list[dict] | None = None
    extraction_active: str = KEEP   # "" → the Gemini pool
    revision_providers: list[dict] | None = None
    revision_active: str = KEEP
    # Q1 section toggles (ui key → .env flag) and Q2 extra connections.
    enabled: dict[str, bool] | None = None
    db_connections: list[dict] | None = None


def _merge_db_connections(stored, incoming):
    """Extra connections under the Supabase card (Q2 — presentation only: the
    pipeline keeps its one fixed active connection, db.py untouched).
    Fields take the submitted value; `password` follows the KEEP / "KEEP:<i>"
    / literal sentinel rule of _merge_providers, `enabled` defaults on."""
    out = []
    for i, raw in enumerate(incoming or []):
        label = str(raw.get("label", "")).strip().replace(",", " ")
        if not label:
            continue
        pwd = str(raw.get("password", KEEP) or KEEP)
        if pwd == "__CLEAR__":
            pwd = ""
        elif pwd == KEEP:
            pwd = (stored[i] if i < len(stored) else {}).get("password", "")
        elif pwd.startswith(KEEP + ":"):
            try:
                j = int(pwd.split(":", 1)[1])
            except ValueError:
                pwd = ""
            else:
                pwd = stored[j].get("password", "") if 0 <= j < len(stored) else ""
        out.append({"label": label,
                    "host": str(raw.get("host", "")).strip(),
                    "port": str(raw.get("port", "")).strip(),
                    "db": str(raw.get("db", "")).strip(),
                    "user": str(raw.get("user", "")).strip(),
                    "password": pwd,
                    "sslmode": str(raw.get("sslmode", "")).strip().lower(),
                    "enabled": bool(raw.get("enabled", True))})
    seen, dedup = set(), []
    for c in out:
        if c["label"] not in seen:
            seen.add(c["label"])
            dedup.append(c)
    return dedup


def _db_conn_view(c: dict) -> dict:
    """Masked row for the dashboard — never the raw password."""
    return {"label": c.get("label", ""), "host": c.get("host", ""),
            "port": c.get("port", ""), "db": c.get("db", ""),
            "user": c.get("user", ""), "sslmode": c.get("sslmode", ""),
            "enabled": bool(c.get("enabled", True)),
            "password_set": bool(c.get("password")),
            "password_masked": mask_key(c.get("password", ""))}


def _merge_providers(stored, incoming):
    """Fold the submitted provider rows over the stored ones.

    Labels/URLs/models are plain fields and always take the submitted
    value; `api_key` is KEEP (keep the stored secret at the same position),
    "KEEP:<i>" (keep the stored secret at i — survives a reorder/rename)
    or a new literal. Same sentinel rule as the Gemini key rows above."""
    out = []
    for i, raw in enumerate(incoming or []):
        label = str(raw.get("label", "")).strip().replace(",", " ")
        if not label:
            continue
        key = str(raw.get("api_key", KEEP) or KEEP)
        if key == "__CLEAR__":
            key = ""
        elif key == KEEP:
            prev = stored[i] if i < len(stored) else {}
            key = prev.get("api_key", "")
        elif key.startswith(KEEP + ":"):
            try:
                j = int(key.split(":", 1)[1])
            except ValueError:
                key = ""
            else:
                key = stored[j].get("api_key", "") if 0 <= j < len(stored) else ""
        out.append({"label": label,
                    "base_url": str(raw.get("base_url", "")).strip().rstrip("/"),
                    "api_key": key,
                    "model": str(raw.get("model", "")).strip(),
                    "models": list(raw.get("models") or []) if isinstance(raw.get("models"), list) else []})
    seen, dedup = set(), []
    for p in out:
        if p["label"] not in seen:
            seen.add(p["label"])
            dedup.append(p)
    # Q2: the generic custom-provider kind is deleted — only the built-in
    # 9router entry (fixed loopback URL) survives the save; every other
    # custom entry is discarded by design. Matches the load-time
    # _migrate_ninerouter() coercion in pipeline/config.py.
    kept = ()
    for p in dedup:
        url = p.get("base_url") or ""
        if ("127.0.0.1:20128" in url or "localhost:20128" in url
                or p.get("label") == "9router"):
            if not kept:
                p["label"] = "9router"
                p["base_url"] = "http://127.0.0.1:20128/v1"
                kept = (p,)
        else:
            print(f"[credentials] dropping non-9router provider "
                  f"'{p.get('label', '')}' ({url}) - custom kind removed (Q2)")
    return kept


def _provider_view(p: dict) -> dict:
    """Masked provider row for the dashboard — never the raw api_key."""
    return {"label": p.get("label", ""),
            "base_url": p.get("base_url", ""),
            "model": p.get("model", ""),
            "api_key_set": bool(p.get("api_key")),
            "api_key_masked": mask_key(p.get("api_key", ""))}


def _profiles_view():
    """Masked profile list for the pipeline-tab selector."""
    return {"profiles": [{"name": p.get("name", ""),
                          "host": p.get("host", ""),
                          "port": p.get("port", ""),
                          "scheme": p.get("scheme", "http"),
                          "user": p.get("user", ""),
                          "pass_set": bool(p.get("password")),
                          "pass_masked": mask_key(p.get("password", ""))}
                         for p in cfg.proxy_profiles],
            "active": cfg.proxy_active,   # "" → the Direct (no proxy) entry
            "direct": True}


class ProfileSave(BaseModel):
    profiles: list[dict] = []
    active: str = ""                 # "" is the explicit off value (Q4)


def _host_of(url: str) -> str:
    """Host (and port) of a base URL, for masked display."""
    from urllib.parse import urlparse
    u = urlparse((url or "").strip())
    netloc = u.netloc or (url or "").strip().strip("/")
    return netloc.split("@")[-1]        # strip user:pass@ if present


# Last known verdict of POST /api/credentials/check/provider —
# a convenience cache so the GET view can show a chip without
# probing. In-memory only; a restart simply shows no verdict.
_provider_verdict: dict = {}


def _credentials_view():
    st = router.status(cfg)
    names = list(cfg.gemini_key_names)
    for row in st["keys"]:
        i = row["index"]
        row["name"] = names[i] if i < len(names) else ""
    st["supabase_url"] = cfg.supabase_url
    st["supabase_bucket"] = cfg.supabase_bucket
    st["supabase_key_set"] = bool(cfg.supabase_key)
    st["supabase_key_masked"] = mask_key(cfg.supabase_key)
    # Per-section saved providers — two independent lists, masked (TASK 2/3).
    st["extraction_providers"] = [_provider_view(p)
                                  for p in cfg.extraction_providers]
    st["extraction_active"] = cfg.extraction_active
    st["revision_providers"] = [_provider_view(p)
                                for p in cfg.revision_providers]
    st["revision_active"] = cfg.revision_active
    # Local/other Postgres, beside Supabase (TASK 6).
    st["postgres_host"] = cfg.postgres_host
    st["postgres_port"] = str(cfg.postgres_port)
    st["postgres_db"] = cfg.postgres_db
    st["postgres_user"] = cfg.postgres_user
    st["postgres_sslmode"] = cfg.postgres_sslmode
    st["postgres_password_set"] = bool(cfg.postgres_password)
    st["postgres_password_masked"] = mask_key(cfg.postgres_password)
    st["provider_ok"] = dict(_provider_verdict) or None
    st["legacy_pool"] = os.getenv("GEMINI_API_KEYS") is None and bool(cfg.gemini_key_pool)
    # Q1 section toggles + Q2 extra connections (presentation-only) + Q3 dates.
    st["enabled"] = {"gemini": cfg.gemini_pool_enabled,
                     "ninerouter": cfg.ninerouter_enabled,
                     "revision": cfg.revision_provider_enabled,
                     "supabase": cfg.supabase_enabled}
    st["db_connections"] = [_db_conn_view(c) for c in cfg.db_connections]
    meta = router._key_meta()
    for row in st["keys"]:
        i = row["index"]
        if 0 <= i < len(cfg.gemini_key_pool):
            row["added"] = meta.get(router._key_hash(cfg.gemini_key_pool[i]), "")
    return st


@app.get("/api/credentials")
async def credentials_get():
    return JSONResponse(_credentials_view())


@app.post("/api/credentials")
async def credentials_save(body: CredentialSave):
    """Writes the Gemini pool + Supabase values into .env (only their own
    lines change) and hot-reloads the running config."""
    global cfg
    old_keys = list(cfg.gemini_key_pool)
    old_names = list(cfg.gemini_key_names)
    keys, names = [], []
    for i, raw in enumerate(body.gemini_keys):
        v = (raw or "").strip()
        name = ""
        if v == KEEP:
            idx = i
        elif v.startswith(KEEP + ":"):
            try:
                idx = int(v.split(":", 1)[1])
            except ValueError:
                continue
        else:
            idx = -1
        if idx >= 0:
            if idx >= len(old_keys):
                continue
            v = old_keys[idx]
            name = old_names[idx] if idx < len(old_names) else ""
            # A label edit on a kept key must survive too.
            if i < len(body.gemini_names) and body.gemini_names[i].strip():
                name = body.gemini_names[i].replace(",", " ").strip()
        else:
            if i < len(body.gemini_names):
                # Commas would corrupt the CSV line — labels are cosmetic.
                name = body.gemini_names[i].replace(",", " ").strip()
        if not v or v in keys:
            continue
        keys.append(v)
        names.append(name)

    ladder = [m.strip() for m in body.model_ladder if m.strip()]
    if not ladder:
        # An absent ladder (older client) keeps the current one; a
        # submitted-but-blank ladder is a user error, not a fallback.
        if body.model_ladder:
            return JSONResponse(
                {"ok": False, "error": "The model ladder cannot be "
                                       "empty — list at least one model."},
                status_code=400)
        ladder = list(cfg.gemini_model_ladder)
    if len(set(ladder)) != len(ladder):
        return JSONResponse(
            {"ok": False, "error": "The model ladder has duplicate "
                                   "entries — every rung must be unique."},
            status_code=400)

    url = cfg.supabase_url if body.supabase_url == KEEP else body.supabase_url.strip().rstrip("/")
    skey = cfg.supabase_key if body.supabase_key == KEEP else (
        "" if body.supabase_key == "__CLEAR__" else body.supabase_key.strip())
    bucket = cfg.supabase_bucket if body.supabase_bucket == KEEP else body.supabase_bucket.strip()

    # Local/other Postgres (TASK 6) — KEEP keeps the stored value.
    pg_host = cfg.postgres_host if body.postgres_host == KEEP else body.postgres_host.strip()
    pg_port = str(cfg.postgres_port) if body.postgres_port == KEEP else body.postgres_port.strip()
    if pg_port and not pg_port.isdigit():
        return JSONResponse(
            {"ok": False, "error": "Postgres port must be a number — "
                                   "leave it blank to keep the stored value."},
            status_code=400)
    pg_db = cfg.postgres_db if body.postgres_db == KEEP else body.postgres_db.strip()
    pg_user = cfg.postgres_user if body.postgres_user == KEEP else body.postgres_user.strip()
    pg_password = (cfg.postgres_password if body.postgres_password == KEEP
                   else body.postgres_password.strip())
    pg_sslmode = (cfg.postgres_sslmode if body.postgres_sslmode == KEEP
                  else body.postgres_sslmode.strip().lower())

    # Per-section custom providers (TASK 2/3): lists merge over what is
    # stored, "" is the meaningful "not selected" value (Gemini pool for
    # extraction, no endpoint for revision).
    providers_x = (list(cfg.extraction_providers)
                   if body.extraction_providers is None
                   else _merge_providers(list(cfg.extraction_providers),
                                         body.extraction_providers))
    active_x = (cfg.extraction_active if body.extraction_active == KEEP
                else body.extraction_active.strip())
    providers_r = (list(cfg.revision_providers)
                   if body.revision_providers is None
                   else _merge_providers(list(cfg.revision_providers),
                                         body.revision_providers))
    active_r = (cfg.revision_active if body.revision_active == KEEP
                else body.revision_active.strip())
    if active_x and active_x not in {p["label"] for p in providers_x}:
        return JSONResponse(
            {"ok": False, "error": f"Extraction active provider "
                                   f"'{active_x}' is not in the saved list."},
            status_code=400)
    if active_r and active_r not in {p["label"] for p in providers_r}:
        return JSONResponse(
            {"ok": False, "error": f"Revision active provider "
                                   f"'{active_r}' is not in the saved list."},
            status_code=400)

    # Legacy ROUTER_*/OCR_* keys are deliberately NOT written: Q1 keeps
    # them read-only, and a fresh write would resurrect the local/openai
    # vocabulary acceptance 2 forbids — _providers() in config.py is the
    # read-fallback that keeps an untouched older .env working.
    # Q3 — "date added" is stamped the first time a key is saved; keys that
    # predate this file keep showing "-" in the pane (PREFERENCE §2).
    for v in keys:
        if v not in old_keys:
            router._key_meta_stamp(v)
    env_values = {
        "GEMINI_API_KEYS": ",".join(keys),
        "GEMINI_KEY_NAMES": ",".join(names),
        "GEMINI_MODEL_LADDER": ",".join(ladder),
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_KEY": skey,
        "SUPABASE_BUCKET": bucket,
        "POSTGRES_HOST": pg_host,
        "POSTGRES_PORT": pg_port,
        "POSTGRES_DB": pg_db,
        "POSTGRES_USER": pg_user,
        "POSTGRES_PASSWORD": pg_password,
        "POSTGRES_SSLMODE": pg_sslmode,
        "EXTRACTION_PROVIDERS": json.dumps(list(providers_x), ensure_ascii=False),
        "EXTRACTION_ACTIVE": active_x,
        "REVISION_PROVIDERS": json.dumps(list(providers_r), ensure_ascii=False),
        "REVISION_ACTIVE": active_r,
    }
    # Q1 — the section enable switches (ui key → .env flag; 1 = on).
    if body.enabled is not None:
        for ui_key, env_key in (("gemini", "GEMINI_POOL_ENABLED"),
                                ("ninerouter", "NINEROUTER_ENABLED"),
                                ("revision", "REVISION_PROVIDER_ENABLED"),
                                ("supabase", "SUPABASE_ENABLED")):
            if ui_key in body.enabled:
                env_values[env_key] = "1" if body.enabled[ui_key] else "0"
    # Q2 — extra connection cards under Supabase (presentation only).
    if body.db_connections is not None:
        db_conns = _merge_db_connections(list(cfg.db_connections),
                                         body.db_connections)
        env_values["DB_CONNECTIONS"] = json.dumps(list(db_conns),
                                                  ensure_ascii=False)
    envfile.update_env_file(ENV_PATH, env_values)
    load_dotenv(ENV_PATH, override=True)   # pick up the file we just wrote
    cfg = load_config()
    router.reset()                         # new pool → stale cooldowns are void
    view = _credentials_view()
    view["saved"] = True
    view["key_count"] = len(cfg.gemini_key_pool)
    return JSONResponse(view)


@app.post("/api/credentials/check/provider")
async def credentials_check_provider():
    """Free health check for the custom OCR endpoint: ONE GET
    <base>/models listing, no model run, no generation tokens — the same
    philosophy as the Gemini key checks. (The LIVE test that really chats
    is POST /api/credentials/test below.) Gemini itself has no single
    endpoint to probe here (its keys are checked individually above).

    Registered BEFORE the {index} route below — literal paths must
    precede parameterized ones or FastAPI matches 'provider' as an int."""
    if (cfg.ocr_provider or "gemini").lower() == "gemini":
        return JSONResponse(
            {"ok": False, "provider": "gemini", "url": "",
             "status": 0, "models": [],
             "detail": "The Gemini pool has no endpoint to test — "
                       "use Check all keys to verify the pool.",
             "fault": None}, status_code=409)
    try:
        report = await asyncio.to_thread(vision.check_provider, cfg)
    except Exception as exc:                       # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)
    # Never echo the raw base URL — it may carry user:pass@ in it.
    report["url"] = _host_of(report.get("url", ""))
    _provider_verdict.clear()
    _provider_verdict.update(report)
    return JSONResponse(report)


@app.post("/api/credentials/check")
async def credentials_check():
    """Whole pool, checked ONE key at a time: a keyless googleapis probe
    first (tunnel/region vs. nothing), then one free metadata listing per
    key. A model is never used for a health check."""
    try:
        report = await asyncio.to_thread(router.check_all, cfg)
    except Exception as exc:                       # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(report)

class ProviderTest(BaseModel):
    section: str = "extraction"     # 'extraction' | 'revision'
    base_url: str = ""              # "" → test the Gemini key pool instead
    api_key: str = ""               # unsent values are sent by the UI as stored
    model: str = ""


@app.post("/api/credentials/test")
async def credentials_test(body: ProviderTest):
    """One real model call: reply snippet + latency (TASK item 4).

    This is deliberately NOT /api/credentials/check/provider — that one is
    a free metadata listing and can never show a reply. Exactly one
    generateContent ping for the Gemini pool, or one tiny chat completion
    for a custom provider (Gemini keys and custom providers alike)."""
    started = time.monotonic()
    if not (body.base_url or "").strip():
        pool = cfg.gemini_key_pool
        if not pool:
            return JSONResponse(
                {"ok": False, "error": "No Gemini key is saved — add one first."},
                status_code=400)
        model = (cfg.gemini_model_ladder[0] if cfg.gemini_model_ladder
                 else "gemini-3.5-flash")

        def _one():
            return gemini_mod.call_gemini(
                pool[0], model, 'Reply with exactly: {"ping": true}', "",
                "text/plain", max_tokens=16, retries=1)
        try:
            parsed, _raw, _usage = await asyncio.to_thread(_one)
        except Exception as exc:                       # pragma: no cover
            flt = getattr(exc, "fault", None)
            return JSONResponse(
                {"ok": False, "error": str(exc)[:400],
                 "fault": (flt or {}).get("label", ""),
                 "latency_ms": round((time.monotonic() - started) * 1000, 1)},
                status_code=502)
        return JSONResponse(
            {"ok": True, "model": model,
             "snippet": json.dumps(parsed, ensure_ascii=False)[:200],
             "latency_ms": round((time.monotonic() - started) * 1000, 1)})

    try:
        snippet, _ms = await asyncio.to_thread(
            vision.test_chat, body.base_url.strip(), body.api_key,
            body.model.strip())
    except Exception as exc:
        flt = getattr(exc, "fault", None)
        return JSONResponse(
            {"ok": False, "error": str(exc)[:400],
             "fault": (flt or {}).get("label", ""),
             "latency_ms": round((time.monotonic() - started) * 1000, 1)},
            status_code=502)
    return JSONResponse(
        {"ok": True, "model": body.model,
         "snippet": snippet,
         "latency_ms": round((time.monotonic() - started) * 1000, 1)})


@app.post("/api/credentials/check/{index}")
async def credentials_check_one(index: int):
    """Check a single stored key (the tab walks the pool with this so every
    verdict appears live, and each ♥ re-checks just its own key)."""
    try:
        row = await asyncio.to_thread(router.check_key, cfg, index)
        row["gateway"] = router.gateway()
    except IndexError:
        return JSONResponse({"error": "That key is not saved yet — save it first."},
                            status_code=404)
    except Exception as exc:                       # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(row)


# ════════════════ PROFILES · named proxy profiles (pipeline tab) ════════════════
# The lane only ever rewrites calls that would otherwise hit
# generativelanguage.googleapis.com (OCR, health checks, gateway probes) —
# Supabase, Postgres and the local routers keep their direct paths, which is
# exactly what a system-wide VPN would have slowed down.

@app.get("/api/profiles")
async def profiles_get():
    return JSONResponse(_profiles_view())


@app.post("/api/profiles")
async def profiles_save(body: ProfileSave):
    """Persist named proxy profiles + the active one to the local
    .proxy-profiles.json store and hot-swap the live lane — no restart
    (Q4: active "" is Direct, no proxy)."""
    global cfg
    names, clean = [], []
    stored = {p.get("name", ""): p for p in cfg.proxy_profiles}
    for raw in body.profiles:
        name = str(raw.get("name", "")).strip()
        host = str(raw.get("host", "")).strip()
        if not name or name in names or not host:
            continue
        names.append(name)
        # Password KEEP rules: KEEP → stored one at the same name,
        # "KEEP:<i>" → stored one at position i (reorder/rename safe),
        # anything else is a new literal.
        prev = stored.get(name, {})
        key = str(raw.get("password", "") or "")
        if key == KEEP:
            password = prev.get("password", "")
        elif key.startswith(KEEP + ":"):
            try:
                j = int(key.split(":", 1)[1])
            except ValueError:
                password = ""
            else:
                ordered = [p.get("password", "") for p in cfg.proxy_profiles]
                password = ordered[j] if 0 <= j < len(ordered) else ""
        else:
            password = key
        clean.append({"name": name, "host": host,
                      "port": str(raw.get("port", "")).strip(),
                      "scheme": (raw.get("scheme") or "http").lower(),
                      "user": str(raw.get("user", "")).strip(),
                      "password": password})
    active = body.active.strip()
    if active and active not in names:
        active = ""
    save_proxy_store(clean, active)
    cfg = load_config()
    gemini_mod.set_proxy(cfg)        # live lane swap, no restart
    view = _profiles_view()
    view["ok"] = True
    view["saved"] = True
    return JSONResponse(view)


class ProfileTest(BaseModel):
    name: str = ""
    host: str = ""
    port: str = ""
    scheme: str = "http"
    user: str = ""
    password: str = ""               # KEEP sentinel → the stored secret


@app.post("/api/profiles/test")
async def profiles_test(body: ProfileTest):
    """Probe googleapis.com through ONE profile (or Direct) — a keyless
    gateway probe: no model, no generation tokens, no key. Resolves the KEEP
    sentinel against the stored profile so a masked password is never
    re-sent, and never touches the live lane (gemini.test_proxy restores it)."""
    password = body.password
    if password == KEEP:
        stored = {p.get("name", ""): p for p in cfg.proxy_profiles}
        password = stored.get(body.name, {}).get("password", "")
    profile = {"name": body.name, "host": body.host.strip(),
               "port": str(body.port).strip(),
               "scheme": (body.scheme or "http").lower(),
               "user": body.user.strip(), "password": password}
    try:
        report = await asyncio.to_thread(gemini_mod.test_proxy, profile)
    except Exception as exc:                       # pragma: no cover
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
    report["ok"] = bool(report.get("reachable"))
    return JSONResponse(report)


@app.post("/api/review/bbox")
async def review_bbox(body: BboxUpdate):
    try:
        bbox = [float(v) for v in body.bbox]
        if len(bbox) != 4 or any(v < 0 or v > 1000 for v in bbox):
            raise ValueError("bbox must be 4 numbers between 0 and 1000.")
        ymin, xmin, ymax, xmax = bbox
        if ymax <= ymin or xmax <= xmin:
            raise ValueError("Requires ymin < ymax and xmin < xmax.")
        db.update_diagram_bbox(body.id, [ymin, xmin, ymax, xmax])
        return JSONResponse({"ok": True})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

if __name__ == "__main__":
    import uvicorn
    print(f"Konkour OCR dashboard -> http://localhost:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning",
                timeout_graceful_shutdown=3)
