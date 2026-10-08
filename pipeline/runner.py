"""The orchestrator: replaces Loop Over Items + If + Loop On Success.
Runs in a background thread and pushes live events to the dashboard."""
import asyncio
import base64
import os
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime

from . import deferrals, faults, parallel
from .answers import build_answer_prompt, finalize_answer_rows, prepare_answer_rows
from .config import Config
from .db import Database
from .gemini import set_proxy
from .gemini_router import router, mask
from .questions import QUESTION_PROMPT, build_question_rows
from .scanner import scan
from .storage import upload_file
from .vision import call_ocr_openai


def _call_ocr(cfg, prompt, data_b64, mime_type, on_problem=None, on_route=None,
              key_pin=None):
    """Dispatch one OCR request to the configured engine.

    Lives in the runner (not vision.py) on purpose: the Gemini lane is the
    module-level `router` singleton here, which tests patch, and the
    dispatch honours cfg.ocr_provider ('gemini' default | 'custom').
    'custom' is the one OpenAI-compatible provider kind — its endpoint
    never rides the proxy lane (gemini.proxy_proxies only scopes
    Google-bound calls).

    `key_pin` (parallel mode) pins the call to ONE key: the router walks
    only that key's own ladder and never rotates to another key."""
    if cfg is not None and (cfg.ocr_provider or "gemini").lower() != "gemini":
        # Custom lane (the built-in 9router, Q2): time the call and record
        # it into the same per-call JSON history the usage reports read —
        # Gemini rows are recorded by router._count_send, so this lane must
        # record its own (criterion 6 covers both sections).
        started = time.monotonic()
        parsed = call_ocr_openai(cfg, prompt, data_b64, mime_type,
                                 on_problem=on_problem, on_route=on_route)
        try:
            router._calls_append(
                getattr(cfg, "ocr_api_key", "") or "",
                getattr(cfg, "ocr_model", "") or "default",
                ms=(time.monotonic() - started) * 1000.0,
                key_name="9router", ok=True)
        except Exception:
            pass
        return parsed
    if key_pin:
        # Parallel lane: one PINNED key, its own ladder, no key rotation.
        parsed, _raw, _route = router.call_pinned(
            cfg, key_pin, prompt, data_b64, mime_type,
            on_problem=on_problem, on_route=on_route)
        return parsed
    parsed, _raw, _route = router.call(
        cfg, prompt, data_b64, mime_type,
        on_problem=on_problem, on_route=on_route)
    return parsed


def missing_credentials(cfg):
    """Pre-flight gate — the env names a run's active providers need.
    A switched-off section reports its own flag, so the log names the toggle
    that turned the run down (Q1)."""
    missing = []
    if not cfg.supabase_key:
        missing.append("SUPABASE_SERVICE_KEY")
    if (cfg.ocr_provider or "gemini").lower() == "gemini":
        if not cfg.gemini_pool_enabled:
            missing.append("GEMINI_POOL_ENABLED (Credentials · OCR engine)")
        elif not cfg.gemini_key_pool:
            missing.append("GEMINI_API_KEYS (Credentials tab)")
    else:
        if not cfg.ninerouter_enabled:
            missing.append("NINEROUTER_ENABLED (Credentials · OCR engine)")
        elif not cfg.ocr_base_url:
            missing.append("OCR_BASE_URL (Credentials tab)")
    if cfg.revision_active and not cfg.revision_provider_enabled:
        missing.append("REVISION_PROVIDER_ENABLED (Credentials · Revision)")
    return missing


def _sleep_or_stop(hub, seconds):
    """Sleep that wakes early when the stop button is pressed (criterion 10).

    A plain time.sleep(wait) ignores Stop until the next file; chunked
    waits make the button land mid-wait instead. Returns True when the
    full wait elapsed, False when stop was requested. A single in-flight
    HTTP request still cannot abort mid-socket (U2) — stop lands between
    phases and retries.
    """
    end = time.monotonic() + max(0.0, seconds)
    while True:
        if hub.stop_flag.is_set():
            return False
        if end - time.monotonic() <= 0:
            return True
        time.sleep(min(0.5, end - time.monotonic()))


