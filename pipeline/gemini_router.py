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
  RESOURCE_EXHAUSTED answer fills that model's bar to the cap at once.
* **Last-known-good is sticky.** Once a pair works, that model is tried
  first for that key from then on, which keeps the ladder walk at one hop.
"""
import hashlib
import json
import os
import threading
import time
from datetime import date

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
# Survives restarts so the bars do not reset every time the server does.
USAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".gemini-usage.json")

# Faults that mean "this key/pair is the problem" → rotate.
ROTATE_KINDS = {"bad_key", "quota", "rate_limit", "model_unavailable"}
# Which of those are per MODEL (free-tier day quota is per model, and so is
# a 404 rollout gap) → step up the ladder. The rest are per key → next key.
MODEL_LEVEL_ROTATIONS = {"quota", "model_unavailable"}
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

    # ------------------------------------------------------------------ state
    def _entry(self, key):
        with self._lock:
            return self._state.setdefault(key, {
                "dead": False, "unavailable_until": 0.0, "last_kind": "",
                "ok_model": "", "blocked": {}, "probes": 0, "calls": 0,
                "fails": 0, "last_fault": "", "checked_at": 0.0,
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
        return date.today().isoformat()

    def _key_hash(self, key):
        """Identity for the usage file — a hash, never the secret itself."""
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

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

    def _count_send(self, key, model, quota_hit=False):
        """Record one SUCCESSFUL send for today. A real quota refusal from
        Google fills that model's bar to the cap immediately."""
        self._usage_load()
        limit = self.day_limit()
        with self._lock:
            today = self._today()
            if today != self._usage_day:
                self._usage, self._usage_day = {}, today
            per = self._usage.setdefault(self._key_hash(key), {})
            used = per.get(model, 0) + (0 if quota_hit else 1)
            per[model] = max(used, limit) if quota_hit else used
            self._usage_save()

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
    def _plan(self, cfg, now):
        """Ordered [(index, key, [models…])] of what is worth trying.

        Skips keys in cooldown and models already known-unavailable (or
        with today's sends spent) for a key, so nothing is paid for twice."""
        plan = []
        with self._lock:
            for i, key in enumerate(cfg.gemini_key_pool):
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
                plan.append((i, key, ladder))
        return plan

    def _mark(self, key, kind, model=None, now=None):
        """Record what a failure taught us, with the right cooldown."""
        now = now if now is not None else time.time()
        st = self._entry(key)
        with self._lock:
            st["last_fault"] = kind
            st["probes"] += 1
            if kind == "model_unavailable" and model:
                st["blocked"][model] = now + MODEL_COOLDOWN
            elif kind == "quota" and model:
                # Free-tier day quota is per model — the bar (below) spends
                # this rung, but the key keeps serving the rest of the ladder.
                pass
            elif kind == "rate_limit":
                st["dead"] = True
                st["unavailable_until"] = now + RATE_LIMIT_COOLDOWN
            elif kind in ("bad_key", "quota", "geo_block"):
                st["dead"] = True
                st["unavailable_until"] = now + KEY_COOLDOWN.get(kind, 900)
        if kind == "quota" and model:
            # Google itself said this model is spent for this key — fill
            # its bar so the Credentials tab shows the truth, not a guess.
            self._count_send(key, model, quota_hit=True)

    def _mark_ok(self, key, model, now=None):
        now = now if now is not None else time.time()
        st = self._entry(key)
        with self._lock:
            st["dead"] = False
            st["unavailable_until"] = 0.0
            st["last_fault"] = ""
            st["ok_model"] = model
            st["calls"] += 1
            st["checked_at"] = now
            st["blocked"].pop(model, None)
        self._count_send(key, model)

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
                raise faults.FaultError(
                    faults.fault("quota", "all keys cooling down"),
                    "Every Gemini key is cooling down from a recent failure. "
                    "Open the Credentials tab and run a health check, or wait "
                    "for the cooldown to expire.")
            raise faults.FaultError(
                faults.fault("model_unavailable", "every ladder model is blocked"),
                "Every model version is unavailable or out of daily sends for "
                "every key — check the Credentials bars; the cap resets with "
                "Google's daily window, or add more keys.")

        last_flt = faults.fault("unknown")
        tried = 0
        for index, key, models in plan:
            label = self._label(cfg, index, key)
            quality_retried = set()
            for model in models:
                tried += 1
                if on_route:
                    on_route(label, mask(key), model, tried)
                try:
                    parsed, text = call_gemini(
                        key, model, prompt, data_b64, mime_type,
                        temperature=temperature, max_tokens=max_tokens,
                        retries=retries,
                        on_problem=self._problem_cb(on_problem, label, model))
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
                    raise               # nothing rotation can fix
                self._mark_ok(key, model)
                self._log(f"✔ {label} · {model} serving")
                return parsed, text, {"key": mask(key), "label": label,
                                      "model": model, "attempts": tried}

        raise faults.FaultError(
            last_flt, f"{last_flt['label']} — no working key/model pair after "
                      f"{tried} attempt(s): {last_flt['hint']}")

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
        return {"index": index, "label": self._label(cfg, index, key),
                "masked": mask(key), "status": "unknown", "fault": None,
                "models": {}, "ok_model": "", "calls": 0, "last_fault": "",
                "usage": self._usage_of(key)}

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
            })
        return {"keys": rows, "models": list(cfg.gemini_model_ladder),
                "gateway": self.gateway_cached(),
                "recent": list(self.events[-40:]), "now": now}

    def reset(self, key=None):
        """Forget cached failures (after editing credentials, or from the UI).
        Daily send counts are facts, not caches — they survive a reset."""
        with self._lock:
            if key is None:
                self._state = {}
                self.preflight_for = None
                self._gateway = (0.0, None)
            else:
                self._state.pop(key, None)


router = GeminiRouter()
