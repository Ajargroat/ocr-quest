"""Turns raw network/API failures into named, explainable faults.

The pipeline runs behind a tunnel (Google services are not directly
reachable from Iran), so an identical "Gemini failed" message can mean a
dozen different things: the VPN link blipped, the request never got sent,
the answer was cut on the way back, the free quota ran out, the key is
dead, or Google simply blocked the exit IP. This module inspects the whole
exception chain — exception type names, error wording, HTTP status and the
Google error JSON — and maps it to one fault kind carrying a short label
and actionable advice, which the runner forwards to the dashboard.
"""
import os
import re


def backoff_wait(attempt, base=None, cap=None):
    """Exponential backoff: short first wait, doubling, capped (Q8).

    ``base`` defaults to NET_RETRY_WAIT (compat, default 5s); ``cap``
    defaults to NET_RETRY_CAP (default 120s). Attempt is 1-based, so
    the sequence is base, 2*base, 4*base, … capped.
    """
    if base is None:
        try:
            base = max(0.0, float(os.getenv("NET_RETRY_WAIT", "5")))
        except ValueError:
            base = 5.0
    if cap is None:
        try:
            cap = max(0.0, float(os.getenv("NET_RETRY_CAP", "120")))
        except ValueError:
            cap = 120.0
    return min(cap, base * (2 ** (max(1, attempt) - 1)))

# group = colour family the UI uses for the chip (net/bill/auth/srv/model/…)
KINDS = {
    "tunnel_down":    {"label": "TUNNEL DOWN",     "emoji": "🔌", "group": "net",   "retryable": True,
                       "hint": "The link to Google never came up — your VPN/proxy is down, its exit node dropped, or DNS failed. Restore the tunnel and rerun; the file stays queued."},
    "send_blocked":   {"label": "SEND TIMEOUT",    "emoji": "📤", "group": "net",   "retryable": True,
                       "hint": "The tunnel opened but the request could not finish going out — uplink stalled mid-send (slow tunnel or heavy page)."},
    "recv_dropped":   {"label": "RECV DROPPED",    "emoji": "📉", "group": "net",   "retryable": True,
                       "hint": "The request reached Google but the answer was cut mid-flight — a classic tunnel blip while waiting. The file stays queued for the next run."},
    "rate_limit":     {"label": "RATE LIMIT",      "emoji": "🚦", "group": "bill",  "retryable": True,
                       "hint": "Too many requests per minute (HTTP 429). This clears by itself in ~60 s; the built-in retries usually absorb it."},
    "quota":          {"label": "FREE QUOTA",      "emoji": "🧮", "group": "bill",  "retryable": False,
                       "hint": "Your Gemini free-tier daily limit is exhausted (RESOURCE_EXHAUSTED). Wait for the reset window or upgrade the plan — retrying now won't help."},
    "bad_key":        {"label": "BAD API KEY",     "emoji": "🔑", "group": "auth",  "retryable": False,
                       "hint": "Google rejected the API key (401/403). Check GEMINI_API_KEY in .env — retries cannot fix this."},
    "model_unavailable": {"label": "MODEL UNAVAILABLE", "emoji": "🧊", "group": "model", "retryable": False,
                          "hint": "This key cannot use that model (404 not found, or 403 because the model is not enabled/rolled out for it). The router steps up to the next model in the ladder."},
    "geo_block":      {"label": "GEO BLOCK",       "emoji": "🌍", "group": "auth",  "retryable": False,
                       "hint": "Google refused this exit IP (region / account-country restriction). Switch the tunnel to a supported-country exit — a different key won't help."},
    "overload":       {"label": "GOOGLE BUSY",     "emoji": "🌩", "group": "srv",   "retryable": True,
                       "hint": "Google answered 5xx / DEADLINE_EXCEEDED — high demand on their side. Retrying a little later usually works."},
    "model_empty":    {"label": "EMPTY REPLY",     "emoji": "👻", "group": "model", "retryable": True,
                       "hint": "Gemini answered 200 but produced no text (often a safety filter or a page it could not read)."},
    "model_garbage":  {"label": "BAD JSON",        "emoji": "🌀", "group": "model", "retryable": True,
                       "hint": "Gemini replied, but not with parsable JSON — a one-off model hiccup; a retry usually fixes it."},
    "db_down":        {"label": "DATABASE DOWN",   "emoji": "🗄", "group": "store", "retryable": True,
                       "hint": "Postgres is not answering — check the docker-compose DB (or the tunnel that reaches it)."},
    "storage_fail":   {"label": "STORAGE FAIL",    "emoji": "☁",  "group": "store", "retryable": True,
                       "hint": "Supabase refused the upload — check SUPABASE_SERVICE_KEY, the bucket name, or the tunnel on the way there."},
    "local_io":       {"label": "LOCAL FILE",      "emoji": "💾", "group": "local", "retryable": False,
                       "hint": "Could not read or move the source file — check the disk path and whether another program holds the file open."},
    "unknown":        {"label": "UNKNOWN FAULT",   "emoji": "❔", "group": "misc",  "retryable": True,
                       "hint": "No known signature matched — inspect the raw error attached to this event."},
}