class Hub:
    """Holds live state and fans events out to all WebSocket clients."""

    def __init__(self):
        self.loop = None
        self.clients = set()          # asyncio.Queue per connected browser
        self.lock = threading.RLock()
        self.running = False
        self.log = deque(maxlen=400)
        self.rev_log = deque(maxlen=400)   # Revision-tab events (kept out of the pipeline log)
        self.seq = 0                        # monotonic id so the UI can dedupe replays
        self.stop_flag = threading.Event()  # set by /api/stop: finish the current file, then halt
        self.files = {}
        self.stats = {"total": 0, "processed": 0, "questions": 0,
                      "answers": 0, "errors": 0, "deferred": 0}
        self.error_kinds = {}      # fault kind → count, reset each run
        self.db_fail_streak = 0    # consecutive files ending in parked writes
        self.lanes = {}            # pool index → last lane state (reconnect seed)

    def attach_loop(self, loop):
        self.loop = loop

    def emit(self, event: dict):
        event.setdefault("ts", datetime.now().strftime("%H:%M:%S"))
        with self.lock:
            self.seq += 1
            event["seq"] = self.seq
            self._apply(event)
            (self.rev_log if event.get("rev") else self.log).append(event)
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)

    async def _broadcast(self, event):
        for queue in list(self.clients):
            try:
                queue.put_nowait(event)
            except Exception:
                pass

    def _apply(self, event):
        kind = event.get("type")
        if kind == "file":
            record = self.files.setdefault(event["id"], {})
            for key in ("name", "kind", "status", "detail"):
                if event.get(key) is not None:
                    record[key] = event[key]
        elif kind == "stats":
            self.stats.update(event["stats"])
        elif kind == "lane":
            k = event.get("key")
            if k is not None:
                self.lanes[k] = {f: event[f] for f in
                                 ("state", "index", "file", "model", "attempt",
                                  "phase")
                                 if event.get(f) is not None}
        elif kind == "run_started":
            self.running = True
            self.error_kinds = {}
            self.db_fail_streak = 0
            self.lanes = {}
        elif kind == "error":
            k = event.get("kind", "unknown")
            self.error_kinds[k] = self.error_kinds.get(k, 0) + 1
        elif kind == "run_finished":
            self.running = False

    def rev_tail(self, n: int = 160):
        with self.lock:
            return list(self.rev_log)[-n:]

    def request_stop(self):
        """Ask a running scan to halt after its current file/chunk."""
        with self.lock:
            if not self.running:
                return False
            self.stop_flag.set()
            return True

    def snapshot(self):
        with self.lock:
            return {
                "running": self.running,
                "stats": dict(self.stats),
                "files": dict(self.files),
                "log": list(self.log),
                "rev_log": list(self.rev_log),
                "error_kinds": dict(self.error_kinds),
                "lanes": dict(self.lanes),
            }


def start_run(hub: Hub, cfg: Config, db: Database, mode: str = "serial",
              enabled_keys=None) -> bool:
    """Start a run. `mode` is "serial" (one-by-one, today's loop) or
    "parallel" (round-based, one file per healthy key per round).
    `enabled_keys` is the session-only per-key on/off selection sent with
    Run — None means "every key in the pool is on"."""
    with hub.lock:
        if hub.running:
            return False
        hub.running = True
        hub.stop_flag.clear()
    options = {"mode": mode,
               "enabled_keys": list(enabled_keys) if enabled_keys else None}
    threading.Thread(target=_run, args=(hub, cfg, db, options), daemon=True).start()
    return True


