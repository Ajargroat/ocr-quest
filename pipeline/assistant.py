"""The floating AI assistant: an explainer/advisor chat inside the dashboard.

It has an OWN, editable provider list — one or more OpenAI-compatible
endpoints (OpenRouter, a local 9router, anything that speaks
``/chat/completions``) — each with its own key and model list, plus one active
model. Nothing here ever touches the OCR lanes, so a chat can never spend a
Gemini key's daily quota or disturb a run.

Layout of the settings in `.env` (all written through the settings endpoint):

    ASSISTANT_PROVIDERS   JSON list of {label, base_url, api_key, models}
    ASSISTANT_MODEL       "label::model" — the one active model
    ASSISTANT_EFFORT      "minimal" | "low" | "medium" | "high" ("" = provider default)

The browser never sees a key: `/api/assistant/chat` relays the upstream SSE
stream as our own events — text, reasoning, and a final ``usage`` event with
token counts and the measured token/s speed.

``STREAM_OVERRIDE`` is the seam tests set, so no unit test reaches the network.
"""
import json
import os
import re
import time

import requests

from . import prompts

STREAM_OVERRIDE = None

# The assistant's instructions. Editable in the Prompts view ("assistant.system")
# so the user can tune its role without touching code.
SYSTEM_PROMPT = """You are the in-app assistant of OCR-Quest, a Persian exam-paper OCR system.

The app has these surfaces:
- Pipeline: watches konkour-ocr/{subject}/{grade}/{topic}/{question|answer}/, uploads each
  file to Supabase Storage, extracts questions/answers with the Gemini key pool
  (multi-key, multi-model ladder) or the built-in 9router provider, writes rows to
  Postgres, and archives the source to done/ or failed/.
- Inspect: review staged OCR rows, correct boxes, captions and table text,
  crops, review by hand (Inspect/Crops tabs), then import. Edits are LOCAL first
  (workspace/<job>/review.json); only Import touches the database.
- QBank: review extracted questions/answers, fix pictures.
- Database: data console (grid, SQL, backups, bucket browser).
- Usage: per-call model usage history as grouped bars.
- Prompts: the editable instructions sent to the models.
- Credentials: Gemini pool + 9router, Supabase, Postgres; all secrets live in .env.

How to answer:
- Be concrete and short. Explain what a control does and where it lives.
- When asked for feedback or improvements, name the surface, the concrete change,
  and the tradeoff. Never invent a feature that does not exist.
- Use Markdown: short paragraphs, lists, tables for comparisons, fenced code for
  commands. Use a ```mermaid block for a diagram only when it genuinely helps.
- If a question needs information you do not have (a live run, a DB row), say so
  and name the tab that would show it.
- Never reveal or ask for API keys, and never print a `.env` value.
"""

# Upstream answers can be long; a chat reply never needs the OCR budget.
MAX_TOKENS = 2048
# Long enough for a slow relay to answer, short enough to fail while the user waits.
TIMEOUT = (10, 180)

# The thinking levels the settings offer, in the order the picker shows them.
EFFORTS = ("", "minimal", "low", "medium", "high")


def _mask(key):
    from .gemini_router import mask
    return mask(key or "")


def _providers(cfg):
    """The saved provider list, legacy ASSISTANT_URL synthesised when absent."""
    raw = getattr(cfg, "assistant_providers", ()) or ()
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        url = (entry.get("base_url") or "").strip()
        if not url:
            continue
        models = [str(m).strip() for m in entry.get("models") or [] if m]
        out.append({"label": (entry.get("label") or "").strip() or "provider",
                    "base_url": url,
                    "api_key": (entry.get("api_key") or "").strip(),
                    "models": models})
    if out:
        return out
    url = (getattr(cfg, "assistant_url", "") or "").strip()
    if not url:
        return []
    model = (getattr(cfg, "assistant_model", "") or "").strip()
    label, sep, bare = model.partition("::")
    if sep:
        model = bare
    else:
        label = "default"
    return [{"label": label, "base_url": url,
             "api_key": (getattr(cfg, "assistant_key", "") or "").strip(),
             "models": [model] if model else []}]


def active(cfg, providers=None):
    """(provider dict, model) for the one active model — ({}, '') if none."""
    providers = providers if providers is not None else _providers(cfg)
    active_id = (getattr(cfg, "assistant_model", "") or "").strip()
    label, sep, model = active_id.partition("::")
    if not sep:
        # the legacy spelling: one bare model id on the one endpoint
        model, label = label, "default"
    if not model:
        return ({}, "")
    for provider in providers:
        if provider["label"] == label:
            return provider, model
    # the label no longer exists: the model itself may still be unambiguous
    hits = [p for p in providers if model in p["models"]]
    if len(hits) == 1:
        return hits[0], model
    return {}, ""


