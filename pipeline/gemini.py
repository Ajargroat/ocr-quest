"""Thin client for the Gemini REST API (equivalent of the two
'Analyze ...' nodes), with the same tolerant JSON extraction.

Every failure is classified by pipeline.faults so the dashboard can tell a
tunnel blip apart from a dead key or an exhausted quota. Transient faults
are retried with a growing wait; faults that retrying cannot fix (bad key,
geo block, daily quota) are raised immediately."""
import json
import os
import re
import time
from urllib.parse import quote

import requests

from . import faults

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LIST_URL = "https://generativelanguage.googleapis.com/v1beta/models"

# The plain HTTP(S)/SOCKS tunnel for Google-bound calls only. Set by the
# runner / server when the pipeline tab saves a proxy profile; kept here
# (not in the router) so OCR, health checks and gateway probes ride the
# same lane. Local providers and Supabase/Postgres never see it.
_proxy_url = {"url": ""}


def set_proxy(cfg):
    """Point module state at the profile (or no profile) a Config selects."""
    prof = next((p for p in cfg.proxy_profiles
                 if p.get("name") == cfg.proxy_active), None)
    if not prof or not prof.get("host"):
        _proxy_url["url"] = ""
        return
    scheme = (prof.get("scheme") or "http").lower()
    auth = (f"{quote(prof['user'], safe='')}:{quote(prof['password'], safe='')}@"
            if prof.get("user") else "")
    _proxy_url["url"] = f"{scheme}://{auth}{prof['host']}:{prof.get('port') or ''}"


def proxy_proxies():
    """requests `proxies=` dict for the lane, or None to keep defaults.

    Only outbound HTTPS is scoped — Supabase/Postgres/local traffic must
    never ride the tunnel, which is the whole point of a per-call lane
    instead of a machine-wide VPN."""
    url = _proxy_url["url"]
    if not url:
        return None
    return {"http": url, "https": url}


def call_gemini(api_key, model, prompt, data_b64, mime_type,
                temperature=0.1, max_tokens=16384, retries=None,
                on_problem=None):
    """Sends one image/PDF plus the prompt to Gemini.
    Returns (parsed_json, raw_text).

    ``on_problem(fault, attempt, retries, wait)`` is called before every
    back-off sleep so the caller can log tunnel hiccups live."""
    if retries is None:
        retries = max(1, int(os.getenv("NET_RETRIES", "3")))
    base_wait = max(0.0, float(os.getenv("NET_RETRY_WAIT", "8")))
    parts = [{"text": prompt}]
    if data_b64:
        # Empty payload = a text-only call (the cheapest possible probe).
        parts.append({"inline_data": {"mime_type": mime_type, "data": data_b64}})
    payload = {
        "contents": [{
            "parts": parts,
        }],
        "generationConfig": {
            "temperature": temperature,
            "maxOutputTokens": max_tokens,
        },
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
    url = API_URL.format(model=model)
    last = faults.fault("unknown")

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=(10, 300), proxies=proxy_proxies())
        except requests.RequestException as exc:
            last = faults.classify(exc)
        else:
            outcome = _outcome(resp)
            if "result" in outcome:
                return outcome["result"]
            last = outcome
        if not last["retryable"]:
            raise faults.FaultError(
                last, f"{last['label']} — {last['hint']} · {last['raw'][:200]}")
        if attempt < retries:
            wait = base_wait * attempt
            if on_problem:
                on_problem(last, attempt, retries, wait)
            time.sleep(wait)

    raise faults.FaultError(
        last, f"{last['label']} after {retries} attempts — {last['hint']} "
              f"(last error: {last['raw'][:200]})")


def list_models(api_key, timeout=(10, 30)):
    """Enumerates the models visible to a key with ONE metadata call —
    no generation tokens are ever billed here. The router's health check
    uses it to confirm the key is alive and which ladder entries the key
    can actually serve, so dead combos are skipped before spending any
    real request. Returns {model_name: supports_generateContent} and
    raises faults.FaultError (classified) on any failure."""
    try:
        resp = requests.get(LIST_URL, params={"pageSize": 1000},
                            headers={"x-goog-api-key": api_key},
                            timeout=timeout, proxies=proxy_proxies())
    except requests.RequestException as exc:
        flt = faults.classify(exc)
        flt["raw"] = str(exc)[:400]
        raise faults.FaultError(flt, f"{flt['label']} — {flt['hint']}")
    if resp.status_code != 200:
        flt = faults.classify_text(f"Gemini HTTP {resp.status_code}: {resp.text[:300]}")
        flt["raw"] = f"HTTP {resp.status_code}: {resp.text[:300]}"
        raise faults.FaultError(flt, f"{flt['label']} — {flt['hint']}")
    try:
        body = resp.json()
    except ValueError:
        flt = faults.fault("tunnel_down", resp.text[:300])
        raise faults.FaultError(flt, f"{flt['label']} — {flt['hint']}")
    out = {}
    for m in body.get("models", []):
        name = (m.get("name") or "").split("/")[-1]
        if name:
            out[name] = "generateContent" in (m.get("supportedGenerationMethods") or [])
    return out