def _run(hub: Hub, cfg: Config, db: Database, options=None):
    options = options or {}
    def log(level, message, err=None):
        event = {"type": "log", "level": level, "message": message}
        if err:
            event["err"] = err
        hub.emit(event)

    def diag(flt, where, source=""):
        hub.emit({"type": "error", "kind": flt["kind"], "label": flt["label"],
                  "emoji": flt["emoji"], "hint": flt["hint"], "where": where,
                  "file": source, "raw": flt.get("raw", "") or ""})

    try:
        hub.emit({"type": "run_started"})

        # --- pre-flight checks -------------------------------------
        set_proxy(cfg)                # profile lane for Google-bound calls
        missing = missing_credentials(cfg)
        if missing:
            log("error", "Missing credentials in .env: " + ", ".join(missing), "bad_key")
            return
        try:
            db.ping()
        except Exception as exc:
            flt = faults.classify(exc)
            flt["raw"] = str(exc)[:400]
            diag(flt, "preflight · Postgres ping")
            log("error", f"Cannot reach Postgres — {flt['label']}: {flt['hint']}", flt["kind"])
            return

        # One free metadata call per key (no generation tokens) gives the
        # router a fresh map of dead keys / dead models, so the first real
        # request lands on a working pair instead of probing blind. Skipped
        # when this process already checked, unless the pool changed — or
        # when the OCR provider is not Gemini, in which case the key×model
        # router is simply not on duty.
        fingerprint = (tuple(cfg.gemini_key_pool), tuple(cfg.gemini_model_ladder))
        if (cfg.ocr_provider or "gemini").lower() != "gemini":
            log("info", f"Preflight: OCR provider is '{cfg.ocr_provider}' "
                        f"({cfg.ocr_model or 'default model'}); the Gemini key "
                        "pool stays untouched this run.")
        elif router.preflight_for != fingerprint:
            log("info", f"Preflight: checking {len(cfg.gemini_key_pool)} Gemini key(s) "
                         "one by one — googleapis probe + free listings, no model used…")
            report = router.check_all(cfg)
            gw = report.get("gateway") or {}
            if gw and not gw.get("reachable"):
                log("warn", f"  ↳ gateway: {gw.get('detail', 'googleapis.com unreachable')} "
                            "— tunnel/region problem; no key is blamed until it is up")
            usable = [r for r in report["keys"] if r["status"] in ("ok", "alive_unlisted")]
            for r in report["keys"]:
                if r["fault"]:
                    log("warn", f"  ↳ {r['label']} ({r['masked']}): "
                                f"{r['fault']['emoji']} {r['fault']['label']}")
            if not usable:
                net_only = bool(report["keys"]) and all(
                    (r["fault"] or {}).get("kind") in
                    ("tunnel_down", "send_blocked", "recv_dropped", "overload")
                    for r in report["keys"] if r["fault"])
                if net_only:
                    log("error", "Cannot reach Google right now — restore the tunnel "
                                 "and rerun; no key was blamed for a network fault.",
                        "tunnel_down")
                else:
                    log("error", "No Gemini key is usable right now — open the Credentials "
                                 "tab, fix a key or wait for a quota reset.", "bad_key")
                return
            log("success", f"  ↳ {len(usable)}/{len(report['keys'])} key(s) ready; "
                           f"router will serve {', '.join(cfg.gemini_model_ladder)}")
            router.preflight_for = fingerprint

        # --- scan ----------------------------------------------------
        log("info", f"Scanning folder: {cfg.input_root}")
        items = scan(cfg, warn=lambda message: log("warn", f"  ↳ {message}"))
        if not items:
            log("warn", "Scan finished — no files to process.")
            return

        hub.emit({"type": "stats", "stats": {"total": len(items), "processed": 0}})
        for item in items:
            hub.emit({"type": "file", "id": item.source_id, "name": item.rel_path,
                      "kind": item.type, "status": "queued", "detail": "Waiting"})
        log("info", f"Found {len(items)} file(s). Starting pipeline…")
        if hub.stop_flag.is_set():
            log("warn", "Stop was requested before processing began — run cancelled; "
                        "no file was touched.")
            return

        # --- dispatch: serial (one-by-one) or parallel (round-based) -----
        # Parallel mode only exists for the Gemini key pool; the custom
        # (9router) lane has no key to pin a lane to, so it falls back.
        mode = options.get("mode", "serial")
        if mode == "parallel" and (cfg.ocr_provider or "gemini").lower() == "gemini":
            _run_parallel(hub, cfg, db, items, options, log)
        else:
            if mode == "parallel":
                log("warn", "Parallel mode needs the Gemini key pool — "
                            "running one-by-one this run.")
            _run_serial(hub, cfg, db, items, log)
    except Exception as exc:
        flt = getattr(exc, "fault", None) or faults.classify(exc)
        log("error", f"Run aborted — {flt['label']}: {exc}", flt["kind"])
    finally:
        hub.emit({"type": "run_finished"})