def configured(cfg):
    """True when there is an active model to call."""
    provider, model = active(cfg)
    return bool(provider.get("base_url") and model)


def endpoint(url):
    """Normalise one base URL to an OpenAI-compatible chat endpoint.

    A bare base URL ("http://host/v1") gets /chat/completions appended; a full
    endpoint is used exactly as typed, so a relay with another path works too.
    """
    url = (url or "").strip().rstrip("/")
    if not url:
        return ""
    if url.endswith("/chat/completions"):
        return url
    return url + "/chat/completions"


def _messages(cfg, messages):
    """The system prompt + the client's turns (roles clamped, text only)."""
    system = {"role": "system",
              "content": prompts.get("assistant.system", SYSTEM_PROMPT)}
    turns = []
    for item in messages or ():
        if not isinstance(item, dict):
            continue
        role = item.get("role") if item.get("role") in ("user", "assistant") \
            else "user"
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        turns.append({"role": role, "content": content[:12000]})
    return [system] + turns


def _effort_fields(effort):
    """The body fields that set a reasoning budget on the common relays.

    OpenRouter reads ``reasoning.effort``; most others either ignore it or
    read ``reasoning_effort`` — sending both is harmless and covers more.
    """
    effort = (effort or "").strip().lower()
    if effort not in EFFORTS or not effort:
        return {}
    return {"reasoning": {"effort": effort}, "reasoning_effort": effort}


def payload_for(cfg, messages, model=None, provider=None):
    """The request body for one chat turn (also the unit tests inspect)."""
    if provider is None or model is None:
        chosen_provider, chosen_model = active(cfg)
        provider = provider or chosen_provider
        model = model or chosen_model
    body = {"model": model or "",
            "stream": True,
            "stream_options": {"include_usage": True},
            "temperature": 0.4,
            "max_tokens": MAX_TOKENS,
            "messages": _messages(cfg, messages)}
    body.update(_effort_fields(getattr(cfg, "assistant_effort", "")))
    return body


def _usage_events(usage, started, text, thinking):
    """Our 'usage' event from an upstream usage block (tokens + speed)."""
    if not isinstance(usage, dict):
        return []
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or (prompt_tokens + completion))
    details = usage.get("completion_tokens_details") or {}
    reasoning = int((details or {}).get("reasoning_tokens") or 0)
    seconds = max(0.001, time.monotonic() - started)
    return [{"type": "usage",
             "prompt_tokens": prompt_tokens,
             "completion_tokens": completion,
             "total_tokens": total,
             "reasoning_tokens": reasoning,
             "seconds": round(seconds, 3),
             "tokens_per_second": round(completion / seconds, 2),
             "chars": len(text or ""),
             "thinking_chars": len(thinking or "")}]