_STATUS_RE = re.compile(r"http (\d{3})")


def fault(kind, raw=""):
    """A mutable copy of the kind metadata plus its kind id and raw text."""
    meta = dict(KINDS.get(kind, KINDS["unknown"]))
    meta["kind"] = kind if kind in KINDS else "unknown"
    meta["raw"] = str(raw or "")[:400]
    return meta


class FaultError(RuntimeError):
    """A failure that already carries its classified fault (see .fault)."""

    def __init__(self, flt, message=None):
        super().__init__(message or f"{flt['label']} — {flt['hint']}")
        self.fault = flt


def _chain(exc):
    """The exception plus everything reachable through __cause__/__context__."""
    out, seen, cur, depth = [], set(), exc, 0
    while cur is not None and depth < 8 and id(cur) not in seen:
        seen.add(id(cur))
        out.append(cur)
        nxt = cur.__cause__ or cur.__context__
        if nxt is None and cur.args and isinstance(cur.args[0], BaseException):
            nxt = cur.args[0]
        cur, depth = nxt, depth + 1
    return out


def classify(exc):
    """Classify a real exception (walks the whole chained trace)."""
    chain = _chain(exc)
    blob = " ".join(f"{type(e).__name__} {e}" for e in chain).lower()
    names = " ".join(type(e).__name__ for e in chain).lower()
    flt = _decide(blob, names)
    if not flt["raw"]:
        flt["raw"] = str(exc)[:400]
    return flt


def classify_text(text):
    """Same detection for failure strings we build ourselves (HTTP NNN + body)."""
    blob = str(text).lower()
    return _decide(blob, blob)


