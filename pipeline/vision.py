"""OpenAI-compatible vision calls (the alternative OCR engine).

One POST to <base>/chat/completions with the image inline as a data URL —
the same dialect the Revision AI speaks — so any local server (llama.cpp,
Ollama, vLLM, 9router…) or hosted provider (OpenRouter, Groq…) can replace
Gemini as the main OCR model. Parsed with the same tolerant JSON extraction
as Gemini, and faults flow through pipeline.faults so the dashboard
classifies them identically. The Gemini lane itself stays in
gemini_router (its module-level `router` singleton is what the runner and
tests patch); the runner dispatches between the two by cfg.ocr_provider.
"""
import os
import time

import requests

from . import faults
from .gemini import extract_json

MIME_TO_DATAURL = {
    "image/jpeg": "image/jpeg", "image/jpg": "image/jpeg",
    "image/png": "image/png", "image/webp": "image/webp",
}


def _openai_fault(resp=None, exc=None):
    """Classify an OpenAI-compatible failure into the shared fault kinds."""
    if exc is not None:
        flt = faults.classify(exc)
        flt["raw"] = str(exc)[:400]
        return flt
    snippet = (resp.text or "")[:300]
    kind = "unknown"
    if resp.status_code in (401, 403):
        kind = "bad_key"
    elif resp.status_code == 429:
        kind = "rate_limit"
    elif resp.status_code == 404:
        kind = "model_unavailable"
    elif resp.status_code >= 500:
        kind = "overload"
    flt = faults.fault(kind)
    flt["raw"] = f"HTTP {resp.status_code}: {snippet}"
    return flt


def provider_label(cfg):
    """Human name for the configured OCR endpoint: 'local' for a
    loopback server, else the URL's host. Used in dashboard events
    so the user sees WHICH engine served a file."""
    from urllib.parse import urlparse
    base = (cfg.ocr_base_url or "").strip()
    if not base:
        return "OCR endpoint"
    host = urlparse(base).hostname or base
    if host in ("localhost", "127.0.0.1", "::1") or host.endswith(".local"):
        return "local"
    return host


def call_ocr_openai(cfg, prompt, data_b64, mime_type,
                    temperature=0.1, max_tokens=16384, retries=None,
                    on_problem=None, on_route=None):
    """One vision chat-completion against an OpenAI-compatible endpoint.

    Mirrors gemini.call_gemini's retry contract (growing wait, classified
    faults) so the runner can treat both engines identically. Returns the
    parsed JSON object; raises faults.FaultError on failure.

    `on_route(label, masked, model, attempt)` fires before every retry
    with the endpoint's host as the label — the OpenAI lane has one
    endpoint, so a route event always means "retrying this one"."""
    if retries is None:
        retries = max(1, int(os.getenv("NET_RETRIES", "3")))
    base_wait = max(0.0, float(os.getenv("NET_RETRY_WAIT", "8")))
    url = cfg.ocr_base_url.rstrip("/") + "/chat/completions"
    content = [{"type": "text", "text": prompt}]
    if data_b64:
        mime = MIME_TO_DATAURL.get((mime_type or "").lower(), mime_type or "image/jpeg")
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{data_b64}"}})
    payload = {
        "model": cfg.ocr_model,
        "stream": False,
        "messages": [{"role": "user", "content": content}],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.ocr_api_key:
        headers["Authorization"] = "Bearer " + cfg.ocr_api_key

    last = faults.fault("unknown")
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers,
                                 timeout=(10, 300))
        except requests.RequestException as exc:
            last = _openai_fault(exc=exc)
        else:
            if resp.status_code != 200:
                last = _openai_fault(resp=resp)
            else:
                try:
                    body = resp.json()
                    text = (body["choices"][0]["message"]["content"] or "")
                except (KeyError, IndexError, TypeError, ValueError):
                    last = faults.fault("model_garbage",
                                        "OpenAI-compatible endpoint returned an "
                                        "unexpected response shape.")
                else:
                    if not (text or "").strip():
                        last = faults.fault("model_empty",
                                            "Model returned no text content.")
                    else:
                        try:
                            return extract_json(text)
                        except ValueError as exc:
                            last = faults.fault("model_garbage", str(exc))
        if not last["retryable"]:
            raise faults.FaultError(
                last, f"{last['label']} — {last['hint']} · {last['raw'][:200]}")
        if attempt < retries:
            wait = base_wait * attempt
            if on_route:
                on_route(provider_label(cfg), "", cfg.ocr_model, attempt)
            if on_problem:
                on_problem(last, attempt, retries, wait)
            time.sleep(wait)
    raise faults.FaultError(
        last, f"{last['label']} after {retries} attempts — {last['hint']} "
              f"(last error: {last['raw'][:200]})")