def _run_serial(hub: Hub, cfg: Config, db: Database, items, log):
    """Today's strictly-serial loop — one file end to end (the one-by-one
    system). Behaviour is unchanged; the parallel engine is the alternative."""
    # Circuit breaker: MAX_CONSECUTIVE_DB_FAILURES files in a row whose
    # Postgres writes had to be parked means the DB is down, not blipping
    # — stop so we keep neither burning quota nor stacking up cache.
    limit = deferrals.max_consecutive_failures()
    stopped = False
    blocked = 0
    for index, item in enumerate(items, 1):
        if hub.stop_flag.is_set():
            stopped = True
            log("warn", f"Stop requested — halting after file {index - 1} of "
                        f"{len(items)}; the remaining files stay for the next run.")
            break
        outcome = _process_item(hub, cfg, db, item, index, len(items))
        if outcome == "clean":
            hub.db_fail_streak = 0
        elif outcome == "blocked":
            # Q4: every key×model pair is exhausted, so the rest of the
            # queue would fail identically — stop and say how many files
            # the run did not process instead of re-queueing them all.
            blocked = blocked_remaining(len(items), index)
            break
        elif outcome in ("deferred", "store_failed"):
            hub.db_fail_streak += 1
            if hub.db_fail_streak >= limit:
                _major_db_halt(hub, log, limit)
                break
        # a plain "failed" (unreadable file, Google tunnel…) leaves the
        # streak untouched — it is not a database signal
    else:
        # Normal end of the queue: come back and import everything parked.
        _flush_parked(hub, db, log)

    if blocked:
        _flush_parked(hub, db, log)
        log("error", f"Run stopped — {blocked} file(s) blocked: every "
                     "Gemini key/model pair is exhausted. See the "
                     "Credentials tab for the per-key bars.")
    elif stopped:
        # A manual stop still imports parked writes: that work is finished
        # and must not wait for another run.
        _flush_parked(hub, db, log)
        log("warn", "Run stopped on request.")
    else:
        log("success", "Run finished.")


def _lane_event(hub, cfg, bundle, state, **extra):
    """One `lane` WS event for a parallel lane. Shared by the round worker
    (`produce`) and the import loop, which owns the terminal state.
    `hub=None` means no visibility channel (tests)."""
    if hub is None:
        return
    key = bundle.get("key")
    pool = list(getattr(cfg, "gemini_key_pool", ()) or ())
    ev = {"type": "lane",
          "key": pool.index(key) if key in pool else None,   # pool index
          "masked": mask(key),                               # display + fallback
          "state": state,
          "index": bundle["index0"] + 1,
          "file": getattr(bundle.get("item"), "rel_path", "")}
    ev.update(extra)
    hub.emit(ev)


def _produce_round(cfg, db, assignments, hub=None):
    """Phase A+B of one parallel round.

    Phase A (this thread, file order): read each file's bytes and build its
    OCR prompt — the answer branch reads its DB context here, so the single
    Postgres connection is never touched off-thread.

    Phase B (small thread pool): the slow part — Supabase upload + OCR with
    the lane's PINNED key. No DB write happens here.

    `hub` (parallel runs) receives `lane` events WHILE the pool works — the
    dashboard's parallel readout draws from these, not from the import phase
    that runs after the OCR wait. None keeps the unit-test/serial paths quiet.

    Returns {index0: bundle}; a bundle carries the produced values, or the
    exception to replay in the import phase. The pool's workers never raise,
    so one file's failure cannot abort its round."""
    prepared = []
    for index0, item, key in assignments:
        bundle = {"item": item, "index0": index0, "key": key}
        try:
            with open(item.file_path, "rb") as fh:
                raw = fh.read()
            bundle["raw"] = raw
            bundle["data_b64"] = base64.b64encode(raw).decode()
            if item.type == "سوال":
                bundle["prompt"] = QUESTION_PROMPT
                bundle["candidates"] = None
            else:
                candidates = db.fetch_context_questions(
                    item.subject, item.topic, item.grade)
                bundle["candidates"] = candidates
                bundle["prompt"] = build_answer_prompt(candidates)
        except Exception as exc:
            bundle["error"] = exc
        prepared.append(bundle)

    def lane(state, bundle, **extra):
        _lane_event(hub, cfg, bundle, state, **extra)

    def produce(bundle):
        def lane_problem(flt, attempt, tries, wait, where=""):
            # Backoff → the lane says what it is waiting on. `where` defaults so
            # the 4-arg callers (gemini.py / storage.py / vision.py) work too.
            lane("wait", bundle, phase=f"waiting {int(wait)}s")

        lane("start", bundle, phase="uploading")  # the lane picked this file up
        if bundle.get("error"):
            lane("failed", bundle, phase="failed",
                 reason=str(bundle["error"])[:160])
            return bundle
        try:
            item = bundle["item"]
            bundle["storage_url"] = upload_file(
                cfg, item.source_id, bundle["raw"], item.mime_type)
            lane("start", bundle, phase="ocr")     # upload done — now OCR
            bundle["parsed"] = _call_ocr(
                cfg, bundle["prompt"], bundle["data_b64"], item.mime_type,
                on_problem=lane_problem,
                on_route=lambda label, masked, model, attempt: lane(
                    "route", bundle, model=model, attempt=attempt,
                    phase="ocr"),
                key_pin=bundle["key"])
        except Exception as exc:
            bundle["error"] = exc
            lane("failed", bundle, phase="failed", reason=str(exc)[:160])
            return bundle
        lane("done", bundle, phase="importing")
        return bundle

    with ThreadPoolExecutor(max_workers=max(1, len(prepared))) as pool:
        futures = [pool.submit(produce, b) for b in prepared]
        for fut in as_completed(futures):
            fut.result()          # produce() never raises — it stores errors
    return {b["index0"]: b for b in prepared}


