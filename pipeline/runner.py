import asyncio
import base64
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime

from . import deferrals, faults, parallel, stage
from .answers import build_answer_prompt
from .config import Config
from .db import Database
from .gemini import proxy_proxies, set_proxy
from .gemini_router import router, mask
from .questions import pages_prompt, question_prompt, split_pages
from .scanner import scan
from .staging import DEFAULT_PATH as STAGING_DEFAULT_PATH, StagingStore
from .vision import call_ocr_openai


def _call_ocr(cfg, prompt, data_b64, mime_type, on_problem=None, on_route=None,
              key_pin=None, labels=None):
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
                                 on_problem=on_problem, on_route=on_route,
                                 labels=labels)
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
            on_problem=on_problem, on_route=on_route, labels=labels)
        return parsed
    parsed, _raw, _route = router.call(
        cfg, prompt, data_b64, mime_type,
        on_problem=on_problem, on_route=on_route, labels=labels)
    return parsed

def _open_store(cfg):
    """The staging store for this run (local SQLite; never the live DB)."""
    path = getattr(cfg, "staging_path", None) or STAGING_DEFAULT_PATH
    return StagingStore(path)


def missing_credentials(cfg):
    """Pre-flight gate — the env names a run's active providers need.
    A switched-off section reports its own flag, so the log names the toggle
    that turned the run down (Q1)."""
    missing = []
    # No bucket credential is checked: since P3 a run only writes the local
    # staging store, so SUPABASE_SERVICE_KEY is the importer business. A
    # staging run must be possible with no live access at all.
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
            try:
                asyncio.run_coroutine_threadsafe(self._broadcast(event), self.loop)
            except RuntimeError:
                # A closed loop (a test's own asyncio.run, a torn-down worker)
                # must not fail the endpoint that emitted the event: the event
                # is already recorded in the log, the broadcast is best-effort.
                pass

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


def _run_log_path(cfg):
    """Where a run writes its own trace, so a dead process still leaves one.

    The dashboard log lives in an in-memory deque and a WebSocket push, so a
    crash, a hard kill or a Stop loses the whole run's history — which is
    exactly the moment the history is needed. This file is appended as the run
    goes, one JSON object per line, in the repo's logs/ directory.
    """
    base = os.path.join(getattr(cfg, "input_root", None) or ".", "..", "logs")
    base = os.path.abspath(base)
    try:
        os.makedirs(base, exist_ok=True)
    except OSError:
        base = tempfile.gettempdir()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.join(base, f"run-{stamp}.log")


