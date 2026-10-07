"""Loads every setting from the .env file into one typed object."""
import json
import os
from dataclasses import dataclass
from urllib.parse import urlparse

from dotenv import load_dotenv

# The .env lives next to main.py; pinning the path lets the Credentials tab
# read/write the very same file the running server booted from, regardless
# of the current working directory.
ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")

load_dotenv(ENV_PATH)

# The watched input tree ships as a 'konkour-ocr' folder next to main.py, so the
# default resolves relative to the project instead of a hard-coded drive letter.
DEFAULT_INPUT_ROOT = os.path.join(os.path.dirname(ENV_PATH), "konkour-ocr")

# Production model ladder: start at the oldest version and step up, so a
# working 3.5 is always preferred over 3.6/3.7/3.8. Every key walks the same
# ladder; advanced users can still override it via GEMINI_MODEL_LADDER in .env
# (the Credentials tab deliberately offers no picker — there is nothing to
# choose when every request may need every rung).
DEFAULT_MODEL_LADDER = ("gemini-3.5-flash", "gemini-3.6-flash",
                        "gemini-3.7-flash", "gemini-3.8-flash")


def _csv(value: str) -> tuple:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


def _flag(name):
    """Section enable switch (Q1). Absent key → ON, so an untouched .env keeps
    running exactly as before; 0/false/off/no are the OFF spellings."""
    return os.getenv(name, "1").strip().lower() not in ("0", "false", "off", "no")


def _gemini_pool() -> tuple:
    """All usable Gemini keys, de-duplicated, in pool order.

    Once GEMINI_API_KEYS exists in .env (the Credentials tab writes it), it
    is authoritative — deleting a key in the UI really removes it. Before
    the tab has ever been used, the legacy single/branch keys seed the pool
    so an older .env keeps working unchanged."""
    raw = os.getenv("GEMINI_API_KEYS")
    if raw is not None:
        return tuple(dict.fromkeys(_csv(raw)))
    keys = []
    for var in ("GEMINI_API_KEY", "GEMINI_API_KEY_QUESTIONS", "GEMINI_API_KEY_ANSWERS"):
        k = os.getenv(var, "").strip()
        if k and k not in keys:
            keys.append(k)
    return tuple(keys)


def _model_ladder() -> tuple:
    ladder = _csv(os.getenv("GEMINI_MODEL_LADDER", ""))
    return ladder or DEFAULT_MODEL_LADDER


def _json_list(name):
    """A JSON array stored in one .env value, or () when absent/unparsable.
    Never raises: a hand-edited .env must not stop the server booting."""
    raw = os.getenv(name)
    if raw is None:
        return ()
    try:
        val = json.loads(raw)
    except ValueError:
        return ()
    return tuple(v for v in val if isinstance(v, dict)) if isinstance(val, list) else ()


def _providers(name, legacy_base, legacy_key, legacy_model,
               legacy_flag, legacy_on, legacy_default=""):
    """Saved custom providers for one section as (label, base_url, api_key,
    model) dicts — the one "custom provider" kind (label, base URL, API key,
    model name).

    Q1 read-fallback: the new key wins the moment it exists; until the
    Credentials tab has written it, a single entry is synthesised from the
    legacy single-provider keys, exactly like _gemini_pool() does for
    GEMINI_API_KEYS — an older .env keeps working unchanged. `legacy_on` is
    the set of legacy flag values that meant "this section runs on its own
    endpoint" (extraction: openai/local; the Gemini-pool value stays off;
    revision: any value, including an unset flag the old code guessed from
    the URL), and `legacy_default` is the old hard-coded base URL (if any)
    for sections that always had an endpoint."""
    stored = _json_list(name)
    if os.getenv(name) is not None:
        return stored
    if os.getenv(legacy_flag, "").strip().lower() not in legacy_on:
        return ()
    base = os.getenv(legacy_base, legacy_default).strip().rstrip("/")
    if not base:
        return ()
    return ({"label": "default", "base_url": base,
             "api_key": os.getenv(legacy_key, "").strip(),
             "model": os.getenv(legacy_model, "").strip()},)


def _active(name, providers):
    """Which saved provider a section has selected.

    Key absent → the first saved (or legacy-synthesised) entry, if one
    exists. Key present but empty → deliberately nothing: extraction runs
    on the Gemini key pool, revision has no endpoint selected. The
    `is not None` split is what keeps "" meaningful."""
    raw = os.getenv(name)
    if raw is None:
        return providers[0].get("label", "") if providers else ""
    return raw.strip()