def _run_parallel(hub: Hub, cfg: Config, db: Database, items, options, log):
    """Round-based parallel run.

    Q1=A: N lanes, each PINNED to one healthy key (one file per lane per
    round, a round barrier before the next round). Q2=B: a round's results
    are imported in ORIGINAL FILE ORDER, on this thread — the DB connection
    is single, so nothing writes it from a lane. Q3=B: a file whose lane has
    no usable model left stays for the next run (its lane is dropped for the
    rest of the run)."""
    plan = router._plan(cfg, time.time())
    lanes = parallel.lane_keys(cfg, options.get("enabled_keys"), plan=plan)
    if not lanes:
        log("warn", "Parallel mode: no healthy key has a free lane — "
                    "running one-by-one this run.")
        _run_serial(hub, cfg, db, items, log)
        return
    log("info", f"Parallel mode: {len(lanes)} lane(s) — one file per key per "
                f"round over {len(items)} file(s); results import in file order.")

    limit = deferrals.max_consecutive_failures()
    stopped = False
    blocked = 0
    base = 0
    round_no = 0
    for segment in parallel.type_segments(items):
        remaining = list(segment)
        while remaining and lanes and not stopped and not blocked:
            if hub.stop_flag.is_set():
                stopped = True
                log("warn", f"Stop requested — halting before file {base + 1} of "
                            f"{len(items)}; the remaining files stay for the next run.")
                break
            # One round = one file per surviving lane, in file order.
            _round_no, chunk = parallel.round_plan(remaining, lanes)[0]
            round_no += 1
            assignments = [(base + idx, item, key) for idx, item, key in chunk]
            hub.emit({"type": "round", "no": round_no,
                      "lanes": len(lanes), "files": len(assignments)})
            bundles = _produce_round(cfg, db, assignments, hub=hub)

            exhausted = set()
            imported = 0
            for _index0, bundle in parallel.import_order(
                    [(b["index0"], b) for b in bundles.values()]):
                index = bundle["index0"] + 1
                pre = {"storage_url": bundle.get("storage_url"),
                       "parsed": bundle.get("parsed"),
                       "candidates": bundle.get("candidates"),
                       "error": bundle.get("error")}
                outcome = _process_item(hub, cfg, db, bundle["item"], index,
                                        len(items), pre=pre)
                ok = outcome in ("clean", "deferred")   # both reach done/
                extra = {"phase": "imported" if ok else "not imported"}
                if not ok and bundle.get("error"):
                    extra["reason"] = str(bundle["error"])[:160]
                _lane_event(hub, cfg, bundle, "done" if ok else "failed",
                            **extra)
                imported += 1
                if outcome == "clean":
                    hub.db_fail_streak = 0
                elif outcome == "blocked":
                    # This lane's pinned key is out of usable models — the
                    # file stays for the next run (Q3=B); drop the lane.
                    exhausted.add(bundle["key"])
                elif outcome in ("deferred", "store_failed"):
                    hub.db_fail_streak += 1
                    if hub.db_fail_streak >= limit:
                        _major_db_halt(hub, log, limit)
                        stopped = True
                        break

            remaining = remaining[imported:]
            base += imported
            if exhausted:
                lanes = [k for k in lanes if k not in exhausted]
                if not lanes:
                    blocked = len(items) - base
                    break
                log("warn", f"Parallel: {len(exhausted)} key(s) out of usable "
                            f"models — {len(lanes)} lane(s) continue.")

    if blocked:
        _flush_parked(hub, db, log)
        log("error", f"Run stopped — {blocked} file(s) blocked: no key has a "
                     "usable model left. See the Credentials tab.")
    elif stopped:
        _flush_parked(hub, db, log)
        log("warn", "Run stopped on request.")
    else:
        _flush_parked(hub, db, log)
        log("success", "Run finished.")