def _run(hub: Hub, cfg: Config, db: Database, options=None):
    options = options or {}
    log_path = _run_log_path(cfg)

    def write(level, message, err=None, **extra):
        """Append one line to the run file. Never raises: logging is not worth
        failing a run over, and a broken log must not hide the work."""
        try:
            row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "level": level,
                   "message": message}
            if err:
                row["err"] = err
            row.update(extra)
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:                      # noqa: BLE001 - see docstring
            pass

    def log(level, message, err=None):
        event = {"type": "log", "level": level, "message": message}
        if err:
            event["err"] = err
        write(level, message, err)
        hub.emit(event)

    def diag(flt, where, source=""):
        hub.emit({"type": "error", "kind": flt["kind"], "label": flt["label"],
                  "emoji": flt["emoji"], "hint": flt["hint"], "where": where,
                  "file": source, "raw": flt.get("raw", "") or ""})

    try:
        hub.emit({"type": "run_started"})
        write("info", "run started", path=log_path)
        log("info", f"Run log: {log_path}")

        # --- pre-flight checks -------------------------------------
        set_proxy(cfg)                # profile lane for Google-bound calls
        missing = missing_credentials(cfg)
        if missing:
            log("error", "Missing credentials in .env: " + ", ".join(missing), "bad_key")
            return
        # The Postgres ping is informational now: a question file needs no
        # live row at all, and an answer file reads the bank only as a bonus
        # (stage.candidate_questions swallows a dead pooler). So a dead DB
        # WARNS and the run stages anyway - only the importer cares.
        try:
            db.ping()
        except Exception as exc:
            flt = faults.classify(exc)
            flt["raw"] = str(exc)[:400]
            diag(flt, "preflight · Postgres ping")
            log("warn", f"Postgres is unreachable ({flt['label']}) — staging "
                        "continues; the importer will need it later.", flt["kind"])
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
                # Google's edge refused the route before any key was checked,
                # so every key row looks identical: that is the tunnel / exit
                # IP, never the pool.
                edge_only = bool(report["keys"]) and all(
                    (r["fault"] or {}).get("kind") in ("edge_block", "geo_block")
                    for r in report["keys"] if r["fault"])
                lane = proxy_proxies()
                lane = lane["https"] if lane else "direct (no proxy lane)"
                if edge_only:
                    log("error", "Google refused this route before checking any key "
                                 "(its own 403 page, not the API) — fix the tunnel, "
                                 f"not the keys. Lane: {lane}.", "edge_block")
                elif net_only:
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
        write("error", f"run aborted: {exc}", flt.get("kind"),
              traceback=traceback.format_exc()[-2000:])
        log("error", f"Run aborted — {flt['label']}: {exc}", flt["kind"])
    finally:
        # A run that ends without this line died mid-flight (crash, kill,
        # OOM) - which is precisely what the file exists to reveal.
        write("info", "run finished", stats=dict(hub.stats))
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
    # Pages per call: OCR the page images of one paper together before the
    # per-file loop, then hand each file its own slice of that one reply. A
    # paper that arrives as one image per file (the converter's usual output)
    # is where the whole saving lives; a single file still batches inside
    # itself through _ocr_pages.
    per_call = max(1, int(getattr(cfg, "ocr_pages_per_call", 1) or 1))
    prefetched = _page_ocr_prepass(hub, cfg, db, items, per_call, log) \
        if per_call > 1 else {}
    # Staging: one local SQLite store for this run. Nothing this loop does
    # touches Postgres or the bucket - the importer does that later, after a
    # human has marked the rows ready in Inspect.
    store = _open_store(cfg)
    for index, item in enumerate(items, 1):
        if hub.stop_flag.is_set():
            stopped = True
            log("warn", f"Stop requested — halting after file {index - 1} of "
                        f"{len(items)}; the remaining files stay for the next run.")
            break
        outcome = _process_item(hub, cfg, db, item, index, len(items),
                                pre=prefetched.get(item.source_id),
                                store=store)
        if outcome in ("clean", "empty"):
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


