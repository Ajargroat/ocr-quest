"""Entry point: FastAPI server + WebSocket live events + dashboard UI."""
import logging
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
import asyncio
import os
from contextlib import asynccontextmanager

import requests as http_requests
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
from pipeline import envfile
from pipeline import backups
from pipeline.dataconsole import DataConsole, GuardError
from pipeline.revision import runner as revision_runner
from pipeline.config import load_config, ENV_PATH
from pipeline.db import Database
from pipeline.gemini_router import router
from pipeline.gemini_router import mask as mask_key
from pipeline.runner import Hub, start_run
from dotenv import load_dotenv

cfg = load_config()
db = Database(cfg)
console = DataConsole(cfg, db)
hub = Hub()


async def _backup_loop():
    """Weekly database snapshots (backups/ inside the project). Checked every
    6 hours against the newest manifest, so restarts never double-fire and a
    missed week self-heals on the next check."""
    while True:
        try:
            if await asyncio.to_thread(backups.due):
                manifest = await asyncio.to_thread(backups.run_backup, cfg, "weekly")
                print(f"[backups] snapshot {manifest['name']} - "
                      f"{manifest['total_rows']} rows written")
        except Exception as exc:
            print(f"[backups] snapshot failed: {exc}")
        await asyncio.sleep(6 * 3600)


@asynccontextmanager
async def lifespan(_app):
    hub.attach_loop(asyncio.get_running_loop())
    backup_task = asyncio.create_task(_backup_loop())
    try:
        yield
    finally:
        backup_task.cancel()
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


@app.post("/api/run")
async def run():
    started = start_run(hub, cfg, db)
    return JSONResponse({"started": started}, status_code=200 if started else 409)

@app.post("/api/stop")
async def stop():
    """Ask the running pipeline scan to halt after its current file."""
    stopping = hub.request_stop()
    return JSONResponse({"stopping": stopping},
                        status_code=200 if stopping else 409)

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

def _router_provider_guess(base_url: str) -> str:
    """Loopback URLs mean a local server; anything else is a hosted
    OpenAI-compatible endpoint (same rule pipeline.config.load_config uses)."""
    u = (base_url or "").lower()
    return "local" if ("localhost" in u or "127.0.0.1" in u or "::1" in u) else "openai"


class CredentialSave(BaseModel):
    gemini_keys: list[str] = []      # real key text, or "__KEEP__"/"__KEEP__:<i>"
    gemini_names: list[str] = []     # optional labels, same order
    model_ladder: list[str] = []     # ordered Gemini models (oldest first)
    supabase_url: str = KEEP
    supabase_key: str = KEEP         # service key
    supabase_bucket: str = KEEP
    router_provider: str = KEEP      # 'local' | 'openai'
    router_url: str = KEEP
    router_key: str = KEEP
    router_model: str = KEEP


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
    st["router_provider"] = cfg.router_provider
    st["router_url"] = cfg.router_base_url
    st["router_model"] = cfg.router_model
    st["router_key_set"] = bool(cfg.router_api_key)
    st["router_key_masked"] = mask_key(cfg.router_api_key)
    st["legacy_pool"] = os.getenv("GEMINI_API_KEYS") is None and bool(cfg.gemini_key_pool)
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
        ladder = list(cfg.gemini_model_ladder)

    url = cfg.supabase_url if body.supabase_url == KEEP else body.supabase_url.strip().rstrip("/")
    skey = cfg.supabase_key if body.supabase_key == KEEP else body.supabase_key.strip()
    bucket = cfg.supabase_bucket if body.supabase_bucket == KEEP else body.supabase_bucket.strip()

    rurl = cfg.router_base_url if body.router_url == KEEP else body.router_url.strip().rstrip("/")
    rkey = cfg.router_api_key if body.router_key == KEEP else body.router_key.strip()
    rmodel = cfg.router_model if body.router_model == KEEP else body.router_model.strip()
    rprov = body.router_provider.strip().lower()
    if rprov not in ("local", "openai"):
        rprov = _router_provider_guess(rurl)

    envfile.update_env_file(ENV_PATH, {
        "GEMINI_API_KEYS": ",".join(keys),
        "GEMINI_KEY_NAMES": ",".join(names),
        "GEMINI_MODEL_LADDER": ",".join(ladder),
        "SUPABASE_URL": url,
        "SUPABASE_SERVICE_KEY": skey,
        "SUPABASE_BUCKET": bucket,
        "ROUTER_PROVIDER": rprov,
        "ROUTER_BASE_URL": rurl,
        "ROUTER_API_KEY": rkey,
        "ROUTER_MODEL": rmodel,
    })
    load_dotenv(ENV_PATH, override=True)   # pick up the file we just wrote
    cfg = load_config()
    router.reset()                         # new pool → stale cooldowns are void
    view = _credentials_view()
    view["saved"] = True
    view["key_count"] = len(cfg.gemini_key_pool)
    return JSONResponse(view)


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
    print(f"Konkour OCR dashboard → http://localhost:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning",
                timeout_graceful_shutdown=3)