def check_provider(cfg, timeout=(8, 20)):
    """Free health check for an OpenAI-compatible OCR endpoint:
    ONE GET <base>/models metadata listing — no model is run, no
    generation tokens are spent (same philosophy as the Gemini
    key checks). Returns a verdict dict, never raises."""
    base = (cfg.ocr_base_url or "").strip().rstrip("/")
    if not base:
        return {"ok": False, "provider": "openai", "url": "",
                "status": 0, "models": [], "detail": "No OCR endpoint "
                "URL is configured (OCR_BASE_URL).", "fault": None}
    headers = {}
    if cfg.ocr_api_key:
        headers["Authorization"] = "Bearer " + cfg.ocr_api_key
    try:
        resp = requests.get(base + "/models", headers=headers,
                            timeout=timeout)
    except requests.RequestException as exc:
        flt = _openai_fault(exc=exc)
        return {"ok": False, "provider": "openai", "url": base,
                "status": 0, "models": [],
                "detail": f"{flt['label']} — {flt['hint']}",
                "fault": flt}
    if resp.status_code != 200:
        flt = _openai_fault(resp=resp)
        return {"ok": False, "provider": "openai", "url": base,
                "status": resp.status_code, "models": [],
                "detail": f"HTTP {resp.status_code}: "
                          f"{(resp.text or '')[:200]}",
                "fault": flt}
    names = []
    try:
        body = resp.json()
        for m in (body.get("data") or []):
            if isinstance(m, dict) and m.get("id"):
                names.append(str(m["id"]))
    except ValueError:
        # Some minimal servers answer 200 with a non-JSON body;
        # the endpoint is reachable, just not model-listing.
        return {"ok": True, "provider": "openai", "url": base,
                "status": 200, "models": [],
                "detail": "reachable, but /models returned a "
                          "non-JSON body", "fault": None}
    return {"ok": True, "provider": "openai", "url": base,
            "status": 200, "models": names,
            "detail": f"{len(names)} model(s) listed", "fault": None}


def test_chat(base_url, api_key, model, timeout=(8, 40)):
    """ONE tiny chat completion against an OpenAI-compatible endpoint.

    Returns (reply_snippet, latency_ms). Unlike check_provider() this spends
    a few real tokens — the only honest way to tell a reachable endpoint from
    a dead one. Raises faults.FaultError, classified the same way as
    call_ocr_openai()."""
    started = time.monotonic()
    try:
        resp = requests.post(
            base_url.rstrip("/") + "/chat/completions",
            json={"model": model, "stream": False, "temperature": 0,
                  "max_tokens": 24,
                  "messages": [{"role": "user",
                                "content": "Reply with exactly: pong"}]},
            headers=({"Content-Type": "application/json",
                      "Authorization": "Bearer " + api_key} if api_key
                     else {"Content-Type": "application/json"}),
            timeout=timeout)
    except requests.RequestException as exc:
        flt = _openai_fault(exc=exc)
        raise faults.FaultError(flt, f"{flt['label']} — {flt['hint']}")
    if resp.status_code != 200:
        flt = _openai_fault(resp=resp)
        raise faults.FaultError(flt, f"HTTP {resp.status_code}")
    try:
        text = resp.json()["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, ValueError):
        raise faults.FaultError(
            faults.fault("model_garbage",
                         "OpenAI-compatible endpoint returned an "
                         "unexpected response shape."),
            "unexpected response shape")
    return text.strip()[:200], round((time.monotonic() - started) * 1000, 1)