def _produce_round(cfg, db, assignments, hub=None, on_bundle=None):
    """Phase A+B of one parallel round.

    Phase A (this thread, file order): read each file's bytes and build its
    OCR prompt — the answer branch reads its DB context here, so the single
    Postgres connection is never touched off-thread.

    Phase B (small thread pool): the slow part - one OCR call per page with
    the lane's PINNED key. No DB write happens here.

    `hub` (parallel runs) receives `lane` events WHILE the pool works — the
    dashboard's parallel readout draws from these. None keeps the
    unit-test/serial paths quiet.

    `on_bundle`, when given, is called for each bundle the instant its lane
    finishes (completion order), on THIS thread — so the caller can import it
    without waiting for the round's slowest lane, while every DB write still
    happens on the one run thread. None (serial / unit tests) keeps the old
    "return everything, import after" shape.

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
            bundle["pages"] = stage.page_images(raw, item.mime_type)
            if item.type == "سوال":
                bundle["candidates"] = None
            else:
                # The prompt itself is built per page-batch by _ocr_pages;
                # only the answer branch's candidate bank is read here, on
                # this thread, so the single DB connection is never touched
                # off-thread.
                bundle["candidates"] = db.fetch_context_questions(
                    item.subject, item.topic, item.grade)
        except Exception as exc:
            bundle["error"] = exc
        prepared.append(bundle)

    def lane(state, bundle, **extra):
        _lane_event(hub, cfg, bundle, state, **extra)

    def produce(bundle):
        def lane_problem(flt, attempt, tries, wait, where=""):
            # Backoff → the lane says what it is waiting on, and carries the
            # deadline so the dashboard draws an exact countdown ring. `where`
            # defaults so the 4-arg callers (gemini.py / storage.py / vision.py)
            # work too.
            lane("wait", bundle, phase=f"waiting {int(wait)}s",
                 until=time.time() + wait, total=wait)

        lane("start", bundle, phase="ocr")    # the lane picked this file up
        if bundle.get("error"):
            lane("failed", bundle, phase="failed",
                 reason=str(bundle["error"])[:160])
            return bundle
        try:
            item = bundle["item"]
            lane("start", bundle, phase="ocr")
            # `_ocr_pages` owns the "Pages / call" batching, so a lane sends
            # the SAME request shape as the one-by-one engine — including the
            # per-page labels and the split back onto each page.
            bundle["parsed"] = _ocr_pages(
                cfg, item, bundle["pages"],
                questions=(item.type == "سوال"),
                candidates=bundle["candidates"],
                on_problem=lane_problem,
                on_route=lambda label, masked, model, attempt: lane(
                    "route", bundle, model=model, attempt=attempt,
                    phase="ocr"),
                key_pin=bundle["key"])
        except Exception as exc:
            bundle["error"] = exc
            lane("failed", bundle, phase="failed", reason=str(exc)[:160])
            return bundle
        lane("done", bundle, phase="staging")
        return bundle

    with ThreadPoolExecutor(max_workers=max(1, len(prepared))) as pool:
        futures = [pool.submit(produce, b) for b in prepared]
        for fut in as_completed(futures):
            bundle = fut.result()     # produce() never raises — it stores errors
            if on_bundle is not None:
                on_bundle(bundle)     # completion order, on the caller's thread
    return {b["index0"]: b for b in prepared}


def _run_parallel(hub: Hub, cfg: Config, db: Database, items, options, log):
    """Round-based parallel run.

    Q1=A: N lanes, each PINNED to one healthy key (one file per lane per
    round, a round barrier before the next round). Q2=A: each lane's file is
    imported the instant its OCR returns — COMPLETION order — on this thread,
    so the DB connection stays single (nothing writes it from a lane) while a
    fast key never waits on a slow one. Q3=B: a file whose lane has no usable
    model left stays for the next run (its lane is dropped for the rest of
    the run)."""
    plan = router._plan(cfg, time.time())
    lanes = parallel.lane_keys(cfg, options.get("enabled_keys"), plan=plan)
    if not lanes:
        log("warn", "Parallel mode: no healthy key has a free lane — "
                    "running one-by-one this run.")
        _run_serial(hub, cfg, db, items, log)
        return
    log("info", f"Parallel mode: {len(lanes)} lane(s) — one file per key per "
                f"round over {len(items)} file(s); each file imports the moment "
                "its lane finishes.")

    # Staging is where a lane's result lands; a lane never writes it (the
    # import step below does, on this thread, in completion order).
    store = _open_store(cfg)
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
            exhausted = set()
            imported = 0

            def import_bundle(bundle):
                """Import ONE finished lane's bundle on THIS (run) thread — the
                instant the lane's OCR returns, not after the round's slowest
                lane. The DB connection stays single because this runs on the
                run thread (never from a pool worker)."""
                nonlocal imported, stopped
                if stopped:
                    return                      # breaker tripped — drop the rest
                index = bundle["index0"] + 1
                pre = {"parsed": bundle.get("parsed"),
                       "candidates": bundle.get("candidates"),
                       "pages": bundle.get("pages"),
                       "error": bundle.get("error")}
                outcome = _process_item(hub, cfg, db, bundle["item"], index,
                                        len(items), pre=pre, store=store)
                ok = outcome in ("clean", "empty")      # both reach done/
                extra = {"phase": "staged" if ok else "not staged"}
                if not ok and bundle.get("error"):
                    extra["reason"] = str(bundle["error"])[:160]
                _lane_event(hub, cfg, bundle, "done" if ok else "failed",
                            **extra)
                imported += 1
                if outcome in ("clean", "empty"):
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

            _produce_round(cfg, db, assignments, hub=hub,
                           on_bundle=import_bundle)

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
                  pre=None, store=None):
    """Runs one file end to end and returns an outcome for the circuit
    breaker: 'clean', 'empty', 'failed' or 'blocked' (every Gemini key/model
    pair exhausted — stop the run).

    Since P3 this function WRITES NOTHING live: no bucket upload, no Postgres
    row. The OCR reply becomes local staging rows (``pipeline.stage``), an
    ``empty`` extraction is reported as such instead of silently archived, and
    the source file moves to ``done/`` because the staged result — not a DB
    transaction — is now the durable artifact.

    `pre` (parallel mode only) carries the slow steps a lane already did —
    {"parsed", "candidates", "pages", "error"}; None means the serial path,
    which reads, renders and OCRs here."""
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

    class _Stopped(Exception):
        pass

    def check_stop(where=""):
        """Stop between read→render→OCR→stage→archive phases (criterion 10).

        A run stuck inside the blocking OCR call still cannot abort
        mid-socket (U2) — but every phase boundary honours the button, and
        retry sleeps wake early via _sleep_or_stop.
        """
        if hub.stop_flag.is_set():
            log("warn", f"Stop requested — halting during {where or 'processing'}; "
                        f"file {index}/{total} stays for the next run.")
            file_event("queued", "Stop requested")
            raise _Stopped()

    branch = "Question" if item.type == "سوال" else "Answer"
    phase = "reading local file"
    try:
        log("info", f"[{index}/{total}] {branch}: {item.rel_path}")

        parsed = pre.get("parsed") if pre is not None else None
        candidates = pre.get("candidates") if pre is not None else None
        pages_data = pre.get("pages") if pre is not None else None
        if pre is not None and parsed is None:
            raise pre.get("error") or faults.FaultError(
                faults.fault("unknown"), "parallel lane produced no OCR")

        # 1) Render the pages locally — one image per PDF page, so a bbox is
        # always measured in the frame of the page it came from.
        if pages_data is None:
            with open(item.file_path, "rb") as fh:
                raw = fh.read()
            phase = "page render"
            file_event("preparing", "Rendering pages locally")
            pages_data = stage.page_images(raw, item.mime_type,
                                           cfg.ocr_pages_per_call or None)
            check_stop("page render")

        if parsed is None:
            # 2) OCR. Page runs already read the file in one call (prefetch);
            # one image per page keeps each call small and each bbox true.
            phase = "OCR"
            file_event("ocr", f"{ocr_label} is extracting {branch.lower()}s")
            if item.type == "سوال":
                parsed = _ocr_pages(cfg, item, pages_data, questions=True,
                                    on_problem=on_problem, on_route=on_route)
            else:
                phase = "candidate read"
                file_event("ocr", "Reading the question bank")
                candidates = stage.candidate_questions(
                    store, db, item.subject, item.topic, item.grade)
                phase = "OCR"
                parsed = _ocr_pages(cfg, item, pages_data, questions=False,
                                    candidates=candidates,
                                    on_problem=on_problem, on_route=on_route)
            check_stop("OCR")

        # 3) Stage locally. This is the only write this file performs.
        phase = "staging"
        file_event("saving", "Staging the result locally")
        staged = stage.stage_file(store, item, parsed, pages_data=pages_data,
                                  candidates=candidates, db=db)
        check_stop("staging")

        if staged.question_ids and item.type == "سوال":
            bump("questions", len(staged.question_ids))
        if staged.answer_ids:
            bump("answers", len(staged.answer_ids))
        # Inspect is a LIVE surface: each file that lands announces its new
        # row counts, so the tab updates the moment the store does instead of
        # only on the next visit. Same hub, same push path as `stats`.
        hub.emit({"type": "staged", "id": item.source_id,
                  "questions": len(staged.question_ids),
                  "answers": len(staged.answer_ids),
                  "crops": staged.crop_files, "pages": staged.pages,
                  "empty": bool(staged.empty_records)})
        if staged.empty_records:
            names = ", ".join(sorted({kind for kind, _ in staged.empty_records}))
            log("warn", f"  ↳ nothing was read from {item.file_name} "
                        f"(staged as empty: {names}) — it needs a look in Inspect",
                "empty_text")
        else:
            log("success", f"  ↳ staged {len(staged.question_ids)} question(s), "
                           f"{len(staged.answer_ids)} answer(s) · "
                           f"{staged.crop_files} crop(s) · {staged.pages} page(s)")

        # 4) Archive the source. Staging is the durable artifact now, so even
        # an empty extraction is archived — re-running OCR would only burn
        # quota, and the empty row is what the human needs to see.
        phase = "archiving to done/"
        _move_to_done(item.file_path)
        if staged.empty_records:
            # The final word stays "empty": a later "done" would hide the
            # one row a human has to look at.
            file_event("empty", "Staged as empty - awaiting review in Inspect")
        else:
            file_event("done", "Staged - awaiting review in Inspect")
        bump("processed")
        return "empty" if staged.empty_records else "clean"

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
        # A store-group fault feeds the breaker too. Since P3 the "store" is
        # the LOCAL staging disk: an unreadable image, an unwritable staging
        # store or a closed store is the new "database is down" - stop the run
        # rather than burn OCR calls on results that cannot be kept.
        if flt.get("group") == "store" or isinstance(
                exc, (OSError, sqlite3.Error, ValueError)):
            return "store_failed"
        return "failed"

# ── pages per call, ACROSS FILES ───────────────────────────────────────────
# The converter splits one paper into <base>-1.jpg, <base>-2.jpg, ... so a
# folder of 84 scans is 84 FILES of ONE paper. "Pages / call" has to group
# those files, or it can never do anything: a file that holds a single image
# cannot batch inside itself. A run never crosses a folder, a type or a paper,
# and the files sort as -1, -10, -2, so a run is collected by base name and
# only then put back in reading order. Every page still gets its own source
# row, its own staged rows and its own archive move.
_PAGE_SUFFIX = re.compile(r"^(?P<base>.+)-(?P<page>\d+)$")


def _page_of(item):
    """(paper base name, page number) from a file name, or (None, None)."""
    stem = os.path.splitext(item.file_name or "")[0]
    match = _PAGE_SUFFIX.match(stem)
    if not match:
        return None, None
    return match.group("base"), int(match.group("page"))


def _page_key(item):
    """The run a page belongs to — None for anything that is not a page image."""
    base, _page = _page_of(item)
    if base is None or not (item.mime_type or "").lower().startswith("image/"):
        return None
    return (os.path.dirname(item.file_path or "").lower(), item.type, base)


def page_runs(items, per_call):
    """Split the queue into runs of CONSECUTIVE page images of one paper.

    Contiguity matters: a run is put back in reading order, so grouping page 20
    with page 2 would hand the model an out-of-sequence pair. A page number
    that does not continue the current run starts a new one."""
    if per_call <= 1:
        return [[item] for item in items]
    runs, current, key, last_page = [], [], None, None
    for item in items:
        item_key = _page_key(item)
        if item_key is None:
            if current:
                runs.append(current)
            current, key, last_page = [], None, None
            runs.append([item])
            continue
        page = _page_of(item)[1]
        if current and (item_key != key or len(current) >= per_call
                        or page != last_page + 1):
            runs.append(current)
            current = []
        if not current:
            key = item_key
        current.append(item)
        last_page = page
    if current:
        runs.append(current)
    return [sorted(run, key=lambda it: _page_of(it)[1]) for run in runs]


def _page_ocr_prepass(hub, cfg, db, items, per_call, log):
    """OCR each run of page images in ONE call -> {source_id: pre}.

    Each file is handed back its own slice of the reply as a `pre` dict —
    ``{"parsed": {questions|answers: [...], "page": n}, "candidates": ...}`` —
    which `_process_item` already accepts from the parallel lanes. The file's
    own pages are still rendered and staged normally, so bbox frames stay
    true.

    Errors are swallowed on purpose: a batch that fails only costs one wasted
    call, and its files then take the normal one-picture path with the usual
    fault reporting, blocking and retry behaviour."""
    runs = [run for run in page_runs(items, per_call) if len(run) > 1]
    if not runs:
        return {}
    covered = sum(len(run) for run in runs)
    log("info", f"Pages per call: {per_call} — {len(runs)} batch(es) for "
                f"{covered} of {len(items)} file(s)")
    prefetched = {}
    for run in runs:
        if hub is not None and hub.stop_flag.is_set():
            break
        pages = [_page_of(item)[1] for item in run]
        blobs, labels, inline = [], [], []
        for item in run:
            try:
                with open(item.file_path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                blobs = []
                break
            inline.append((item, raw))
            labels.append("PAGE %d:" % (_page_of(item)[1]))
            blobs.append(base64.b64encode(raw).decode())
        if blobs:
            # The page index is the LABEL's business; the files come from
            # `inline`, which keeps each item beside its own bytes.
            pages = [_page_of(item)[1] for item in run]
        if not blobs:
            log("warn", f"  ↳ page batch {pages} skipped — a page file could "
                        "not be read")
            continue
        is_question = run[0].type == "سوال"
        field = "questions" if is_question else "answers"
        candidates = None
        try:
            if is_question:
                parsed = _call_ocr(cfg, pages_prompt(), blobs,
                                   run[0].mime_type, labels=labels)
            else:
                candidates = db.fetch_context_questions(
                    run[0].subject, run[0].topic, run[0].grade)
                parsed = _call_ocr(
                    cfg, build_answer_prompt(candidates, pages=len(run)),
                    blobs, run[0].mime_type, labels=labels)
            split = split_pages(parsed, pages, key=field)
            # A model that ignores the page schema hands back rows we cannot
            # attribute: every page reads empty. Staging that would file page
            # 4's rows on page 3's source and archive page 4 with nothing —
            # wrong data that looks like success. The batch is dropped here
            # instead, so each file of the run takes its own call (below) and
            # every row lands on the page it was printed on.
            empty = [p for p in pages if not (split.get(p) or {}).get(field)]
            if len(empty) == len(pages):
                log("warn", f"  ↳ page batch {pages} could not be attributed "
                            "to its pages — reading each page on its own "
                            "instead", "bad_page_label")
                continue
            if len(empty) * 2 > len(pages):
                # A real page can be blank, so a stray blank page is fine; a
                # majority blank means the reply is not trustworthy, and the
                # per-file calls below are the safe answer there too.
                log("warn", f"  ↳ page batch {pages} left page(s) {empty} "
                            "blank — reading each page on its own instead",
                    "bad_page_label")
                continue
        except Exception as exc:               # noqa: BLE001 - never fatal
            flt = getattr(exc, "fault", None) or faults.classify(exc)
            log("warn", f"  ↳ page batch {pages} failed ({flt['label']}) — each "
                        "page takes its own call instead", flt.get("kind"))
            continue
        found = 0
        landed = 0
        for item, page in zip(run, pages):
            # The FILE's own image is the frame: staging mirrors it as page 1
            # of that source, whatever page of the paper it is. So the rows
            # are handed over without the paper page number — the index is
            # the log line's business, not the staged frame's.
            entry = dict(split.get(page) or {field: []})
            prefetched[item.source_id] = {"parsed": entry,
                                          "candidates": candidates}
            if entry.get(field):
                landed += 1
            found += len(entry.get(field) or [])
        log("info", f"  ↳ pages {pages[0]}-{pages[-1]} read in one call "
                    f"({found} {field} on {landed} of {len(pages)} page(s))")
    return prefetched


def _page_batches(pages_data, pages_per_call):
    """Split ``[(page_no, jpeg)]`` into consecutive OCR-call windows.

    ``pages_per_call`` is the toolbar's "Pages / call" (`OCR_PAGES_PER_CALL`):
    N=1 is one picture per request; N=2..8 puts that many page images of ONE
    paper into a single request. Windows never straddle a gap in page numbers,
    so a batch always describes a contiguous run of pages."""
    size = max(1, int(pages_per_call or 1))
    window = []
    for page_no, data in pages_data:
        if window and (len(window) >= size
                       or page_no != window[-1][0] + 1):
            yield window
            window = []
        window.append((page_no, data))
    if window:
        yield window


def _ocr_pages(cfg, item, pages_data, *, questions, candidates=None,
               on_problem=None, on_route=None, key_pin=None):
    """OCR the rendered pages of one file in "Pages / call" batches.

    A batch of N>1 pages rides ONE request: the page images are sent with a
    ``PAGE n:`` label before each, under the pages prompt, which asks for the
    reply grouped per page. ``questions.split_pages`` then puts every entry
    back on the page it was printed on, so each row keeps a ``page`` and every
    ``diagram_bbox`` stays in the frame of its own page.

    A single-page batch keeps the classic one-picture prompt and shape."""
    merged, key = {}, "questions" if questions else "answers"
    merged[key] = []
    pages_per_call = max(1, int(getattr(cfg, "ocr_pages_per_call", 1) or 1))
    batches = list(_page_batches(pages_data, pages_per_call))

    def absorb(parsed, pages):
        """Fold one parsed reply into `merged`, attributing it to its pages."""
        if not isinstance(parsed, dict):
            return
        if len(pages) == 1:
            # One page in the request. The page prompt asks such a call to
            # answer under the same "pages" container as a batch, so read the
            # reply both ways: the classic flat shape and the page dict.
            block = parsed
            if not isinstance(parsed.get(key), list):
                for page_block in split_pages(parsed, pages, key=key).values():
                    if isinstance(page_block.get(key), list):
                        block = page_block
                        break
            by_page = {pages[0]: block}
        else:
            by_page = split_pages(parsed, pages, key=key)
            # A small model sometimes answers a whole batch flat instead of
            # grouping it under "pages". split_pages cannot attribute those
            # rows, so it returns every page empty and the batch would be
            # lost. Keeping the reply beats dropping it: the rows carry no
            # reliable page, so their bbox stays untrustworthy and they land
            # on the batch's first page for a human in Inspect to place.
            if all(not (by_page.get(p) or {}).get(key) for p in pages):
                flat = parsed.get(key)
                if isinstance(flat, list) and any(
                        isinstance(e, dict) for e in flat):
                    by_page = {pages[0]: parsed}
        for page_no in pages:
            block = by_page.get(page_no) or {}
            entries = block.get(key)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict):
                        entry.setdefault("page", page_no)
                        merged[key].append(entry)
            for field, value in block.items():
                if field != key and value not in (None, "", []):
                    merged[field] = value

    for batch in batches:
        page_nos = [page_no for page_no, _ in batch]
        if len(batch) == 1:
            prompt = question_prompt() if questions else build_answer_prompt(
                candidates)
            blobs = stage.b64(batch[0][1])
            labels = None
        else:
            prompt = (pages_prompt() if questions
                      else build_answer_prompt(candidates, pages=len(batch)))
            blobs = [stage.b64(data) for _page_no, data in batch]
            labels = [f"PAGE {page_no}:" for page_no in page_nos]
        parsed = _call_ocr(cfg, prompt, blobs, "image/jpeg",
                           on_problem=on_problem, on_route=on_route,
                           labels=labels, key_pin=key_pin)
        absorb(parsed, page_nos)

    merged["pages_read"] = len(pages_data)
    merged["ocr_calls"] = len(batches)
    return merged
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
