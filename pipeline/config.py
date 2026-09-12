"""Loads every setting from the .env file into one typed object."""
import os
from dataclasses import dataclass
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


def _router_provider(base_url: str) -> str:
    """Explicit ROUTER_PROVIDER wins; otherwise infer: loopback URLs are a
    'local' server, anything else is treated as a hosted OpenAI-compatible
    endpoint (which usually wants an API key)."""
    p = os.getenv("ROUTER_PROVIDER", "").strip().lower()
    if p in ("local", "openai"):
        return p
    u = (base_url or "").lower()
    return "local" if ("localhost" in u or "127.0.0.1" in u or "::1" in u) else "openai"

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
    gemini_key_pool: tuple
    gemini_key_names: tuple
    gemini_model_ladder: tuple
    host: str
    port: int
    router_provider: str
    router_base_url: str
    router_api_key: str
    router_model: str
    revision_batch_limit: int
    revision_chunk_size: int
    revision_scan_chunk: int


def load_config() -> Config:
    return Config(
        input_root=os.getenv("INPUT_ROOT", DEFAULT_INPUT_ROOT),
        min_age_seconds=int(os.getenv("MIN_AGE_SECONDS", "60")),
        supabase_url=os.getenv("SUPABASE_URL", "").rstrip("/"),
        supabase_key=os.getenv("SUPABASE_SERVICE_KEY", ""),
        supabase_bucket=os.getenv("SUPABASE_BUCKET", "konkour-pages"),
        postgres_host=os.getenv("POSTGRES_HOST", "localhost"),
        postgres_port=int(os.getenv("POSTGRES_PORT", "5432")),
        postgres_db=os.getenv("POSTGRES_DB", "postgres"),
        postgres_user=os.getenv("POSTGRES_USER", "postgres"),
        postgres_password=os.getenv("POSTGRES_PASSWORD", ""),
        # Multi-key / multi-model Gemini routing (see pipeline.gemini_router).
        gemini_key_pool=_gemini_pool(),
        gemini_key_names=_csv(os.getenv("GEMINI_KEY_NAMES", "")),
        gemini_model_ladder=_model_ladder(),
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", "8080")),
        router_base_url=os.getenv("ROUTER_BASE_URL", "http://localhost:20128/v1"),
        router_provider=_router_provider(os.getenv("ROUTER_BASE_URL",
                                                   "http://localhost:20128/v1")),
        router_api_key=os.getenv("ROUTER_API_KEY", ""),
        router_model=os.getenv("ROUTER_MODEL", "OCR-Quest"),
        revision_batch_limit=int(os.getenv("REVISION_BATCH_LIMIT", "40")),
        revision_chunk_size=int(os.getenv("REVISION_CHUNK_SIZE", "8")),
        revision_scan_chunk=int(os.getenv("REVISION_SCAN_CHUNK", "50")),
    )