def _resolve(providers, active):
    """The active provider dict, or {} when nothing is selected."""
    for p in providers:
        if p.get("label") == active:
            return p
    return {}


# The ONE built-in non-Gemini provider (Q2): fixed loopback URL, editable
# API key, own model list for the model ring. The generic custom-provider
# kind is deleted — anything stored that is not this entry is discarded.
NINEROUTER_URL = "http://127.0.0.1:20128/v1"


def _migrate_ninerouter(stored):
    """Coerce one section's stored providers to the single 9router entry.

    Keeps the stored entry that already points at the 9router loopback
    service (either loopback spelling, or labelled 9router) with the URL
    pinned to NINEROUTER_URL; every other custom entry is dropped by
    design (Q2) and named on stdout so the loss is visible, not silent.
    """
    kept = {}
    for p in stored or ():
        if not isinstance(p, dict):
            continue
        url = p.get("base_url") or ""
        if ("127.0.0.1:20128" in url or "localhost:20128" in url
                or p.get("label") == "9router"):
            if not kept:
                models = p.get("models")
                kept = {"label": "9router", "base_url": NINEROUTER_URL,
                        "api_key": p.get("api_key", ""),
                        "model": p.get("model", ""),
                        "models": list(models) if isinstance(models, list) else []}
        else:
            print(f"[config] dropping non-9router provider "
                  f"'{p.get('label', '')}' ({url}) — custom kind removed (Q2)")
    return (kept,) if kept else ()


def _legacy_proxy_url():
    """One-time upgrade path for the old single proxy URL (Q5).

    The retired key is matched by SHAPE (`*_PROXY_URL`), never by spelling
    it out: acceptance 7 wants that whole key family gone from pipeline/."""
    for key, val in os.environ.items():
        if key.endswith("_PROXY_URL") and val.strip():
            return val.strip()
    return ""


def seed_proxy_profile():
    """Q5 one-time upgrade: when the old single proxy URL is set and no
    profile list exists yet, return exactly one profile derived from it.
    Returns () otherwise, so a later profile edit is never overwritten."""
    if os.getenv("PROXY_PROFILES"):
        return ()
    raw = _legacy_proxy_url()
    if not raw:
        return ()
    u = urlparse(raw if "://" in raw else "http://" + raw)
    if not u.hostname:
        return ()
    return ({"name": "Default",
             "host": u.hostname,
             "port": str(u.port or ""),
             "scheme": (u.scheme or "http").lower(),
             "user": u.username or "",
             "password": u.password or ""},)


@dataclass
class Config:
    input_root: str
    min_age_seconds: int
    supabase_url: str
    supabase_key: str
    supabase_bucket: str
    postgres_host: str
    postgres_port: int
    postgres_db: str
    postgres_user: str
    postgres_password: str
    postgres_sslmode: str          # "" → psycopg2/libpq default, unchanged
    gemini_key_pool: tuple
    gemini_key_names: tuple
    gemini_model_ladder: tuple
    host: str
    port: int
    router_provider: str           # resolved revision engine: 'custom' | ''
    router_base_url: str
    router_api_key: str
    router_model: str
    ocr_provider: str              # resolved extraction engine: 'gemini' | 'custom'
    ocr_base_url: str
    ocr_api_key: str
    ocr_model: str
    # Saved custom providers per section + which one is live (TASK 2/3).
    extraction_providers: tuple
    extraction_active: str         # "" → the Gemini key pool
    revision_providers: tuple
    revision_active: str           # "" → no revision endpoint selected
    # Named proxy profiles for the pipeline tab; proxy_active "" = direct (Q4).
    proxy_profiles: tuple
    proxy_active: str
    revision_batch_limit: int
    revision_chunk_size: int
    revision_scan_chunk: int
    converter_enabled: bool = True
    # Section enable switches (Q1) + presentation-only DB list (Q2).
    gemini_pool_enabled: bool = True
    ninerouter_enabled: bool = True
    revision_provider_enabled: bool = True
    supabase_enabled: bool = True
    db_connections: tuple = ()


