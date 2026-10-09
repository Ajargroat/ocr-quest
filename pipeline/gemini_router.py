"""Key × model routing for Gemini, built to spend the fewest possible calls.

The production problem
----------------------
Several Gemini keys, several model versions (3.5 → 3.8 flash), and any of
them can be unavailable at any moment: a key whose free quota ran out, a
model version not yet rolled out to one of the Google accounts, a key that
got revoked. The pipeline must find a working combination by itself.

Why this is cheap
-----------------
The naive design "probe every key, then every model, then work" costs
one billed request per key×model pair *per run* for no benefit — the real
request would have answered the same question. So:

* **The first real request is the probe.** `call()` sends actual work to
  its best guess and only rotates when that guess fails. A healthy setup
  therefore spends exactly one API call per file: zero probing overhead.
* **Health checks never touch a model.** A keyless probe of
  googleapis.com separates "my tunnel/region is the problem" from "this
  key is the problem", then ONE free `list_models` call per key (checked
  one by one, in order) proves liveness and which ladder rungs Google
  even shows that key. Generation tokens are never spent on a check; a
  model is only ever judged when real traffic hits an error with it.
* **Outcomes are cached** (key dead, key rate-limited, model unavailable
  for *this* key, model spent its daily quota), so a known-bad
  combination is never paid for twice.
* **Faults are split into "rotation" and "abort" classes.** A dead key or
  an unsupported model is specific to that pair, so the router rotates.
  A broken tunnel, a geo-blocked exit IP or a Google 5xx is not — it hits
  every key exactly the same way, so trying nine more keys would burn nine
  300-second timeouts and change nothing. Those raise immediately.
* **Daily successful sends are counted per key × model** and persisted to
  a small JSON file (keyed by a hash, never the secret), so the
  Credentials tab can draw a filling green→red bar for every model and
  the router steps over models that have spent their day — Google has no
  public "quota remaining" endpoint for free-tier keys, so counting the
  sends that actually succeeded is the honest source of truth. A real
  RESOURCE_EXHAUSTED answer parks that pair for the cooldown instead —
  failures never inflate the gauge.
* **Last-known-good is sticky.** Once a pair works, that model is tried
  first for that key from then on, which keeps the ladder walk at one hop.
"""
import hashlib
import json
import os
import threading
import time
from datetime import date, datetime, timezone

from . import faults
from .gemini import call_gemini, gateway_probe, list_models

# A key Google said is dead/finished is worth re-testing only rarely —
# quota resets are daily and revocations are permanent.
KEY_COOLDOWN = {"bad_key": 900, "quota": 3600, "geo_block": 300}
# 429s are per-minute windows; a short cooldown clears them.
RATE_LIMIT_COOLDOWN = 75
# Model not available for this key: retried on the next day boundary, or
# on demand from the Credentials tab, whichever comes first.
MODEL_COOLDOWN = 6 * 3600
# Free-tier daily request cap per key × model when Google does not tell us
# better (it never does — there is no such endpoint for plain API keys).
# GEMINI_MODEL_DAY_LIMIT in .env tunes it.
DAY_LIMIT_DEFAULT = 20
# Soft penalty window for key-level faults (bad key, rate limit,
# geo block). Deliberately separate from the hard cooldown: once
# that expires the key is eligible again, but a fresh blip must
# not put it straight back at the front of the rotation — every
# unpenalized healthy key is tried before it is.
PENALTY_WINDOW = 10 * 60
# Exponential-moving-average weight for the per-key latency
# gauge (0..1; higher tracks recent latency more closely).
LATENCY_ALPHA = 0.3
# A latency edge only counts when a key is at least this many
# times slower than the fastest healthy key — sub-millisecond
# measurement noise must never move the rotation.
LATENCY_RATIO = 2.0
# …AND slower by at least this many milliseconds. The ratio alone
# fires on jitter (0.1 ms vs 0.04 ms is 2.5×), which would let
# clock noise steal the rotation order from the round-robin.
LATENCY_MIN_GAP = 100.0
# How far a real latency edge may push a key back in the
# rotation order (in pool slots, fractional). Bounded well
# under one-and-a-half slots so a slow key can fall behind its
# rotation neighbours but a fast one can never take every file:
# the round-robin stays the backbone, latency only refines it.
LATENCY_WINDOW = 1.2
# Survives restarts so the bars do not reset every time the server does.
USAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".gemini-usage.json")
# Per-call history beside the usage gauge (Q4: no schema change). Local-only,
# never committed — feeds the credentials usage reports + period stats.
# Derived from USAGE_PATH at call time (not import time) so tests that
# redirect USAGE_PATH to a tmp dir automatically isolate history too.
CALLS_PATH = os.path.join(os.path.dirname(USAGE_PATH), ".gemini-calls.json")


def _calls_path():
    return os.path.join(os.path.dirname(USAGE_PATH), ".gemini-calls.json")


