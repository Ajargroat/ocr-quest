"""One call lane for every studio stage (model picker + proxy aware).

Stage → provider mapping follows the studio-local selectors saved in `.env`:

- a 9router label  → that provider's OpenAI-compatible endpoint (loopback);
- "" or missing    → the Gemini key pool.

The studio proxy choice applies to the Gemini lane only — the same rule the
pipeline's custom lane follows (9router is loopback by design).

`CALL_OVERRIDE` is the seam tests set to a callable, so no unit test ever
reaches the network.
"""
import copy
import time
import types

from pipeline import faults
from pipeline import gemini as gemini_mod
from pipeline.gemini_router import router
from pipeline.vision import call_ocr_openai

CALL_OVERRIDE = None

_STAGE_KEYS = {"extract": "studio_extract_active",
               "enrich": "studio_enrich_active",
               "vision": "studio_vision_active"}


def active_label(cfg, stage):
    """The provider label the Models panel selected for one stage ('' = pool)."""
    return (getattr(cfg, _STAGE_KEYS.get(stage, ""), "") or "").strip()


def job_prompt(cfg, stage):
    """This job's own extra instruction for one stage (SPARE; '' = none).

    Per-job overrides ride on a config copy, so the shared config never sees
    them — `lane.lane_cfg` is the only writer.
    """
    wanted = stage
    mapping = getattr(cfg, "studio_gemini_model_prompt", None) or {}
    if not isinstance(mapping, dict):
        return ""
    return (mapping.get(wanted) or "").strip()


def stage_prompt(cfg, stage, prompt):
    """``prompt`` with the job's own instruction appended for the model call."""
    extra = job_prompt(cfg, stage)
    if not extra:
        return prompt
    return prompt.rstrip() + "\n\n" + extra


def choose_model(provider, model=""):
    """The provider dict with ``model`` picked (a copy), or the entry as-is.

    A pick is honoured whether or not the provider's ``models_enabled`` list
    contains it — the pick is explicit. An empty pick keeps the lane's current
    first/model behaviour, so an old save means exactly what it did before.
    """
    want = (model or "").strip()
    if not want:
        return provider
    picked = dict(provider)
    picked["model"] = want
    men = picked.get("models_enabled")
    if isinstance(men, list) and want not in men:
        picked["models_enabled"] = list(men) + [want]
    return picked


def provider_for(cfg, label):
    """The studio-local provider dict with this label, or {}."""
    for entry in getattr(cfg, "studio_providers", ()) or ():
        if isinstance(entry, dict) and entry.get("label") == label:
            return entry
    if label == "9router":
        # relabelled by an older save: the built-in loopback endpoint is the
        # only provider this list can hold, so its URL decides
        for entry in getattr(cfg, "studio_providers", ()) or ():
            url = (entry.get("base_url") or "") if isinstance(entry, dict) else ""
            if "127.0.0.1:20128" in url or "localhost:20128" in url:
                return entry
    return {}


def _shim(provider):
    """A minimal cfg object the OpenAI-compatible lane understands."""
    models = provider.get("models") or []
    men = provider.get("models_enabled")
    allowed = [m for m in models if men is None or m in men]
    want = (provider.get("model") or "").strip()
    if men is not None and want and want not in (men or []):
        want = ""
    return types.SimpleNamespace(
        ocr_base_url=provider.get("base_url") or "",
        ocr_api_key=provider.get("api_key") or "",
        ocr_model=want or (allowed[0] if allowed else (models[0] if models else "")),
    )


def _studio_profile(cfg):
    """The proxy profile the studio selector names (None = direct)."""
    name = (getattr(cfg, "studio_proxy", "") or "").strip()
    if not name:
        return None
    for prof in getattr(cfg, "proxy_profiles", ()) or ():
        if prof.get("name") == name:
            return prof
    return None


def call_stage(cfg, stage, prompt, data_b64=None, mime_type=None,
               temperature=0.1, max_tokens=16384, raw=False,
               images=None, labels=None, json_mode=False, thinking=None):
    """One stage call → ``{"text", "parsed", "route", "lane"}``.

    ``images`` is a list of base64 blobs sent as separate image parts in one
    request; ``labels`` names each one (the band pass answers per label).
    ``data_b64`` stays the single-image spelling. ``json_mode`` asks Gemini for
    a JSON body; ``thinking`` caps its reasoning budget (0 for bbox work).
    ``raw=True`` returns the verbatim reply on both lanes (the OCR lane wants
    text, not JSON). Raises ``faults.FaultError`` exactly like the lanes do.
    """
    payload = images if images is not None else data_b64
    prompt = stage_prompt(cfg, stage, prompt)
    if CALL_OVERRIDE is not None:
        return CALL_OVERRIDE(cfg=cfg, stage=stage, prompt=prompt,
                             data_b64=payload, mime_type=mime_type, raw=raw,
                             images=images, labels=labels)

    label = active_label(cfg, stage)
    provider = provider_for(cfg, label) if label else {}
    if provider:
        shim = _shim(provider)
        if not shim.ocr_base_url or not shim.ocr_model:
            raise faults.FaultError(
                faults.fault("unknown"),
                "Studio provider '%s' needs a base URL and a model." % label)
        started = time.monotonic()
        out = call_ocr_openai(shim, prompt, payload, mime_type,
                              temperature=temperature, max_tokens=max_tokens,
                              raw_text=raw, labels=labels)
        try:
            router._calls_append(
                shim.ocr_api_key, shim.ocr_model,
                ms=(time.monotonic() - started) * 1000.0,
                key_name=label, ok=True, source="studio")
        except Exception:
            pass
        return {"text": out if raw else "", "parsed": None if raw else out,
                "route": {"label": label, "key": "9router",
                          "model": shim.ocr_model},
                "lane": label}

    # Gemini pool — the studio proxy applies around this call only.
    saved = gemini_mod._proxy_url["url"]
    lane_cfg = cfg
    vision_model = (getattr(cfg, "studio_vision_model", "") or "").strip()
    studio_pin = (getattr(cfg, "studio_gemini_pin", "") or "").strip()
    # Pin this lane to ONE model when one is chosen: the locate stage keeps its
    # own pin (flash-lite is cheap but reads small diagrams less reliably),
    # otherwise the Studio Models panel's Gemini picker decides.
    pin = vision_model if (stage == "vision" and vision_model) else studio_pin
    if pin:
        lane_cfg = copy.copy(cfg)
        lane_cfg.gemini_model_ladder = (pin,)
        # the pipeline tab's own pin must not filter this lane's one-model ladder
        lane_cfg.gemini_pin = ""
    try:
        gemini_mod._apply_profile(_studio_profile(cfg))
        parsed, text, route = router.call(lane_cfg, prompt, payload, mime_type,
                                          temperature=temperature,
                                          max_tokens=max_tokens,
                                          labels=labels, json_mode=json_mode,
                                          thinking=thinking, source="studio")
    finally:
        gemini_mod._proxy_url["url"] = saved
    return {"text": text or "", "parsed": parsed, "route": route,
            "lane": "gemini"}