def _major_db_halt(hub: Hub, log, limit):
    """The breaker tripped: report a MAJOR error explaining what happened."""
    parked = len(deferrals.load())
    hint = (
        f"{limit} consecutive files could not be saved to Postgres, each after "
        f"{deferrals.save_retries()} failed attempts. The run stopped rather than burn more Gemini "
        "calls on results that cannot be stored. Likely causes: the Postgres container "
        "is down or unreachable (docker compose, host/port/password, or the tunnel to "
        "the DB), the database ran out of disk space or connection slots, or a schema "
        "change (a renamed or retyped column) rejects the INSERTs. Already-extracted "
        f"data is not lost: {parked} finished batch(es) are parked in "
        f"{os.path.basename(deferrals.CACHE_PATH)} and will be re-imported automatically "
        "at the end of the next run. Fix the database, then press Run again.")
    hub.emit({"type": "error", "kind": "db_down", "label": "MAJOR — DATABASE STALLED",
              "emoji": "🛑", "hint": hint, "where": "database circuit breaker",
              "file": "", "raw": ""})
    log("error", "MAJOR ERROR — " + hint, "db_down")


def _flush_parked(hub: Hub, db: Database, log):
    """End-of-run pass: import every batch the main loop had to park."""
    def on_applied(entry):
        if entry.get("source_id"):
            hub.emit({"type": "file", "id": entry["source_id"],
                      "status": "done", "detail": "Parked write imported"})

    applied, still = deferrals.flush(db, log=log, on_applied=on_applied)
    if applied:
        log("success", f"Write-cache imported {applied} parked batch(es).")
    if still:
        log("warn", f"{still} batch(es) still cannot be written — they stay parked in "
                    f"{os.path.basename(deferrals.CACHE_PATH)} and will be retried "
                    "at the end of the next run.")