def gateway_probe(timeout=(8, 12)):
    """Asks googleapis.com WITHOUT a key and WITHOUT touching a model.

    The expected answer is Google's own error JSON about the missing key —
    and the mere fact that Google's JSON comes back proves the tunnel path
    works. A timeout/DNS error, or an HTML page answering in Google's
    place (captive portal, proxy), localises the fault to the network or
    the region *before* any key can be blamed for it. Never raises."""
    try:
        resp = requests.get(LIST_URL, params={"pageSize": 1}, timeout=timeout,
                            proxies=proxy_proxies())
    except requests.RequestException as exc:
        flt = faults.classify(exc)
        return {"reachable": False, "google_err": False, "kind": flt["kind"],
                "detail": f"{flt['label']} — {str(exc)[:140]}"}
    try:
        body = resp.json()
        google = isinstance(body, dict) and ("error" in body or "models" in body)
    except ValueError:
        google = False
    if not google:
        # A keyless GET is refused by Google's own HTML error template too
        # ("Error 403 (Forbidden)!!1", robot.png on www.google.com, and a
        # Server-Timing header from their front-end 'gfe'). Any of these
        # proves the real Google answered — a foreign proxy/captive page
        # would not carry them.
        text = (resp.text or "")[:1500].lower()
        server = (resp.headers.get("server") or "").lower()
        timing = (resp.headers.get("server-timing") or "").lower()
        google = (resp.status_code in (400, 403, 404)
                  and ("googleapis" in text or "gstatic" in text
                       or "www.google.com" in text
                       or "forbidden)!!1" in text
                       or timing.startswith("gfe")
                       or server.startswith(("esf", "gve", "sffe", "gws"))))
    if not google:
        return {"reachable": False, "google_err": False, "kind": "tunnel_down",
                "detail": f"HTTP {resp.status_code} answered in Google's place "
                          f"(proxy/captive page?): {resp.text[:140]}"}
    if resp.status_code >= 500:
        return {"reachable": True, "google_err": True, "kind": "overload",
                "detail": f"Google itself answered HTTP {resp.status_code}"}
    return {"reachable": True, "google_err": False,
            "kind": "ok" if resp.status_code < 400 else "refused",
            "detail": f"googleapis.com answered HTTP {resp.status_code}"}


def _outcome(resp):
    """Turn a non-exception response into (result | fault)."""
    if resp.status_code != 200:
        snippet = resp.text[:300]
        flt = faults.classify_text(f"Gemini HTTP {resp.status_code}: {snippet}")
        flt["raw"] = f"HTTP {resp.status_code}: {snippet}"
        return flt
    try:
        body = resp.json()
    except ValueError:
        # A 200 with a non-JSON body is almost always a proxy error page or a
        # captive portal sitting where the tunnel should be.
        flt = faults.classify_text("Gemini answered non-JSON: " + resp.text[:300])
        if flt["kind"] == "unknown":
            flt = faults.fault("tunnel_down", resp.text[:300])
            flt["hint"] += " (a gateway HTML page answered instead of the API — interception on the tunnel path?)"
        return flt
    text = _extract_text(body)
    if not text:
        return faults.fault("model_empty", "Gemini returned no text content.")
    try:
        parsed = extract_json(text)
    except ValueError as exc:
        return faults.fault("model_garbage", str(exc))
    return {"result": (parsed, text)}


def _extract_text(body) -> str:
    try:
        parts = body["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts if isinstance(p, dict)).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def extract_json(text: str):
    """Accepts clean JSON, ```json fenced blocks, or JSON buried in prose."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("Model returned non-JSON text.")
        return json.loads(cleaned[start:end + 1])