def _key_meta_path():
    """When each key was first saved — beside the usage gauge, derived at call
    time like _calls_path so tests that redirect USAGE_PATH isolate it too."""
    return os.path.join(os.path.dirname(USAGE_PATH), ".gemini-key-meta.json")
# History growth bound: oldest entries are dropped past this.
CALLS_CAP = 20000
# Preset report windows (Q11) in hours; "all" means no cutoff.
PERIOD_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def _next_utc_midnight_ts(now=None):
    """Epoch seconds of the next UTC midnight — the quota refill moment (Q7).

    A free-quota-exhausted model is parked until exactly then (Q6), never
    retried the same day, so one exhausted model cannot chain errors."""
    now = now if now is not None else time.time()
    day = datetime.fromtimestamp(now, timezone.utc).date()
    midnight = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return midnight.timestamp() + 24 * 3600

# Faults that mean "this key/pair is the problem" → rotate.
ROTATE_KINDS = {"bad_key", "quota", "rate_limit", "model_unavailable",
                "geo_block"}
# Which of those are per MODEL (free-tier day quota is per model, and so is
# a 404 rollout gap, and so is a per-key region refusal) → park the PAIR and
# step up the ladder. The rest are per key → next key.
MODEL_LEVEL_ROTATIONS = {"quota", "model_unavailable", "geo_block"}
# Faults that mean "the whole path is the problem" → abort, rotating only
# multiplies the timeout we already waited.
GLOBAL_KINDS = {"tunnel_down", "send_blocked", "recv_dropped", "overload",
                "db_down", "storage_fail", "local_io"}
# Quality hiccups: worth one more model in the same call, but not cached.
MODEL_RETRY_KINDS = {"model_empty", "model_garbage"}

# Which side of the connection a fault blames — the Credentials tab colours
# each key row with this: my tunnel, Google itself, the key, the quota…
FAULT_CAUSE = {
    "tunnel_down": "tunnel", "send_blocked": "tunnel", "recv_dropped": "tunnel",
    "overload": "google", "bad_key": "auth", "geo_block": "region",
    "quota": "quota", "rate_limit": "rate",
    "model_unavailable": "model", "model_empty": "model",
    "model_garbage": "model", "db_down": "storage", "storage_fail": "storage",
    "local_io": "local", "unknown": "other",
}

def mask(key: str, keep: int = 4) -> str:
    """`AIzaSyD-1234567890abcdef` → `AIza…cdef`. Enough to recognise a key,
    never enough to use it."""
    if not key:
        return ""
    if len(key) <= keep * 2:
        return key[0] + "…"
    return f"{key[:keep]}…{key[-keep:]}"


def usage_chart(rows, group="model", y="calls"):
    """Day × series buckets for the Usage tab curve (Q2/Q3).

    group: 'model' | 'key'  — what one colour stands for.
    y:     'calls' | 'tokens' | 'sec' | 'avg_sec' — what the bar height
           counts (tokens = tokens_in + tokens_out of the call; sec = summed
           per-call delay in the cell; avg_sec = delay ÷ calls in the cell).
    Days are UTC, matching the "Time (UTC)" column of the table.
    Returns {"group", "y", "days": [iso…], "series": [label…],
             "cells": [[value, …], …]} where cells[day][series]."""
    group = group if group in ("model", "key") else "model"
    y = y if y in ("calls", "tokens", "sec", "avg_sec") else "calls"
    days, series, counts, calls_n = [], [], {}, {}
    for r in rows or []:
        day = datetime.fromtimestamp(r.get("ts") or 0,
                                     timezone.utc).strftime("%Y-%m-%d")
        if group == "model":
            label = r.get("model") or "unknown"
        else:
            label = (r.get("key_name") or r.get("key_masked")
                     or r.get("key_hash") or "unknown")
        if y == "calls":
            value = 1
        elif y == "tokens":
            value = int(r.get("tokens_in") or 0) + int(r.get("tokens_out") or 0)
        else:
            value = float(r.get("ms") or 0.0) / 1000.0
        if day not in counts:
            counts[day] = {}
            days.append(day)
        if label not in counts[day]:
            counts[day][label] = 0
            if label not in series:
                series.append(label)
        counts[day][label] += value
        if y == "avg_sec":
            calls_n.setdefault(day, {})
            calls_n[day][label] = calls_n[day].get(label, 0) + 1
    days.sort()
    totals = {s: sum(counts[d].get(s, 0) for d in days) for s in series}
    series.sort(key=lambda s: (-totals[s], s))     # biggest series first
    if y == "avg_sec":
        # divide each day×series cell by its own call count (never 0 → no div
        # error); round to 1 decimal so the axis stays readable.
        cells = [[round(counts[d].get(s, 0) / calls_n.get(d, {}).get(s, 1), 1)
                  for s in series] for d in days]
    else:
        cells = [[counts[d].get(s, 0) for s in series] for d in days]
    return {"group": group, "y": y, "days": days, "series": series,
            "cells": cells}


