"""Entry point: FastAPI server + WebSocket live events + dashboard UI."""
import logging
logging.getLogger("asyncio").setLevel(logging.CRITICAL)
import asyncio
import os
import time
from contextlib import asynccontextmanager

import requests as http_requests
from fastapi import FastAPI, File, Form, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import json
from pipeline import envfile
from pipeline import vision
from pipeline import backups
from pipeline import storage
from pipeline import recrop
from pipeline import gemini as gemini_mod
from pipeline.dataconsole import DataConsole, GuardError
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
    """Dated database snapshots (backups/ inside the project). Checked every
    6 hours against the newest manifest, so restarts never double-fire and a
    missed day self-heals on the next check."""
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
    # before the old lane is dropped â€” never overwrites an existing store.
    seeded = seed_proxy_profile()
    if seeded:
        save_proxy_store(seeded, "")
    # The saved lane must be live before the first dashboard call: a test
    # button, key check or Studio stage reaches Google without the runner's
    # per-run set_proxy(cfg), and on an exit-blocked network a keyless call
    # then fails in a way that blames the key.
    gemini_mod.set_proxy(load_config())
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


class NoCacheStatic(StaticFiles):
    """Static UI files with revalidation.

    The dashboard is edited between runs and the browser has no build hash to
    bust, so a cached `inspect.js` keeps running the old code after a restart
    and a markup change looks like it never landed. ``no-cache`` forces a
    conditional re-read: an unchanged file stays a cheap 304.
    """

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", NoCacheStatic(directory=UI_DIR), name="static")

# The Inspect API: staged rows only - never a Postgres row or a bucket object.
from pipeline import staging_api                                        # noqa: E402
app.include_router(staging_api.build_router(cfg))


@app.get("/", include_in_schema=False)
async def index():
    # No build hash exists, so the shell must revalidate: a cached page keeps
    # the OLD markup and the new JS never finds the elements it expects.
    return FileResponse(os.path.join(os.path.dirname(__file__), "ui", "index.html"),
                        headers={"Cache-Control": "no-cache"})


@app.get("/api/state")
async def state():
    return JSONResponse(hub.snapshot())


class RunOptions(BaseModel):
    """Body of POST /api/run â€” page-level, never persisted (PREFERENCE آ§3).

    `mode` is "one" (one-by-one, today's serial loop) or "parallel".
    `enabled_keys` is the Credentials tab's session on/off selection, sent as
    pool INDICES (row i â†” gemini_key_pool[i], the same contract /api/credentials
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


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ UPLOADS آ· in-app PDF queue (workstream B) â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ

@app.post("/api/uploads")
async def upload_pdf(file: UploadFile = File(...), dest: str = Form("")):
    """Enqueue one PDF into the watched tree (criterion 2).

    The file streams to disk chunked (never whole-file in RAM); the
    embedded converter watcher extracts its pages and the normal
    scanner/runner pipeline processes them â€” sequential by construction.
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


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ STATS آ· pipeline period panel (criterion 11) â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ

_STATS_CACHE = {"at": 0.0, "period": "", "payload": None}