def _process_item(hub: Hub, cfg: Config, db: Database, item, index, total,
                  pre=None):
    """Runs one file end to end and returns an outcome for the circuit
    breaker: 'clean', 'deferred' (parked writes), 'store_failed', 'failed'
    or 'blocked' (every Gemini key/model pair exhausted — stop the run).

    `pre` (parallel mode only) carries the slow steps a lane already did —
    {"storage_url", "parsed", "candidates", "error"} — so this call performs
    only the ordered DB import + archive on the run thread. None (the serial
    path) reads, uploads and OCRs here exactly as before."""
    provider = (getattr(cfg, "ocr_provider", "gemini") if cfg else None) or "gemini"
    ocr_label = "Gemini" if provider.lower() == "gemini" \
        else (getattr(cfg, "ocr_model", "") or "OCR model")

    def file_event(status, detail=""):
        hub.emit({"type": "file", "id": item.source_id,
                  "status": status, "detail": detail})

    def log(level, message, err=None):
        event = {"type": "log", "level": level, "message": message}
        if err:
            event["err"] = err
        hub.emit(event)

    def on_problem(flt, attempt, tries, wait, where=""):
        route = f" [{where}]" if where else ""
        log("warn", f"  ↳ {flt['emoji']} {flt['label']}{route} · attempt {attempt}/{tries} "
                    f"failed — retrying in {int(wait)}s", flt["kind"])
        # Q8 countdown: the UI counts down the exact slept value, so the
        # stated wait always matches the actual wait by construction.
        hub.emit({"type": "wait", "until": time.time() + wait,
                  "total": wait, "attempt": attempt, "tries": tries,
                  "where": where})

    def on_route(label, masked, model, attempt):
        if attempt > 1:   # first try is the planned path — no news
            file_event("ocr", f"{label} · {model}")
            text = f"⇄ rerouted to {label} ({masked}) · {model}"
            log("info", f"  ↳ {text}")
            hub.emit({"type": "route", "text": text})

    def bump(key, amount=1):
        with hub.lock:
            hub.stats[key] = hub.stats.get(key, 0) + amount
            stats = dict(hub.stats)
        hub.emit({"type": "stats", "stats": stats})

    # Steps that failed their retries and were parked for the end-of-run import.
    parked_steps = []

    class _Stopped(Exception):
        pass

    def check_stop(where=""):
        """Stop between upload→OCR→save→archive phases (criterion 10).

        A run stuck inside the blocking gemini-ocr call still cannot abort
        mid-socket (U2) — but every phase boundary now honours the button,
        and retry sleeps wake early via _sleep_or_stop.
        """
        if hub.stop_flag.is_set():
            log("warn", f"Stop requested — halting during {where or 'processing'}; "
                        f"file {index}/{total} stays for the next run.")
            file_event("queued", "Stop requested")
            raise _Stopped()

    def try_db(op_step, apply_now, where):
        """One Postgres write with growing retries; parks it in the cache and
        returns False when it cannot be applied right now."""
        if parked_steps:
            # The DB already refused this file — don't stall on more timeouts.
            parked_steps.append(op_step)
            log("warn", f"  ↳ {where} parked without retry — this file already "
                        "has a parked write")
            return False
        retries, base = deferrals.save_retries(), deferrals.retry_wait()
        last = None
        for attempt in range(1, retries + 1):
            try:
                apply_now()
            except Exception as exc:
                last = getattr(exc, "fault", None) or faults.classify(exc)
                last["raw"] = last.get("raw") or str(exc)[:400]
                if attempt < retries:
                    wait = faults.backoff_wait(attempt, base)
                    on_problem(last, attempt, retries, wait, where=where)
                    if not _sleep_or_stop(hub, wait):
                        return False
            else:
                return True
        last = last or faults.fault("unknown")
        log("warn", f"  ↳ {last['emoji']} {last['label']} · {where} parked in the "
                    f"write-cache after {retries} attempts — it will be imported "
                    "at the end of the run", last["kind"])
        hub.emit({"type": "error", "kind": last["kind"], "label": last["label"],
                  "emoji": last["emoji"], "hint": last["hint"],
                  "where": f"{where} · parked to write-cache · file {index}/{total}",
                  "file": item.rel_path, "raw": str(last.get("raw", ""))[:400]})
        parked_steps.append(op_step)
        return False

    branch = "Question" if item.type == "سوال" else "Answer"
    phase = "reading local file"
    try:
        log("info", f"[{index}/{total}] {branch}: {item.rel_path}")

        if pre is not None:
            # Parallel lane already read the file + uploaded it + OCR'd it.
            storage_url = pre.get("storage_url")
            parsed = pre.get("parsed")
            candidates = pre.get("candidates")
            if storage_url is None:            # the lane's upload failed
                raise pre.get("error") or faults.FaultError(
                    faults.fault("unknown"), "parallel lane produced no upload")
        else:
            with open(item.file_path, "rb") as fh:
                raw = fh.read()
            data_b64 = base64.b64encode(raw).decode()

            # 1) Upload to Supabase storage (object name = source_id).
            phase = "Supabase upload"
            file_event("uploading", "Uploading to Supabase Storage")
            storage_url = upload_file(cfg, item.source_id, raw, item.mime_type,
                                      on_problem=on_problem)
            check_stop("Supabase upload")

        # 2) Upsert the sources row.
        phase = "source upsert"
        file_event("saving", "Storing source row")
        if try_db({"op": "upsert_source", "item": asdict(item), "storage_url": storage_url},
                  lambda: db.upsert_source(item, storage_url), "source upsert"):
            log("info", f"  ↳ source {item.source_id[:12]}… stored")

        if item.type == "سوال":
            # ---------- QUESTION BRANCH ----------
            phase = "OCR"
            file_event("ocr", f"{ocr_label} is extracting questions")
            check_stop("source upsert")
            if pre is not None:
                if parsed is None:             # the lane's OCR failed
                    raise pre.get("error") or faults.FaultError(
                        faults.fault("unknown"), "parallel lane produced no OCR")
            else:
                parsed = _call_ocr(cfg, QUESTION_PROMPT, data_b64, item.mime_type,
                                   on_problem=on_problem, on_route=on_route)
            check_stop("gemini-ocr")
            rows = build_question_rows(item, parsed, storage_url)
            if rows:
                phase = "Postgres save"
                file_event("saving", f"Inserting {len(rows)} question(s)")
                if try_db({"op": "upsert_questions", "rows": rows},
                          lambda: db.upsert_questions(rows), "question save"):
                    bump("questions", len(rows))
                    log("success", f"  ↳ saved {len(rows)} question(s)")
            else:
                log("warn", f"  ↳ Gemini found no questions in {item.file_name}")
        else:
            # ---------- ANSWER BRANCH ----------
            phase = "DB context read"
            file_event("ocr", "Fetching related questions from DB")
            if pre is None:
                candidates = db.fetch_context_questions(item.subject, item.topic, item.grade)
                prompt = build_answer_prompt(candidates)

            phase = "OCR"
            file_event("ocr", f"{ocr_label} is extracting answers")
            check_stop("DB context read")
            if pre is not None:
                if parsed is None:             # the lane's OCR failed
                    raise pre.get("error") or faults.FaultError(
                        faults.fault("unknown"), "parallel lane produced no OCR")
            else:
                parsed = _call_ocr(cfg, prompt, data_b64, item.mime_type,
                                   on_problem=on_problem, on_route=on_route)
            check_stop("gemini-ocr")

            prepared = prepare_answer_rows(parsed)
            rows = finalize_answer_rows(item, prepared, candidates)
            if rows:
                phase = "Postgres save"
                file_event("saving", f"Inserting {len(rows)} answer(s)")
                if try_db({"op": "upsert_answers", "rows": rows},
                          lambda: db.upsert_answers(rows), "answer save"):
                    bump("answers", len(rows))
                    log("success", f"  ↳ saved {len(rows)} answer(s)")
            else:
                log("warn", f"  ↳ Gemini found no answers in {item.file_name}")

        # 3) Move the original file into 'done' (Loop On Success). A file with
        # parked writes is archived too: the Gemini result is safe on disk in
        # the cache, so re-running OCR for it would only burn quota.
        phase = "archiving to done/"
        check_stop("Postgres save")
        _move_to_done(item.file_path)
        if parked_steps:
            deferrals.park(parked_steps, item=item)
            bump("deferred")
            bump("processed")
            file_event("cached", f"{len(parked_steps)} write(s) parked — "
                                 "import at end of run")
            return "deferred"
        file_event("done", "Completed")
        bump("processed")
        return "clean"

    except _Stopped:
        # A manual stop is not an error: the file stays for the next run.
        return "failed"
    except Exception as exc:
        bump("errors")
        flt = getattr(exc, "fault", None) or faults.classify(exc)
        raw = flt.get("raw") or (str(exc) or exc.__class__.__name__)
        hub.emit({"type": "error", "kind": flt["kind"], "label": flt["label"],
                  "emoji": flt["emoji"], "hint": flt["hint"],
                  "where": f"{phase} · file {index}/{total}",
                  "file": item.rel_path, "raw": str(raw)[:400]})
        file_event("failed", f"{flt['emoji']} {flt['label']} — {flt['hint']}")
        # Like n8n, the file is NOT moved on failure, so the next run retries it.
        if flt.get("exhausted"):
            # The router has no key×model pair left — every later file would
            # fail the same way, so tell the loop to stop (Q4).
            return "blocked"
        # A store-group fault (DB or Supabase) feeds the circuit breaker too.
        return "store_failed" if flt.get("group") == "store" else "failed"


def blocked_remaining(total, index):
    """Files the run did not process when it stopped at `index` (1-based):
    the failing file itself plus the rest of the queue."""
    return max(0, total - index + 1)


def _move_to_done(file_path: str):
    done_dir = os.path.join(os.path.dirname(file_path), "done")
    os.makedirs(done_dir, exist_ok=True)
    destination = os.path.join(done_dir, os.path.basename(file_path))
    if os.path.exists(file_path):
        os.replace(file_path, destination)