def _decide(blob, names):
    match = _STATUS_RE.search(blob)
    status = int(match.group(1)) if match else None
    gemini = "generativelanguage" in blob or "gemini" in blob
    supa = "supabase" in blob or "storage/v1/object" in blob

    def kind(k, extra="", retryable=None):
        flt = fault(k, blob if k == "unknown" else "")
        if extra:
            flt["hint"] = flt["hint"] + " " + extra
        if retryable is not None:
            flt["retryable"] = retryable
        return flt

    # --- our own model-side failure strings --------------------------------
    if "no text content" in blob:
        return kind("model_empty")
    if ("non-json text" in blob or "jsondecodererror" in names
            or "expecting value" in blob or "substring not found" in blob):
        return kind("model_garbage")

    # --- a model version this key cannot serve (the router steps up) -----
    if ((status == 404 and (gemini or "model" in blob))
            or "not found for api version" in blob
            or "not supported for generatecontent" in blob
            or "listpublishedmodels" in blob):
        return kind("model_unavailable")

    # --- database -----------------------------------------------------------
    if not gemini and not supa and any(
            w in blob for w in ("psycopg", "postgres", "could not connect to server",
                                "connection to server")):
        return kind("db_down")

    # --- billing ------------------------------------------------------------
    if status == 429 or "resource_exhausted" in blob or "rate limit" in blob:
        if "resource_exhausted" in blob or "quota" in blob:
            return kind("quota")
        return kind("rate_limit")

    # --- geo restriction (very common behind Iran-side tunnels) --------------
    if any(w in blob for w in ("perusercountryregistration", "not available in your country",
                               "not available in your region", "region not supported",
                               "service is only available in")):
        return kind("geo_block")

    # --- authentication -------------------------------------------------------
    if status in (401, 403) or "api_key_invalid" in blob or "api key not valid" in blob \
            or "unauthenticated" in blob:
        if supa:
            return kind("storage_fail", "Supabase refused the key/bucket.", retryable=False)
        if status == 403 and gemini and ("permission_denied" in blob or "permission denied" in blob):
            # On Gemini, a bare PERMISSION_DENIED is almost always a regional/IP block.
            return kind("geo_block")
        return kind("bad_key", f"(HTTP {status})" if status else "")

    # --- server-side overload -------------------------------------------------
    if status in (500, 502, 503, 504) or "deadline_exceeded" in blob \
            or "unavailable" in blob or "internal error" in blob or "overloaded" in blob:
        if supa:
            return kind("storage_fail", f"(HTTP {status})" if status else "")
        return kind("overload", f"(HTTP {status})" if status else "")

    # --- proxy / tunnel plumbing (exception types are the strongest signal) ---
    if any(w in names for w in ("proxyerror", "invalidurl", "missingschema", "invalidschema")) \
            or "proxy scheme not supported" in blob or "tunnel connection failed" in blob \
            or ("proxy" in blob and "cannot connect" in blob) \
            or "cannot connect to proxy" in blob:
        return kind("tunnel_down", "The configured proxy itself is unreachable or malformed "
                                   "(check HTTPS_PROXY / Windows system proxy).")
    if any(w in names for w in ("sslerror", "ssleoferror", "certificateerror", "sslcertverificationerror")) \
            or "certificate" in blob or "tlsv1" in blob or "wrapsocket" in blob \
            or "wrong version number" in blob or "unknown protocol" in blob:
        return kind("tunnel_down", "TLS handshake broke — tunnels that hijack or cut HTTPS cause exactly this.")
    if "connecttimeout" in names:
        return kind("send_blocked", "The tunnel could not open a connection at all before the timeout.")
    if "readtimeout" in names or "read timeout" in blob or "timed out" in blob:
        return kind("recv_dropped")
    if "timeout" in names:
        return kind("recv_dropped")
    if any(w in names for w in ("connectionerror", "connectionreseterror", "connectionabortederror",
                                "chunkedencodingerror", "protocolerror", "remoteprotocolerror")) \
            or "connection reset" in blob or "reset by peer" in blob \
            or "connection aborted" in blob or "remote end closed" in blob or "broken pipe" in blob:
        hard_down = any(w in blob for w in ("failed to establish", "unreachable",
                                            "name or service not known", "nodename",
                                            "getaddrinfo", "network is unreachable"))
        # Network plumbing beats service naming: a dropped link on the way to
        # Supabase is a tunnel fault, not a storage-config fault.
        where = "Supabase" if supa else "Google"
        if hard_down or "max retries exceeded" in blob:
            return kind("tunnel_down", f"The broken link was on the way to {where}.")
        return kind("recv_dropped", f"The answer from {where} was cut mid-transfer.")
    if "max retries exceeded" in blob:
        return kind("tunnel_down")

    # --- local file problems ----------------------------------------------------
    if any(w in names for w in ("filenotfounderror", "permissionerror",
                                "isadirectoryerror", "shutilerror", "fileexistserror")) \
            or "no such file" in blob:
        return kind("local_io")

    if supa:
        return kind("storage_fail", f"(HTTP {status})" if status else "")
    return kind("unknown", f"(HTTP {status})" if status else "")