@app.get("/api/stats")
async def period_stats(period: str = "all"):
    """Period stats from data the DB already stores â€” no schema change (Q4).

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


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ PIPELINE آ· pages per call (one-by-one engine) â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ
# How many consecutive page images of one paper go into a single OCR call.
# 1 keeps the classic one-picture-at-a-time behaviour.

class PipelineSettings(BaseModel):
    pages_per_call: int | None = None


@app.get("/api/pipeline/settings")
async def pipeline_settings():
    return JSONResponse({"ok": True,
                         "pages_per_call": int(getattr(cfg, "ocr_pages_per_call", 1) or 1),
                         "max_pages": 8})


@app.post("/api/pipeline/settings")
async def pipeline_settings_save(body: PipelineSettings):
    global cfg
    value = max(1, min(8, int(body.pages_per_call or 1)))
    envfile.update_env_file(ENV_PATH, {"OCR_PAGES_PER_CALL": str(value)})
    load_dotenv(ENV_PATH, override=True)
    cfg = load_config()
    return JSONResponse({"ok": True, "pages_per_call": cfg.ocr_pages_per_call,
                         "max_pages": 8})


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ PROMPTS آ· every LLM instruction, editable â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ
# The defaults live in code (pipeline/prompts.py's catalog); an override is a
# local JSON file beside the usage history, applied to the NEXT call.

class PromptSave(BaseModel):
    id: str
    text: str = ""


class PromptReset(BaseModel):
    id: str | None = None


@app.get("/api/prompts")
async def prompts_get():
    from pipeline import prompts as prompts_mod
    return JSONResponse({"ok": True, "prompts": prompts_mod.view()})


@app.post("/api/prompts")
async def prompts_save(body: PromptSave):
    from pipeline import prompts as prompts_mod
    known = {p["id"]: p for p in prompts_mod.view()}
    if body.id not in known:
        return JSONResponse({"ok": False, "error": "Unknown prompt id."},
                            status_code=404)
    text = body.text or ""
    # identical to the built-in default -> no override at all, so a later
    # default edit in the code shows through
    if text.strip() == (known[body.id].get("default") or "").strip():
        text = ""
    prompts_mod.save(body.id, text)
    return JSONResponse({"ok": True, "prompts": prompts_mod.view()})


@app.post("/api/prompts/reset")
async def prompts_reset(body: PromptReset):
    from pipeline import prompts as prompts_mod
    if body.id:
        prompts_mod.clear(body.id)
    else:
        prompts_mod.clear_all()
    return JSONResponse({"ok": True, "prompts": prompts_mod.view()})


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ ASSISTANT آ· the floating help chat â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ
# Its own endpoint/key/model in .env. The key never reaches the browser: this
# route relays the upstream SSE stream as our own text/thinking events.

class AssistantConfig(BaseModel):
    url: str | None = None
    key: str | None = None      # None/__KEEP__ keeps, "" clears, else the new key
    model: str | None = None
    # The provider list: [{label, base_url, api_key, models}]. api_key follows
    # the same KEEP / "KEEP:<label>" / literal rule as the other providers.
    providers: list[dict] | None = None
    effort: str | None = None   # "" | minimal | low | medium | high
    active: str | None = None   # "label::model" â€” the picker's current pick


class AssistantChat(BaseModel):
    messages: list[dict] = []


class AssistantProbe(BaseModel):
    label: str = ""


@app.get("/api/assistant/config")
async def assistant_config_get():
    from pipeline import assistant
    return JSONResponse(assistant.config_view(cfg))


def _merge_assistant_providers(stored, incoming):
    """Fold the submitted assistant providers over the stored ones (KEEP rule)."""
    from pipeline import assistant
    by_label = {p.get("label"): p for p in stored or [] if isinstance(p, dict)}
    out, seen = [], set()
    for raw in incoming or []:
        if not isinstance(raw, dict):
            continue
        label = str(raw.get("label") or "").strip()
        if not label or label in seen:
            continue
        seen.add(label)
        prev = by_label.get(label) or {}
        key = str(raw.get("api_key", KEEP) or KEEP)
        if key == "__CLEAR__":
            key = ""
        elif key == KEEP:
            key = prev.get("api_key", "")
        elif key.startswith(KEEP + ":"):
            key = (by_label.get(key.split(":", 1)[1]) or {}).get("api_key", "")
        models = [assistant.clean_model_name(m) for m in raw.get("models") or []]
        models = [m for m in models if m]
        out.append({"label": label,
                    "base_url": str(raw.get("base_url") or "").strip(),
                    "api_key": key,
                    "models": list(dict.fromkeys(models))})
    return out


@app.post("/api/assistant/config")
async def assistant_config_save(body: AssistantConfig):
    from pipeline import assistant
    global cfg
    updates = {}
    stored = list(getattr(cfg, "assistant_providers", ()) or ())
    if body.providers is not None:
        providers = _merge_assistant_providers(stored, body.providers)
        updates["ASSISTANT_PROVIDERS"] = json.dumps(providers,
                                                    ensure_ascii=False)
        labels = [p["label"] for p in providers]
        want = (body.active or (getattr(cfg, "assistant_model", "") or "")).strip()
        label, model = assistant.split_id(want)
        if label not in labels or not model:
            first = providers[0] if providers else None
            pick = first["models"][0] if (first and first["models"]) else ""
            updates["ASSISTANT_MODEL"] = (assistant.model_id(first["label"], pick)
                                          if first else "")
        else:
            updates["ASSISTANT_MODEL"] = assistant.model_id(label, model)
    else:
        # legacy single-endpoint fields stay writable on their own
        if body.url is not None:
            updates["ASSISTANT_URL"] = body.url.strip()
        if body.active:
            updates["ASSISTANT_MODEL"] = body.active.strip()
        elif body.model is not None and getattr(cfg, "assistant_url", "").strip():
            # with a legacy endpoint and no saved list, a bare model is the id
            updates["ASSISTANT_MODEL"] = body.model.strip()
    if body.key is not None:
        key = body.key.strip()
        if key == "":
            updates["ASSISTANT_API_KEY"] = ""
        elif key != KEEP:
            updates["ASSISTANT_API_KEY"] = key
    if body.effort is not None:
        effort = body.effort.strip().lower()
        updates["ASSISTANT_EFFORT"] = effort if effort in assistant.EFFORTS else ""
    if updates:
        envfile.update_env_file(ENV_PATH, updates)
        load_dotenv(ENV_PATH, override=True)
        cfg = load_config()
    return JSONResponse(assistant.config_view(cfg))


@app.post("/api/assistant/models")
async def assistant_models(body: AssistantProbe):
    """The provider's own model list (masked config, one metadata GET)."""
    from pipeline import assistant
    provider = next((p for p in assistant._providers(cfg)
                     if p["label"] == body.label.strip()), None)
    if provider is None:
        return JSONResponse({"ok": False, "error": "no such provider",
                             "models": []}, status_code=404)
    report = await asyncio.to_thread(assistant.fetch_models, provider)
    models = report.get("models") or []
    if isinstance(models, list):
        models = [m.get("id") if isinstance(m, dict) else str(m) for m in models]
    return JSONResponse({"ok": bool(report.get("ok")), "models": models,
                         "status": report.get("status"),
                         "detail": report.get("detail") or report.get("error") or ""})


