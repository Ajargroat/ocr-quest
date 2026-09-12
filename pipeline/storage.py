"""Port of the 'Upload to Storage' code nodes (Supabase REST API)."""
import os
import time

import requests

from . import faults
from .config import Config


def upload_file(cfg: Config, object_name: str, data: bytes, mime_type: str,
                retries=None, on_problem=None) -> str:
    """Uploads (upserts) one file to the Supabase bucket.
    Returns the public URL, exactly like the JS node did.

    Tunnel-caused blips are retried with a growing wait, mirroring
    call_gemini's policy; permanent refusals raise a FaultError right away.
    ``on_problem(fault, attempt, retries, wait)`` fires before each sleep.
    """
    if retries is None:
        retries = max(1, int(os.getenv("NET_RETRIES", "3")))
    base_wait = max(0.0, float(os.getenv("NET_RETRY_WAIT", "8")))
    url = f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}/{object_name}"
    headers = {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Content-Type": mime_type,
        "x-upsert": "true",
    }
    public_url = (
        f"{cfg.supabase_url}/storage/v1/object/public/"
        f"{cfg.supabase_bucket}/{object_name}"
    )
    last = faults.fault("unknown")

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, data=data, headers=headers, timeout=180)
        except requests.RequestException as exc:
            last = faults.classify(exc)
        else:
            if response.status_code in (200, 201, 204):
                return public_url
            snippet = response.text[:300]
            last = faults.classify_text(
                f"Supabase upload failed: HTTP {response.status_code} {snippet}")
            last["raw"] = f"HTTP {response.status_code}: {snippet}"
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