def stream(cfg, messages, override=None):
    """Yield our own events: {"type": "thinking"|"text"|"usage"|"error"|"done"}.

    Blocking generator — the endpoint hands it to StreamingResponse, which
    iterates it off the event loop. Never raises: every failure is an event,
    so a dead endpoint shows a line in the chat instead of a 500 page.
    """
    override = STREAM_OVERRIDE if override is None else override
    provider, model = active(cfg)
    payload = payload_for(cfg, messages, model=model, provider=provider)
    if override is not None:
        for event in override(cfg=cfg, payload=payload):
            yield event
        return

    url = endpoint(provider.get("base_url", ""))
    if not url or not payload["model"]:
        yield {"type": "error",
               "message": "The assistant has no active provider/model — add "
                          "one in its settings (ASSISTANT_PROVIDERS / "
                          "ASSISTANT_URL / ASSISTANT_MODEL in .env)."}
        yield {"type": "done"}
        return
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    key = provider.get("api_key", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    started = time.monotonic()
    text_parts, think_parts = [], []
    usage_seen = False
    try:
        resp = requests.post(url, json=payload, headers=headers,
                             stream=True, timeout=TIMEOUT)
    except requests.RequestException as exc:
        yield {"type": "error", "message": "Assistant unreachable: %s" % exc}
        yield {"type": "done"}
        return
    if resp.status_code != 200:
        body = (resp.text or "")[:400]
        yield {"type": "error",
               "message": "Assistant endpoint returned HTTP %s: %s"
                          % (resp.status_code, _readable_403(body) or resp.reason)}
        yield {"type": "done"}
        return

    try:
        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            line = line.strip()
            if not line:
                continue
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except ValueError:
                    continue
            elif line.startswith("{"):
                # a relay that ignored stream=true answers with one JSON body
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
            else:
                continue
            for event in _events(obj):
                if event["type"] == "thinking":
                    think_parts.append(event.get("text") or "")
                elif event["type"] == "text":
                    text_parts.append(event.get("text") or "")
                yield event
            usage = _usage_events(obj.get("usage") if isinstance(obj, dict) else None,
                                  started, "".join(text_parts),
                                  "".join(think_parts))
            if usage:
                usage_seen = True
                for event in usage:
                    yield event
    except requests.RequestException as exc:
        yield {"type": "error", "message": "Assistant stream failed: %s" % exc}
    if not text_parts and not think_parts:
        yield {"type": "error",
               "message": "The assistant returned an empty reply."}
    if not usage_seen and (text_parts or think_parts):
        for event in _usage_events({}, started, "".join(text_parts),
                                   "".join(think_parts)):
            yield {"type": "usage", "estimated": True, **event}
    yield {"type": "done"}


def _readable_403(body):
    """OpenRouter's 403 is usually 'how to get access', not 'bad key'."""
    if not body:
        return ""
    if "openrouter.ai" in body or "access" in body.lower() or "free" in body.lower():
        return ("%s — OpenRouter answers 403 when this key cannot use the "
                "model (free models need allowed providers / added credits). "
                "Point the assistant at 9router instead, or add credits."
                % body)
    return body


def _events(obj):
    """One upstream chunk -> our text/thinking events (OpenAI-compatible)."""
    choices = obj.get("choices") if isinstance(obj, dict) else None
    if isinstance(choices, list) and choices:
        delta = (choices[0] or {}).get("delta") or (choices[0] or {}).get("message") or {}
        if isinstance(delta, dict):
            think = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(think, dict):
                think = think.get("content") or think.get("text")
            if isinstance(think, str) and think:
                yield {"type": "thinking", "text": think}
            text = delta.get("content")
            if isinstance(text, list):
                text = "".join(part.get("text") or "" for part in text
                               if isinstance(part, dict))
            if isinstance(text, str) and text:
                yield {"type": "text", "text": text}
        return
    # some relays stream bare {"text": "..."} or {"output_text": "..."}
    for name in ("output_text", "text"):
        text = obj.get(name) if isinstance(obj, dict) else None
        if isinstance(text, str) and text:
            yield {"type": "text", "text": text}
            return


def config_view(cfg):
    """The panel's own view of the config — masked, never the raw key."""
    providers = _providers(cfg)
    provider, model = active(cfg, providers)
    return {"ok": True,
            "configured": bool(provider.get("base_url") and model),
            "url": provider.get("base_url", ""),
            "url_host": (provider.get("base_url", "") or "").split("://")[-1]
                        .split("/")[0],
            "provider": provider.get("label", ""),
            "model": model,
            "model_id": (getattr(cfg, "assistant_model", "") or "").strip(),
            "effort": (getattr(cfg, "assistant_effort", "") or "").strip(),
            "efforts": [e for e in EFFORTS],
            "key_set": bool(provider.get("api_key")),
            "key_masked": _mask(provider.get("api_key", "")),
            "providers": [{"label": p["label"], "base_url": p["base_url"],
                           "models": list(p["models"]),
                           "api_key_set": bool(p["api_key"]),
                           "api_key_masked": _mask(p["api_key"])}
                          for p in providers],
            "max_tokens": MAX_TOKENS}


def model_id(label, model):
    """The stored "label::model" id for one pick."""
    return ("%s::%s" % ((label or "").strip(), (model or "").strip())).strip(":")


def split_id(value):
    """("label", "model") from a stored model id (model may carry '::')."""
    label, _, model = (value or "").partition("::")
    return label, model


def clean_model_name(raw):
    """Trim a pasted model id; keep the provider prefix, drop stray quotes."""
    return re.sub(r"^[\s'\"`]+|[\s'\"`]+$", "", str(raw or ""))


def fetch_models(provider):
    """The provider's own /models list (best effort — never raises)."""
    import types

    from .vision import check_provider
    shim = types.SimpleNamespace(
        ocr_base_url=(provider.get("base_url") or "").strip(),
        ocr_api_key=(provider.get("api_key") or "").strip())
    try:
        return check_provider(shim)
    except Exception as exc:  # noqa: BLE001 — a probe is never fatal
        return {"ok": False, "models": [], "error": str(exc)}