@app.post("/api/assistant/chat")
async def assistant_chat(body: AssistantChat):
    """Stream the assistant's reply (text + reasoning) to the floating chat."""
    from pipeline import assistant
    from fastapi.responses import StreamingResponse

    def events():
        for event in assistant.stream(cfg, body.messages):
            yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        events(), media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

class StatusUpdate(BaseModel):
    table: str
    id: str
    status: str


@app.get("/api/review/meta")
async def review_meta():
    """Subjects, grades and status counters for the QBank filters."""
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

def _fetch_object(name: str):
    """One bucket object's bytes + MIME (the two read-only image routes).

    Slashes are legal in an object name: crops live at
    ``crops/<folder>/<key>.jpg``, which the single-segment legacy route cannot
    address. Traversal segments are rejected before any outbound call.
    """
    name = (name or "").strip().lstrip("/")
    if not name or ".." in name:
        raise ValueError("bad object name")
    # Supabase's storage edge caches object URLs, so an upsert keeps serving
    # the old bytes until the TTL lapses - a re-cropped picture must be visible
    # on the very next request. A unique query string is always a cache miss.
    url = (f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}/{name}"
           f"?v={int(time.time() * 1000)}")
    headers = {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
    }
    resp = http_requests.get(url, headers=headers, timeout=30,
                             proxies=storage.proxies_for(cfg))
    if resp.status_code != 200:
        raise LookupError("Image not found")
    return resp.content, resp.headers.get("content-type", "image/jpeg")


@app.get("/api/proxy/image/{source_id}")
async def proxy_image(source_id: str):
    """Proxies Supabase storage images through the backend using the service key."""
    try:
        content, content_type = await asyncio.to_thread(_fetch_object, source_id)
        from fastapi.responses import Response
        return Response(content=content, media_type=content_type)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