class GeminiRouter:
    """Thread-safe router over the key pool and model ladder in a Config."""

    # Set by the runner to fingerprint (keys, ladder) already health-checked.
    preflight_for: object = None

    def __init__(self):
        self._lock = threading.RLock()
        # key value → state dict. Keys are their own identity, so editing
        # .env needs no cache invalidation.
        self._state = {}
        self.events = []            # ring of route log lines for the dashboard
        self.preflight_for = None   # pool fingerprint already health-checked
        self._gateway = (0.0, None)     # (checked_at, probe result) cache
        self._usage_loaded = False
        self._usage = {}                # key-hash → {model: sends today}
        self._usage_day = ""
        # Wise-rotation pointer: the pool position the next plan
        # starts from. Advances one step on every SUCCESS, so
        # healthy keys share the load instead of key #1 taking
        # everything until it dies. Ephemeral by design (a restart
        # or a credentials save starts back at pool order); the
        # persisted daily-send counters are facts and survive it.
        self._rotate = 0

    # ------------------------------------------------------------------ state
    def _entry(self, key):
        with self._lock:
            return self._state.setdefault(key, {
                "dead": False, "unavailable_until": 0.0, "last_kind": "",
                "ok_model": "", "blocked": {}, "probes": 0, "calls": 0,
                "fails": 0, "last_fault": "", "checked_at": 0.0,
                # Wise-rotation state: EWMA latency (ms) and a soft
                # penalty expiry — see PENALTY_WINDOW above.
                "latency": 0.0, "penalty_until": 0.0,
            })

    def names_for(self, cfg):
        """Optional human labels so teammates can tell their own key apart."""
        return list(getattr(cfg, "gemini_key_names", []) or [])

    def _label(self, cfg, index, key):
        names = self.names_for(cfg)
        if index < len(names) and names[index]:
            return names[index]
        return f"Key {index + 1}"

    def _log(self, text):
        stamp = time.strftime("%H:%M:%S")
        with self._lock:
            self.events.append({"ts": stamp, "message": text})
            del self.events[:-200]

    # ------------------------------------------------------ daily send count
    def _today(self):
        # UTC midnight tracks Google's quota refill exactly (Q7) — never the
        # server's local date, and never reset by refresh or errors.
        return datetime.now(timezone.utc).date().isoformat()

    def _key_hash(self, key):
        """Identity for the usage file — a hash, never the secret itself."""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    def _key_meta(self):
        try:
            with open(_key_meta_path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _key_meta_stamp(self, key):
        """First save wins — a hash already stamped keeps its original date."""
        meta = self._key_meta()
        h = self._key_hash(key)
        if h in meta:
            return
        meta[h] = {"added": datetime.now(timezone.utc).isoformat()}
        path = _key_meta_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except OSError:
            pass

    def day_limit(self):
        """Cap of successful sends per key × model per day (free tier is
        ~20; Google sometimes lets a few more through — the bar simply
        overflows past 100% when that happens)."""
        try:
            return max(1, int(os.getenv("GEMINI_MODEL_DAY_LIMIT",
                                        str(DAY_LIMIT_DEFAULT))))
        except ValueError:
            return DAY_LIMIT_DEFAULT

    def _usage_load(self):
        if self._usage_loaded:
            return
        with self._lock:
            if self._usage_loaded:
                return
            self._usage_loaded = True
            self._usage_day = self._today()
            try:
                with open(USAGE_PATH, encoding="utf-8") as fh:
                    saved = json.load(fh)
                if saved.get("day") == self._usage_day:
                    self._usage = {k: dict(v)
                                   for k, v in (saved.get("usage") or {}).items()}
            except (OSError, ValueError):
                self._usage = {}

    def _usage_save(self):
        try:
            tmp = USAGE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"day": self._usage_day, "usage": self._usage}, fh)
            os.replace(tmp, USAGE_PATH)
        except OSError:
            pass          # counters are a gauge, never worth breaking a run

    def _count_send(self, key, model, tokens_in=0, tokens_out=0,
                     ms=0.0, key_name="", ok=True):
        """Record one COMPLETED (successful) send for today.

        Failures never touch this gauge — it counts calls that actually
        finished, so it can never read N/N for a key that did not
        complete N. Every completed call is also appended to the local
        per-call history (CALLS_PATH) backing the usage reports (Q4)."""
        self._usage_load()
        with self._lock:
            today = self._today()
            if today != self._usage_day:
                self._usage, self._usage_day = {}, today
            per = self._usage.setdefault(self._key_hash(key), {})
            per[model] = per.get(model, 0) + 1
            self._usage_save()
        self._calls_append(key, model, tokens_in=tokens_in,
                           tokens_out=tokens_out, ms=ms,
                           key_name=key_name, ok=ok)

    def _calls_load(self):
        """Per-call history rows (newest last), tolerant of corruption."""
        try:
            with open(_calls_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _calls_append(self, key, model, tokens_in=0, tokens_out=0,
                      ms=0.0, key_name="", ok=True):
        """Append one history row atomically; failures are swallowed
        (history is a gauge, never worth breaking a run).

        The read-modify-write of the shared JSON file is serialized under
        the router lock: parallel lanes each call this from their own
        thread, and two unlocked read→append→write cycles would drop rows."""
        with self._lock:
            try:
                rows = self._calls_load()
                rows.append({
                    "ts_utc": datetime.now(timezone.utc).isoformat(),
                    "ts": time.time(),
                    "key_hash": self._key_hash(key),
                    "key_name": key_name,
                    "key_masked": mask(key),
                    "model": model,
                    "tokens_in": int(tokens_in or 0),
                    "tokens_out": int(tokens_out or 0),
                    "ms": round(float(ms or 0.0), 1),
                    "ok": bool(ok),
                })
                del rows[:-CALLS_CAP]
                path = _calls_path()
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(rows, fh)
                os.replace(tmp, path)
            except OSError:
                pass

    def usage_report(self, period="all"):
        """Per-call rows filtered to 24h/7d/30d/all-time chips (Q11).

        Returns {"period":…, "calls":[…], "totals": {calls, tokens_in,
        tokens_out, ms}}. Unknown periods fall back to all-time."""
        hours = PERIOD_HOURS.get(period)
        rows = self._calls_load()
        if hours is not None:
            cutoff = time.time() - hours * 3600
            rows = [r for r in rows if r.get("ts", 0) >= cutoff]
        # 9router is a routing helper, not a model-usage series — keep its rows
        # out of BOTH the chart and the table (item 4.2).
        rows = [r for r in rows if (r.get("key_name") or "") != "9router"]
        totals = {"calls": len(rows),
                  "errors": sum(1 for r in rows if not r.get("ok")),
                  "tokens_in": sum(r.get("tokens_in", 0) for r in rows),
                  "tokens_out": sum(r.get("tokens_out", 0) for r in rows),
                  "ms": round(sum(r.get("ms", 0.0) for r in rows), 1)}
        return {"period": period, "calls": rows, "totals": totals}

    def _usage_of(self, key):
        self._usage_load()
        limit = self.day_limit()
        today = self._today()
        with self._lock:
            if today != self._usage_day:
                return {"day": today, "limit": limit, "models": {}}
            return {"day": self._usage_day, "limit": limit,
                    "models": dict(self._usage.get(self._key_hash(key), {}))}

    def _spent_today(self, key, model):
        u = self._usage_of(key)
        return u["models"].get(model, 0) >= u["limit"]

    # ------------------------------------------------------------- selection
    def _plan(self, cfg, now, keys=None):
        """Ordered [(index, key, [models…])] of what is worth trying.

        Skips keys in cooldown and models already known-unavailable (or
        with today's sends spent) for a key, so nothing is paid for twice.

        `keys` narrows the pool to a subset (the parallel engine's enabled
        keys) while keeping the returned index relative to that subset; a
        None `keys` is the whole `cfg.gemini_key_pool`, so the serial path
        is unchanged.

        The ORDER is the wise part: eligible keys are split into
        unpenalized and softly-penalized tiers, and each tier starts
        from the rotation pointer (which advances on success), with
        the latency EWMA as a tiebreak. A key that just recovered
        from a fault is therefore tried after every clean key, and
        healthy keys share the load instead of key #1 taking every
        file until it dies. Everything is still attempted — the
        rotation only decides WHO is tried first."""
        eligible = []
        with self._lock:
            pool = list(cfg.gemini_key_pool)
            allowed = None if keys is None else set(keys)
            for i, key in enumerate(pool):
                if allowed is not None and key not in allowed:
                    continue
                st = self._state.get(key) or {}
                if st.get("dead") and now < st.get("unavailable_until", 0.0):
                    continue
                blocked = st.get("blocked", {})
                last = st.get("ok_model", "")
                ladder = [m for m in cfg.gemini_model_ladder
                          if now >= blocked.get(m, 0.0)
                          and not self._spent_today(key, m)]
                if not ladder:
                    continue
                # Sticky: start from the model that worked last time, then
                # walk the rest in ladder order.
                if last in ladder:
                    ladder.remove(last)
                    ladder.insert(0, last)
                eligible.append((i, key, ladder, st.get("latency") or 0.0,
                                 st.get("penalty_until") or 0.0))
            rotate = self._rotate % len(pool) if pool else 0

        def order(tier):
            def rank(entry):
                # Position relative to the rotation pointer, wrapping
                # around the pool, minus a bounded latency bonus:
                # a key at least LATENCY_RATIO× slower than the
                # fastest in its tier is pushed back up to
                # LATENCY_WINDOW slots — enough to be demoted within
                # the rotation, never enough to dominate it.
                pos = float((entry[0] - rotate) % len(pool))
                lat = entry[3]
                if lat and len(tier) > 1:
                    fastest = min(
                        (o[3] for o in tier if o[3]), default=None)
                    # Ratio AND an absolute floor: only a genuinely
                    # slower key (not clock jitter) is demoted.
                    if (fastest and fastest > 0
                            and lat >= fastest * LATENCY_RATIO
                            and lat - fastest >= LATENCY_MIN_GAP):
                        pos += LATENCY_WINDOW
                return (pos,)
            return sorted(tier, key=rank)

        clean = [e for e in eligible if now >= e[4]]
        pen = [e for e in eligible if now < e[4]]
        return order(clean) + order(pen)

    def _mark(self, key, kind, model=None, now=None):
        """Record what a failure taught us, with the right cooldown.

        With a model in hand the fault is recorded against that key+model
        PAIR only (`blocked`), so one bad rung never parks the key — with a
        single-key pool a key-level park is a whole-pool freeze. Without a
        model (the free health check) the verdict is about the key itself,
        so key-level cooldown is correct there."""
        now = now if now is not None else time.time()
        st = self._entry(key)
        with self._lock:
            st["last_fault"] = kind
            st["probes"] += 1
            if model:
                if kind == "quota":
                    # Q6: a free-quota-exhausted model is NOT retried until
                    # the next UTC midnight reset — skip the rest of the day
                    # instead of chaining errors every KEY_COOLDOWN["quota"].
                    st["blocked"][model] = _next_utc_midnight_ts(now)
                else:
                    st["blocked"][model] = now + KEY_COOLDOWN.get(
                        kind, MODEL_COOLDOWN if kind == "model_unavailable"
                        else RATE_LIMIT_COOLDOWN)
                st["penalty_until"] = now + PENALTY_WINDOW
            elif kind == "rate_limit":
                st["dead"] = True
                st["unavailable_until"] = now + RATE_LIMIT_COOLDOWN
                st["penalty_until"] = now + PENALTY_WINDOW
            elif kind in ("bad_key", "quota", "geo_block"):
                st["dead"] = True
                st["unavailable_until"] = now + KEY_COOLDOWN.get(kind, 900)
                st["penalty_until"] = now + PENALTY_WINDOW

    def _mark_ok(self, key, model, now=None, elapsed_ms=None,
                 tokens_in=0, tokens_out=0, key_name=""):
        now = now if now is not None else time.time()
        st = self._entry(key)
        with self._lock:
            st["dead"] = False
            st["unavailable_until"] = 0.0
            st["penalty_until"] = 0.0     # a success clears the soft penalty
            st["last_fault"] = ""
            st["ok_model"] = model
            st["calls"] += 1
            st["checked_at"] = now
            st["blocked"].pop(model, None)
            if elapsed_ms is not None:
                old = st.get("latency") or 0.0
                st["latency"] = (LATENCY_ALPHA * elapsed_ms
                                 + (1.0 - LATENCY_ALPHA) * old) \
                    if old else elapsed_ms
        self._count_send(key, model, tokens_in=tokens_in, tokens_out=tokens_out,
                         ms=elapsed_ms or 0.0, key_name=key_name)
        # Criterion 10: the 0/20 bars update on EVERY call, not just
        # health-checks. main.py sets router.on_usage to a hub.emit
        # closure; the router itself stays UI-agnostic (no hub import,
        # which would be circular — runner imports this module).
        emit_usage = getattr(self, "on_usage", None)
        if emit_usage is not None:
            try:
                emit_usage(key, model, self._usage_of(key))
            except Exception:
                pass

    # ---------------------------------------------------------- main call
    def call(self, cfg, prompt, data_b64, mime_type,
             temperature=0.1, max_tokens=16384, retries=None,
             on_problem=None, on_route=None):
        """Run real work through the best key×model, rotating on failure.

        Returns (parsed_json, raw_text, route) where `route` is
        {"key": masked, "label": …, "model": …}. Same contract as
        gemini.call_gemini plus the route the caller can log.
        """
        pool = cfg.gemini_key_pool
        if not pool:
            raise faults.FaultError(
                faults.fault("bad_key"),
                "No Gemini API keys configured — add them in the Credentials tab.")
        if not cfg.gemini_model_ladder:
            raise faults.FaultError(
                faults.fault("unknown"), "GEMINI_MODEL_LADDER is empty.")

        plan = self._plan(cfg, time.time())
        if not plan:
            now = time.time()
            with self._lock:
                cooling = sum(
                    1 for k in pool
                    if (self._state.get(k) or {}).get("dead")
                    and now < (self._state.get(k) or {}).get("unavailable_until", 0.0))
            if cooling == len(pool):
                flt = faults.fault("quota", "all keys cooling down")
                flt["exhausted"] = True          # the runner stops on this
                raise faults.FaultError(
                    flt,
                    "Every Gemini key is cooling down from a recent failure. "
                    "Open the Credentials tab and run a health check, or wait "
                    "for the cooldown to expire.")
            flt = faults.fault("model_unavailable", "every ladder model is blocked")
            flt["exhausted"] = True
            raise faults.FaultError(
                flt,
                "Every model version is unavailable or out of daily sends for "
                "every key — check the Credentials bars; the cap resets with "
                "Google's daily window, or add more keys.")

        last_flt = faults.fault("unknown")
        tried = 0
        for index, key, models, _latency, _penalty in plan:
            label = self._label(cfg, index, key)
            quality_retried = set()
            for model in models:
                tried += 1
                if on_route:
                    on_route(label, mask(key), model, tried)
                try:
                    started = time.monotonic()
                    parsed, text, usage = call_gemini(
                        key, model, prompt, data_b64, mime_type,
                        temperature=temperature, max_tokens=max_tokens,
                        retries=retries,
                        on_problem=self._problem_cb(on_problem, label, model))
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                except faults.FaultError as exc:
                    flt = exc.fault
                    kind = flt["kind"]
                    last_flt = flt
                    if kind in GLOBAL_KINDS:
                        # Not the key's fault — rotating wastes a full
                        # timeout per extra key for the same result.
                        raise
                    if kind in ROTATE_KINDS:
                        self._mark(key, kind, model)
                        model_level = kind in MODEL_LEVEL_ROTATIONS
                        self._log(f"↪ {label} · {model} → {flt['label']}; "
                                  f"{'next model' if model_level else 'next key'}")
                        if model_level:
                            continue
                        break           # key-level: stop using this key
                    if kind in MODEL_RETRY_KINDS and model not in quality_retried:
                        quality_retried.add(model)
                        self._log(f"↪ {label} · {model} replied badly "
                                  f"({flt['label']}); trying the next model")
                        continue
                    # Not a global fault and not a cached kind (only
                    # `unknown` reaches here): park the pair so the next
                    # file cannot re-select the exact key×model that just
                    # failed, then step up the ladder instead of aborting.
                    self._mark(key, kind, model)
                    self._log(f"↪ {label} · {model} → {flt['label']}; "
                              f"parking this key/model pair")
                    continue
                self._mark_ok(key, model, elapsed_ms=elapsed_ms,
                              tokens_in=(usage or {}).get("tokens_in", 0),
                              tokens_out=(usage or {}).get("tokens_out", 0),
                              key_name=label)
                # This key just succeeded: start the NEXT call from
                # the following pool position, so healthy keys share
                # the files instead of key #1 taking them all.
                with self._lock:
                    self._rotate = (index + 1) % len(pool)
                self._log(f"✔ {label} · {model} serving")
                return parsed, text, {"key": mask(key), "label": label,
                                      "model": model, "attempts": tried}

        # Every pair in the plan failed inside this one call. If nothing is
        # left to try, say so now — otherwise the next file pays for the
        # same walk and the run keeps failing files one by one.
        if not self._plan(cfg, time.time()):
            last_flt = dict(last_flt)
            last_flt["exhausted"] = True
        raise faults.FaultError(
            last_flt, f"{last_flt['label']} — no working key/model pair after "
                      f"{tried} attempt(s): {last_flt['hint']}")

    def call_pinned(self, cfg, key, prompt, data_b64, mime_type,
                    temperature=0.1, max_tokens=16384, retries=None,
                    on_problem=None, on_route=None):
        """Same contract as call(), but PINNED to one key: it walks only that
        key's own ladder (blocked models / spent day caps / sticky model all
        honoured) and never rotates to another key. Does NOT touch
        self._rotate — a parallel lane must not steal the serial rotation.

        Raises a FaultError whose fault["exhausted"] is True when the pinned
        key has no usable model left (every rung blocked or capped), so the
        parallel runner treats the file like the serial "blocked" case: leave
        it for the next run (Q3=B)."""
        if not cfg.gemini_model_ladder:
            raise faults.FaultError(
                faults.fault("unknown"), "GEMINI_MODEL_LADDER is empty.")
        plan = self._plan(cfg, time.time(), keys=[key])
        if not plan:
            flt = faults.fault("model_unavailable",
                               "pinned key has no usable model")
            flt["exhausted"] = True
            raise faults.FaultError(
                flt, f"Key {mask(key)} has no usable model left today — "
                     "every model is blocked or capped.")
        pool = list(cfg.gemini_key_pool)
        label = self._label(cfg, pool.index(key) if key in pool else 0, key)
        quality_retried = set()
        tried = 0
        last_flt = faults.fault("unknown")
        for _index, _key, models, _latency, _penalty in plan:
            for model in models:
                tried += 1
                if on_route:
                    on_route(label, mask(key), model, tried)
                try:
                    started = time.monotonic()
                    parsed, text, usage = call_gemini(
                        key, model, prompt, data_b64, mime_type,
                        temperature=temperature, max_tokens=max_tokens,
                        retries=retries,
                        on_problem=self._problem_cb(on_problem, label, model))
                    elapsed_ms = (time.monotonic() - started) * 1000.0
                except faults.FaultError as exc:
                    flt = exc.fault
                    kind = flt["kind"]
                    last_flt = flt
                    if kind in GLOBAL_KINDS:
                        # Not the key's fault — abort this file, don't burn
                        # the other models on the same timeout.
                        raise
                    if kind in ROTATE_KINDS:
                        self._mark(key, kind, model)
                        self._log(f"↪ {label} · {model} → {flt['label']}; "
                                  f"{'next model' if kind in MODEL_LEVEL_ROTATIONS else 'key done'}")
                        if kind in MODEL_LEVEL_ROTATIONS:
                            continue          # next rung, same key
                        break                 # key-level: this key is done
                    if kind in MODEL_RETRY_KINDS and model not in quality_retried:
                        quality_retried.add(model)
                        self._log(f"↪ {label} · {model} replied badly "
                                  f"({flt['label']}); trying the next model")
                        continue
                    # `unknown`: park the pair, step up the ladder.
                    self._mark(key, kind, model)
                    continue
                self._mark_ok(key, model, elapsed_ms=elapsed_ms,
                              tokens_in=(usage or {}).get("tokens_in", 0),
                              tokens_out=(usage or {}).get("tokens_out", 0),
                              key_name=label)
                return parsed, text, {"key": mask(key), "label": label,
                                      "model": model, "attempts": tried}
        # The pinned key's whole ladder failed for this file. If the key still
        # has a usable rung the failure is per-file (retried next run); if it
        # has none, flag it so the runner parks the lane.
        if not self._plan(cfg, time.time(), keys=[key]):
            last_flt = dict(last_flt)
            last_flt["exhausted"] = True
        raise faults.FaultError(
            last_flt, f"{last_flt['label']} — pinned key {mask(key)} exhausted "
                      f"after {tried} attempt(s): {last_flt['hint']}")

    def _problem_cb(self, on_problem, label, model):
        if not on_problem:
            return None

        def cb(flt, attempt, tries, wait):
            on_problem(flt, attempt, tries, wait, f"{label} · {model}")
        return cb

    # --------------------------------------------------------- health checks
    def gateway(self, ttl=15.0):
        """Keyless probe of googleapis.com, cached for a few seconds.

        Answers 'is the path to Google up at all?' without a model and
        without a key, so a tunnel/region outage is named as such instead
        of every key being blamed one after another."""
        now = time.time()
        with self._lock:
            seen, res = self._gateway
            if res is not None and now - seen < ttl:
                return res
        try:
            res = gateway_probe()
        except Exception as exc:                      # pragma: no cover
            res = {"reachable": False, "google_err": False,
                   "kind": "tunnel_down", "detail": str(exc)[:160]}
        with self._lock:
            self._gateway = (time.time(), res)
        if not res.get("reachable"):
            self._log(f"🔌 googleapis unreachable: {str(res.get('detail', ''))[:90]}")
        return res

    def gateway_cached(self):
        """The last probe result, without triggering a new one."""
        with self._lock:
            return self._gateway[1]

    def _fault_brief(self, flt):
        """A safe, explainable summary of a fault for the dashboard."""
        return {"kind": flt["kind"], "cause": FAULT_CAUSE.get(flt["kind"], "other"),
                "label": flt["label"], "emoji": flt["emoji"],
                "group": flt.get("group", "misc"), "hint": flt["hint"],
                "raw": flt.get("raw", "")[:300]}

    def _base_row(self, cfg, index):
        key = cfg.gemini_key_pool[index]
        st = self._state.get(key) or {}
        now = time.time()
        return {"index": index, "label": self._label(cfg, index, key),
                "masked": mask(key), "status": "unknown", "fault": None,
                "models": {}, "ok_model": "", "calls": 0, "last_fault": "",
                "usage": self._usage_of(key),
                # Wise-rotation readouts (same shape as status()).
                "latency_ms": round(st.get("latency") or 0.0, 1),
                "penalized": bool(st.get("penalty_until", 0.0) > now)}

    def check_key(self, cfg, index):
        """Health check ONE stored key — the Credentials tab walks the pool
        with this so every verdict appears on screen as it arrives.

        At most two HTTP touches happen: the cached keyless gateway probe
        and a single free metadata listing for the key. No model is used;
        model health is only ever learned from real traffic mid-process."""
        pool = cfg.gemini_key_pool
        if not (0 <= index < len(pool)):
            raise IndexError(f"no stored key at position {index}")
        gw = self.gateway()
        row = self._base_row(cfg, index)
        if not gw.get("reachable"):
            flt = faults.fault(gw.get("kind") or "tunnel_down")
            brief = self._fault_brief(flt)
            brief["label"] = ("GOOGLE ERRORING" if flt["kind"] == "overload"
                              else "GOOGLE UNREACHABLE")
            brief["hint"] = ("googleapis.com never answered — that is your "
                             "tunnel / exit IP / region, not this key. The "
                             "key was left untouched."
                             if flt["kind"] != "overload" else
                             "Google itself is answering badly (5xx). Your "
                             "path is fine; wait a few minutes and re-check.")
            brief["raw"] = str(gw.get("detail", ""))[:300]
            row["status"] = "unreachable"
            row["fault"] = brief
            row["gateway_only"] = True   # the UI shows this once, as a banner
            self._log(f"🔌 {row['label']} · not checked — {brief['label'].lower()}")
            return row
        row = self._check_key(cfg, index, pool[index],
                              list(cfg.gemini_model_ladder))
        if gw.get("google_err"):
            row["google_err"] = str(gw.get("detail", ""))
        return row

    def check_all(self, cfg):
        """Whole pool, one key at a time (free metadata calls only), with
        the gateway probe first so a tunnel outage is never mistaken for
        dead keys. Returns {"keys": [...], "models": [...], "gateway": …}."""
        gw = self.gateway()
        rows = [self.check_key(cfg, i)
                for i in range(len(cfg.gemini_key_pool))]
        return {"keys": rows, "models": list(cfg.gemini_model_ladder),
                "gateway": gw, "checked_at": time.time()}

    def _check_key(self, cfg, index, key, ladder):
        """The actual free listing call for one live-path key."""
        row = self._base_row(cfg, index)
        now = time.time()
        try:
            visible = list_models(key)
        except faults.FaultError as exc:
            flt = exc.fault
            kind = flt["kind"]
            if kind in ROTATE_KINDS or kind == "geo_block":
                self._mark(key, kind, now=now)
            else:
                # A blip on the path says nothing about the key — never put
                # a cooldown on something it did not do.
                st = self._entry(key)
                with self._lock:
                    st["last_fault"] = kind
            st = self._entry(key)
            with self._lock:
                row["calls"] = st["calls"]
                row["last_fault"] = st["last_fault"]
                row["ok_model"] = st.get("ok_model", "")
            if kind in ("bad_key", "quota"):
                row["status"] = "dead"
            elif kind == "geo_block":
                row["status"] = "blocked"
            else:
                row["status"] = "unreachable"
            row["fault"] = self._fault_brief(flt)
            self._log(f"{flt['emoji']} {row['label']} · {flt['label']}")
            return row

        # The listing answers "is the key alive" and which ladder versions
        # Google even shows it — no generation request is spent on models.
        for model in ladder:
            if model in visible:
                row["models"][model] = "ok" if visible[model] else "no_generate"
            else:
                # Not listed: invisible to this account (typical for a
                # version not rolled out to it). A real 404 mid-process
                # will confirm; until then the bar just stays hollow.
                row["models"][model] = "missing"

        st = self._entry(key)
        with self._lock:
            st["checked_at"] = time.time()
            st["last_fault"] = ""
            st["dead"] = False
            st["unavailable_until"] = 0.0
            row["calls"] = st["calls"]
            row["ok_model"] = st.get("ok_model", "")
            for model in ladder:
                if row["models"][model] == "ok":
                    st["blocked"][model] = 0.0
                elif row["models"][model] in ("missing", "no_generate"):
                    st["blocked"][model] = time.time() + MODEL_COOLDOWN
        if any(v == "ok" for v in row["models"].values()):
            row["status"] = "ok"
        else:
            # Key itself is alive — none of the ladder versions are visible
            # to it (real traffic will confirm quickly).
            row["status"] = "alive_unlisted"
        self._log(f"♥ {row['label']} · {row['status'].replace('_', ' ')}")
        return row

    # ---------------------------------------------------------------- status
    def status(self, cfg):
        """Current cached view of the pool, safe to render masked."""
        now = time.time()
        rows = []
        for i, key in enumerate(cfg.gemini_key_pool):
            st = self._state.get(key) or {}
            cooling = st.get("dead") and now < st.get("unavailable_until", 0.0)
            rows.append({
                "index": i,
                "label": self._label(cfg, i, key),
                "masked": mask(key),
                "ok_model": st.get("ok_model", ""),
                "serving": bool(st.get("ok_model")) and not cooling,
                "cooling": bool(cooling),
                "cooldown_seconds": max(0, int(st.get("unavailable_until", 0.0) - now)) if cooling else 0,
                "last_fault": st.get("last_fault", ""),
                "blocked_models": [m for m, until in st.get("blocked", {}).items()
                                   if now < until],
                "calls": st.get("calls", 0),
                "usage": self._usage_of(key),
                "checked_at": st.get("checked_at", 0.0),
                # Wise-rotation readouts (UI shows why this order).
                "latency_ms": round(st.get("latency") or 0.0, 1),
                "penalized": bool(st.get("penalty_until", 0.0) > now),
            })
        return {"keys": rows, "models": list(cfg.gemini_model_ladder),
                "gateway": self.gateway_cached(),
                "rotate": self._rotate % len(cfg.gemini_key_pool)
                          if cfg.gemini_key_pool else 0,
                "recent": list(self.events[-40:]), "now": now}

    def reset(self, key=None):
        """Forget cached failures (after editing credentials, or from the UI).
        Daily send counts are facts, not caches — they survive a reset."""
        with self._lock:
            if key is None:
                self._state = {}
                self.preflight_for = None
                self._gateway = (0.0, None)
                self._rotate = 0
            else:
                self._state.pop(key, None)


router = GeminiRouter()