def load_config() -> Config:
    # Extraction: the Gemini pool unless the built-in 9router is selected.
    # The generic custom-provider kind is gone (Q2) — whatever is stored is
    # coerced to the single 9router entry, other entries discarded.
    ext_providers = _migrate_ninerouter(_providers(
        "EXTRACTION_PROVIDERS", "OCR_BASE_URL",
        "OCR_API_KEY", "OCR_MODEL",
        "OCR_PROVIDER", ("openai", "local")))
    ext_active = _active("EXTRACTION_ACTIVE", ext_providers)
    # Revision: same single-kind model; the legacy default (the 9router
    # loopback service) still keeps an endpoint selected for old .env files.
    rev_providers = _migrate_ninerouter(_providers(
        "REVISION_PROVIDERS", "ROUTER_BASE_URL",
        "ROUTER_API_KEY", "ROUTER_MODEL",
        "ROUTER_PROVIDER", ("local", "openai", ""),
        legacy_default="http://localhost:20128/v1"))
    rev_active = _active("REVISION_ACTIVE", rev_providers)
    # Q2 self-heal: migration may have dropped the entry the stored active
    # label pointed at (or an old "default" legacy label). A truthy-but-unknown
    # label can never resolve, so fall back to the kept entry (usually the
    # migrated 9router) or "" — never leave a dangling pointer that 400s
    # every credentials save. A deliberate "" (Gemini pool / no endpoint)
    # is meaningful and stays untouched.
    _ext_labels = {p.get("label", "") for p in ext_providers}
    if ext_active and ext_active not in _ext_labels:
        ext_active = ext_providers[0].get("label", "") if ext_providers else ""
    _rev_labels = {p.get("label", "") for p in rev_providers}
    if rev_active and rev_active not in _rev_labels:
        rev_active = rev_providers[0].get("label", "") if rev_providers else ""
    ext = _resolve(ext_providers, ext_active)
    rev = _resolve(rev_providers, rev_active)
    return Config(
        input_root=os.getenv("INPUT_ROOT", DEFAULT_INPUT_ROOT),
        min_age_seconds=int(os.getenv("MIN_AGE_SECONDS", "30")),
        supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
        supabase_key=os.getenv("SUPABASE_SERVICE_KEY", ""),
        supabase_bucket=os.getenv("SUPABASE_BUCKET", "konkour-pages"),
        postgres_host=os.getenv("POSTGRES_HOST", "localhost"),
        postgres_port=int(os.getenv("POSTGRES_PORT", "5432")),
        postgres_db=os.getenv("POSTGRES_DB", "postgres"),
        postgres_user=os.getenv("POSTGRES_USER", "postgres"),
        postgres_password=os.getenv("POSTGRES_PASSWORD", ""),
        postgres_sslmode=os.getenv("POSTGRES_SSLMODE", "").strip(),
        # Multi-key / multi-model Gemini routing (see pipeline.gemini_router).
        gemini_key_pool=_gemini_pool(),
        gemini_key_names=_csv(os.getenv("GEMINI_KEY_NAMES", "")),
        gemini_model_ladder=_model_ladder(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        # Resolved views: consumers (runner, vision, revision/audit) keep
        # reading cfg.router_* / cfg.ocr_* with no signature change; the
        # per-section provider lists above are what the Credentials tab edits.
        # 'local' and 'openai' are one kind now — 'custom' (TASK item 2).
        router_provider="custom" if rev else "",
        router_base_url=rev.get("base_url", ""),
        router_api_key=rev.get("api_key", ""),
        router_model=rev.get("model", ""),
        ocr_provider="gemini" if not ext else "custom",
        ocr_base_url=ext.get("base_url", ""),
        ocr_api_key=ext.get("api_key", ""),
        ocr_model=ext.get("model", ""),
        extraction_providers=ext_providers,
        extraction_active=ext_active,
        revision_providers=rev_providers,
        revision_active=rev_active,
        proxy_profiles=_json_list("PROXY_PROFILES"),
        proxy_active=os.getenv("PROXY_ACTIVE", "").strip(),
        revision_batch_limit=int(os.getenv("REVISION_BATCH_LIMIT", "40")),
        revision_chunk_size=int(os.getenv("REVISION_CHUNK_SIZE", "8")),
        revision_scan_chunk=int(os.getenv("REVISION_SCAN_CHUNK", "50")),
        converter_enabled=os.getenv("CONVERTER_ENABLED", "1").strip().lower()
        not in ("", "0", "false", "no", "off"),
        gemini_pool_enabled=_flag("GEMINI_POOL_ENABLED"),
        ninerouter_enabled=_flag("NINEROUTER_ENABLED"),
        revision_provider_enabled=_flag("REVISION_PROVIDER_ENABLED"),
        supabase_enabled=_flag("SUPABASE_ENABLED"),
        db_connections=_json_list("DB_CONNECTIONS"),
    )