@app.get("/api/media")
async def media_object(object: str):
    """Proxy one media_images entry's bucket object (the Q12 array -> <img>)."""
    try:
        content, mime = await asyncio.to_thread(_fetch_object, object)
        from fastapi.responses import Response
        return Response(content=content, media_type=mime)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except LookupError:
        # the object really is absent from the bucket
        return JSONResponse({"error": "Image not found"}, status_code=404)
    except Exception as e:
        # a dead proxy lane is not a missing picture: say which one failed
        return JSONResponse(
            {"error": "storage unreachable: %s: %s" % (type(e).__name__, e)},
            status_code=502)


@app.get("/api/db/bucket")
async def db_bucket(prefix: str = "", limit: int = 100, offset: int = 0):
    """Read-only list of the configured Supabase bucket's objects (item 3.4).

    The Database tab's Bucket panel browses these; each row's `name` is the
    same object /api/media?object=<name> fetches (slashed crops too).
    No mutation, no bucket picker.
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


# The revision engine is retired: its UI was removed, nothing calls these
# routes and revision_reports/revision_summary are gone from the DB. Its code
# stays in pipeline/revision/ as a dormant reference for a possible revival,
# deliberately unimported - so this module holds no /api/revision/* surface.
# The QBank review fields it used to share (review_status, needs_review) are
# live and unrelated to it.

class BboxUpdate(BaseModel):
    id: str
    bbox: list  # [ymin, xmin, ymax, xmax], each 0..1000
    index: int = 0  # which media_images entry the bbox belongs to (Q12)


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


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ CREDENTIALS آ· Gemini key pool + Supabase â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ
# The full secrets only ever live in .env; every response here is masked so
# the dashboard (and its network traffic) never carries a usable key.
KEEP = "__KEEP__"   # UI sentinel: 'leave stored value alone'

class CredentialSave(BaseModel):
    gemini_keys: list[str] = []      # real key text, or "__KEEP__"/"__KEEP__:<i>"
    gemini_names: list[str] = []     # optional labels, same order
    model_ladder: list[str] = []     # ordered Gemini models (cheapest first)
    model_enabled: list[str] | None = None  # enabled subset; None = keep, [] = 400
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
    # api_key, model. api_key is KEEP, "KEEP:<i>" or a new literal â€” the
    # same sentinel rule the Gemini key rows follow below.
    extraction_providers: list[dict] | None = None
    extraction_active: str = KEEP   # "" â†’ the Gemini pool
    revision_providers: list[dict] | None = None
    revision_active: str = KEEP
    # Q1 section toggles (ui key â†’ .env flag) and Q2 extra connections.
    enabled: dict[str, bool] | None = None
    db_connections: list[dict] | None = None


def _merge_db_connections(stored, incoming, keep_fallback=""):
    """Extra connections under the Supabase card (Q2 â€” presentation only: the
    pipeline keeps its one fixed active connection, db.py untouched).
    Fields take the submitted value; `password` follows the KEEP / "KEEP:<i>"
    / literal sentinel rule of _merge_providers, `enabled` defaults on.
    `keep_fallback` is what a bare KEEP resolves to when the submitted row
    has no stored counterpart yet â€” the legacy POSTGRES_PASSWORD the UI's
    synthesized "Local / other" card represents â€” so the first save that
    materialises that card never wipes the secret it was showing as set."""
    out = []
    for i, raw in enumerate(incoming or []):
        label = str(raw.get("label", "")).strip().replace(",", " ")
        if not label:
            continue
        pwd = str(raw.get("password", KEEP) or KEEP)
        if pwd == "__CLEAR__":
            pwd = ""
        elif pwd == KEEP:
            pwd = (stored[i].get("password", "")
                   if i < len(stored) else keep_fallback)
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
    """Masked row for the dashboard â€” never the raw password."""
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
    "KEEP:<i>" (keep the stored secret at i â€” survives a reorder/rename)
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
        men = raw.get("models_enabled")
        models = [str(m).strip() for m in (raw.get("models") or [])] \
            if isinstance(raw.get("models"), list) else []
        models = [m for m in models if m]
        out.append({"label": label,
                    "base_url": str(raw.get("base_url", "")).strip().rstrip("/"),
                    "api_key": key,
                    "model": str(raw.get("model", "")).strip(),
                    # a model typed only as the DEFAULT is part of the list
                    # too â€” picking one must never make another disappear
                    "models": models + ([str(raw.get("model", "")).strip()]
                                        if str(raw.get("model", "")).strip()
                                        and str(raw.get("model", "")).strip()
                                        not in models else []),
                    "models_enabled": [str(m).strip() for m in men if str(m).strip()] if isinstance(men, list) else None})
    seen, dedup = set(), []
    for p in out:
        if p["label"] not in seen:
            seen.add(p["label"])
            dedup.append(p)
    # Q2: the generic custom-provider kind is deleted â€” only the built-in
    # 9router entry (fixed loopback URL) survives the save; every other
    # custom entry is discarded by design. Matches the load-time
    # _migrate_ninerouter() coercion in pipeline/config.py.
    kept = ()
    for p in dedup:
        url = p.get("base_url") or ""
        if ("127.0.0.1:20128" in url or "localhost:20128" in url
                or p.get("label") == "9router"
                or url.startswith("https://openrouter.ai")):
            if all(k.get("base_url") != p.get("base_url") for k in kept):
                if "openrouter.ai" in (p.get("base_url") or ""):
                    p["label"] = p.get("label") or "openrouter"
                else:
                    p["label"] = "9router"
                    p["base_url"] = "http://127.0.0.1:20128/v1"
                kept = tuple(kept) + (p,)
        else:
            print(f"[credentials] dropping unsupported provider "
                  f"'{p.get('label', '')}' ({url}) - custom kind removed (Q2)")
    return kept


def _provider_view(p: dict) -> dict:
    """Masked provider row for the dashboard â€” never the raw api_key."""
    men = p.get("models_enabled")
    return {"label": p.get("label", ""),
            "base_url": p.get("base_url", ""),
            "model": p.get("model", ""),
            "models": list(p.get("models") or []) if isinstance(p.get("models"), list) else [],
            "models_enabled": ([str(m) for m in men] if isinstance(men, list) else None),
            "api_key_set": bool(p.get("api_key")),
            "api_key_masked": mask_key(p.get("api_key", ""))}


def _profiles_view():
    """Masked profile list for the pipeline-tab selector."""
    return {"profiles": [{"name": p.get("name", ""),
                          "host": p.get("host", ""),
                          "port": p.get("port", ""),
                          "scheme": p.get("scheme", "http"),
                          "enabled": bool(p.get("enabled", True)),
                          "user": p.get("user", ""),
                          "pass_set": bool(p.get("password")),
                          "pass_masked": mask_key(p.get("password", ""))}
                         for p in cfg.proxy_profiles],
            "active": cfg.proxy_active,   # "" â†’ the Direct (no proxy) entry
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


# Last known verdict of POST /api/credentials/check/provider â€”
# a convenience cache so the GET view can show a chip without
# probing. In-memory only; a restart simply shows no verdict.
_provider_verdict: dict = {}


def _credentials_view():
    st = router.status(cfg)
    names = list(cfg.gemini_key_names)
    for row in st["keys"]:
        i = row["index"]
        row["name"] = names[i] if i < len(names) else ""
    st["model_ladder"] = list(cfg.gemini_model_ladder)
    st["model_enabled"] = (list(cfg.gemini_enabled_models)
                            if cfg.gemini_enabled_models is not None else None)
    # Per-model RPD caps (flash-lite 500/day, the rest the shared cap) â€” the
    # credential bars read used/limit per model, not one cap for the ladder.
    st["model_limits"] = dict(router.model_limits())
    st["model_limits"]["default"] = router.day_limit()
    st["supabase_url"] = cfg.supabase_url
    st["supabase_bucket"] = cfg.supabase_bucket
    st["supabase_key_set"] = bool(cfg.supabase_key)
    st["supabase_key_masked"] = mask_key(cfg.supabase_key)
    # Per-section saved providers â€” two independent lists, masked (TASK 2/3).
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
            # The meta entry is {"added": "<iso>"} â€” the view carries the ISO
            # string itself; the dashboard renders r.added.slice(0, 10).
            entry = meta.get(router._key_hash(cfg.gemini_key_pool[i]), "")
            row["added"] = (entry.get("added", "")
                            if isinstance(entry, dict) else entry) or ""
    return st


def _gemini_model_choices():
    """The Gemini models a picker may offer: the ladder âˆ© the enabled toggles.

    Same source the credential section's model toggles write, so the dropdown
    can never offer a model the router would refuse to call."""
    ladder = list(cfg.gemini_model_ladder)
    enabled = getattr(cfg, "gemini_enabled_models", None)
    if enabled is None:
        return ladder
    return [m for m in ladder if m in enabled]


class ModelPinSave(BaseModel):
    pin: str = ""            # "" -> All (the round-robin ladder)
    surface: str = "pipeline"


@app.get("/api/gemini/pin")
async def gemini_pin_get():
    """The pipeline's Gemini model picker: options + the saved pin."""
    return JSONResponse({"ok": True, "pin": cfg.gemini_pin,
                         "models": _gemini_model_choices()})


@app.post("/api/gemini/pin")
async def gemini_pin_save(body: ModelPinSave):
    """Pin the OCR lane to ONE Gemini model."""
    global cfg
    pin = (body.pin or "").strip()
    if pin and pin not in _gemini_model_choices():
        return JSONResponse(
            {"ok": False, "error": f"'{pin}' is not an enabled Gemini model."},
            status_code=400)
    envfile.update_env_file(ENV_PATH, {"GEMINI_MODEL_PIN": pin})
    load_dotenv(ENV_PATH, override=True)
    cfg = load_config()
    return JSONResponse({"ok": True, "saved": True, "pin": cfg.gemini_pin,
                         "models": _gemini_model_choices()})


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
                # Commas would corrupt the CSV line â€” labels are cosmetic.
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
                                       "empty â€” list at least one model."},
                status_code=400)
        ladder = list(cfg.gemini_model_ladder)
    if len(set(ladder)) != len(ladder):
        return JSONResponse(
            {"ok": False, "error": "The model ladder has duplicate "
                                   "entries â€” every rung must be unique."},
            status_code=400)
    # Per-model ON/OFF toggles: None keeps the stored set (older client);
    # a list must be non-empty and a subset of the ladder being saved.
    if body.model_enabled is None:
        enabled = (list(cfg.gemini_enabled_models)
                   if cfg.gemini_enabled_models is not None else None)
    else:
        enabled = [m.strip() for m in body.model_enabled if m.strip()]
        if not enabled:
            return JSONResponse(
                {"ok": False, "error": "Enable at least one model â€” "
                                       "use the section toggle to turn the pool off."},
                status_code=400)
        unknown = [m for m in enabled if m not in ladder]
        if unknown:
            return JSONResponse(
                {"ok": False, "error": "Unknown model(s): " + ", ".join(unknown)},
                status_code=400)

    url = cfg.supabase_url if body.supabase_url == KEEP else body.supabase_url.strip().rstrip("/")
    skey = cfg.supabase_key if body.supabase_key == KEEP else (
        "" if body.supabase_key == "__CLEAR__" else body.supabase_key.strip())
    bucket = cfg.supabase_bucket if body.supabase_bucket == KEEP else body.supabase_bucket.strip()

    # Local/other Postgres (TASK 6) â€” KEEP keeps the stored value.
    pg_host = cfg.postgres_host if body.postgres_host == KEEP else body.postgres_host.strip()
    pg_port = str(cfg.postgres_port) if body.postgres_port == KEEP else body.postgres_port.strip()
    if pg_port and not pg_port.isdigit():
        return JSONResponse(
            {"ok": False, "error": "Postgres port must be a number â€” "
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
    # vocabulary acceptance 2 forbids â€” _providers() in config.py is the
    # read-fallback that keeps an untouched older .env working.
    # Q3 â€” "date added" is stamped the first time a key is saved; keys that
    # predate this file keep showing "-" in the pane (PREFERENCE آ§2).
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
    # Per-model toggles: only touch the key when the client sent toggles or
    # the file already carries it â€” a legacy .env must not gain the key until
    # first use (absent = every rung on).
    if body.model_enabled is not None or os.getenv("GEMINI_MODELS_ENABLED") is not None:
        env_values["GEMINI_MODELS_ENABLED"] = ",".join(enabled or ())
    # Q1 â€” the section enable switches (ui key â†’ .env flag; 1 = on).
    if body.enabled is not None:
        for ui_key, env_key in (("gemini", "GEMINI_POOL_ENABLED"),
                                ("ninerouter", "NINEROUTER_ENABLED"),
                                ("revision", "REVISION_PROVIDER_ENABLED"),
                                ("supabase", "SUPABASE_ENABLED")):
            if ui_key in body.enabled:
                env_values[env_key] = "1" if body.enabled[ui_key] else "0"
    # Q2 â€” extra connection cards under Supabase (presentation only).
    if body.db_connections is not None:
        db_conns = _merge_db_connections(list(cfg.db_connections),
                                         body.db_connections,
                                         keep_fallback=cfg.postgres_password)
        env_values["DB_CONNECTIONS"] = json.dumps(list(db_conns),
                                                  ensure_ascii=False)
    envfile.update_env_file(ENV_PATH, env_values)
    load_dotenv(ENV_PATH, override=True)   # pick up the file we just wrote
    cfg = load_config()
    router.reset()                         # new pool â†’ stale cooldowns are void
    view = _credentials_view()
    view["saved"] = True
    view["key_count"] = len(cfg.gemini_key_pool)
    return JSONResponse(view)


@app.post("/api/credentials/check/provider")
async def credentials_check_provider():
    """Free health check for the custom OCR endpoint: ONE GET
    <base>/models listing, no model run, no generation tokens â€” the same
    philosophy as the Gemini key checks. (The LIVE test that really chats
    is POST /api/credentials/test below.) Gemini itself has no single
    endpoint to probe here (its keys are checked individually above).

    Registered BEFORE the {index} route below â€” literal paths must
    precede parameterized ones or FastAPI matches 'provider' as an int."""
    if (cfg.ocr_provider or "gemini").lower() == "gemini":
        return JSONResponse(
            {"ok": False, "provider": "gemini", "url": "",
             "status": 0, "models": [],
             "detail": "The Gemini pool has no endpoint to test â€” "
                       "use Check all keys to verify the pool.",
             "fault": None}, status_code=409)
    try:
        report = await asyncio.to_thread(vision.check_provider, cfg)
    except Exception as exc:                       # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)
    # Never echo the raw base URL â€” it may carry user:pass@ in it.
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
    base_url: str = ""              # "" â†’ test the Gemini key pool instead
    api_key: str = ""               # unsent values are sent by the UI as stored
    model: str = ""


@app.post("/api/credentials/test")
async def credentials_test(body: ProviderTest):
    """One real model call: reply snippet + latency (TASK item 4).

    This is deliberately NOT /api/credentials/check/provider â€” that one is
    a free metadata listing and can never show a reply. Exactly one
    generateContent ping for the Gemini pool, or one tiny chat completion
    for a custom provider (Gemini keys and custom providers alike)."""
    started = time.monotonic()
    if not (body.base_url or "").strip():
        pool = cfg.gemini_key_pool
        if not pool:
            return JSONResponse(
                {"ok": False, "error": "No Gemini key is saved â€” add one first."},
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
    verdict appears live, and each â™¥ re-checks just its own key)."""
    try:
        row = await asyncio.to_thread(router.check_key, cfg, index)
        row["gateway"] = router.gateway()
    except IndexError:
        return JSONResponse({"error": "That key is not saved yet â€” save it first."},
                            status_code=404)
    except Exception as exc:                       # pragma: no cover
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(row)


# â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ PROFILES آ· named proxy profiles (pipeline tab) â•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گâ•گ
# The lane only ever rewrites calls that would otherwise hit
# generativelanguage.googleapis.com (OCR, health checks, gateway probes) â€”
# Supabase, Postgres and the local routers keep their direct paths, which is
# exactly what a system-wide VPN would have slowed down.

@app.get("/api/profiles")
async def profiles_get():
    return JSONResponse(_profiles_view())


@app.post("/api/profiles")
async def profiles_save(body: ProfileSave):
    """Persist named proxy profiles + the active one to the local
    .proxy-profiles.json store and hot-swap the live lane â€” no restart
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
        # Password KEEP rules: KEEP â†’ stored one at the same name,
        # "KEEP:<i>" â†’ stored one at position i (reorder/rename safe),
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
        prev_en = prev.get("enabled", True)
        clean.append({"name": name, "host": host,
                      "port": str(raw.get("port", "")).strip(),
                      "scheme": (raw.get("scheme") or "http").lower(),
                      "enabled": bool(raw.get("enabled", prev_en)),
                      "user": str(raw.get("user", "")).strip(),
                      "password": password})
    active = body.active.strip()
    if active and active not in names:
        active = ""
    by_name = {c.get("name", ""): c for c in clean}
    if active and not by_name.get(active, {}).get("enabled", True):
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
    password: str = ""               # KEEP sentinel â†’ the stored secret


@app.post("/api/profiles/test")
async def profiles_test(body: ProfileTest):
    """Probe googleapis.com through ONE profile (or Direct) â€” a keyless
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


@app.get("/api/proxy/check")
async def proxy_check():
    """Probe googleapis.com through EVERY enabled proxy profile.

    One keyless gateway probe per profile - the same one the lanes use, so a
    verdict here means a verdict there. No model, no key, no tokens.
    """
    results = []
    for profile in cfg.proxy_profiles:
        if not profile.get("enabled", True):
            continue
        try:
            report = await asyncio.to_thread(gemini_mod.test_proxy, profile)
        except Exception as exc:                 # pragma: no cover
            report = {"reachable": False, "kind": "unknown",
                      "detail": str(exc)}
        results.append({"name": profile.get("name", ""),
                        "ok": bool(report.get("reachable")),
                        "kind": report.get("kind") or "",
                        "detail": report.get("detail") or ""})
    return JSONResponse({"ok": True, "active": cfg.proxy_active,
                         "results": results})


@app.post("/api/review/bbox")
async def review_bbox(body: BboxUpdate):
    try:
        bbox = [float(v) for v in body.bbox]
        if len(bbox) != 4 or any(v < 0 or v > 1000 for v in bbox):
            raise ValueError("bbox must be 4 numbers between 0 and 1000.")
        ymin, xmin, ymax, xmax = bbox
        if ymax <= ymin or xmax <= xmin:
            raise ValueError("Requires ymin < ymax and xmin < xmax.")
        if body.index < 0:
            raise ValueError("index must be 0 or more.")
        db.update_media_bbox(body.id, body.index, [ymin, xmin, ymax, xmax])
        return JSONResponse({"ok": True})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


class RecropUpdate(BaseModel):
    id: str
    bbox: list  # [ymin, xmin, ymax, xmax], each 0..1000
    index: int = 0  # which media_images entry to re-cut


@app.get("/api/qbank/page")
async def qbank_page(question: str):
    """The page a QBank picture was cut from - the bbox editor's backdrop.

    Pictures are stored as finished crops, so correcting a box needs the page
    itself: `sources.storage_url` still points at the page JPEG the scanner
    uploaded. Read-only; the picture on the card stays /api/media.
    """
    try:
        row = await asyncio.to_thread(db.fetch_media_row, question)
        if not row or not row.get("source_url"):
            raise LookupError("no source page for this question")
        data = await asyncio.to_thread(recrop.page_bytes, row["source_url"])
        return Response(content=data, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=600"})
    except LookupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": " ".join(str(exc).split())[:300]},
                            status_code=502)


@app.post("/api/qbank/recrop")
async def qbank_recrop(body: RecropUpdate):
    """Re-cut one picture from its page and replace the SAME bucket object.

    The Q12 invariant: a corrected box re-uploads over the crop's own id, so a
    row never accumulates variants. The box lands in the DB in the same call,
    so the stored bbox and the stored JPEG can never disagree.
    """
    try:
        result = await asyncio.to_thread(
            recrop.recrop, cfg, db, body.id, int(body.index), body.bbox)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except LookupError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=404)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": " ".join(str(exc).split())[:300]},
                            status_code=502)

if __name__ == "__main__":
    import uvicorn
    print(f"Konkour OCR dashboard -> http://localhost:{cfg.port}")
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning",
                timeout_graceful_shutdown=3)
