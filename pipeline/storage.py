"""Port of the 'Upload to Storage' code nodes (Supabase REST API)."""
import os
import time
from urllib.parse import quote

import requests

from . import faults
from .config import Config


def proxies_for(cfg: Config):
    """requests `proxies=` for Supabase, or None for a direct connection.

    The storage host is exit-blocked on this network: the active proxy profile
    is the only route that completes a TLS handshake to it, so bucket reads
    and writes must ride the same lane the OCR calls use. An empty
    ``proxy_active`` means Direct, exactly as in the pipeline tab.
    """
    prof = next((p for p in (getattr(cfg, "proxy_profiles", None) or ())
                 if p.get("name") == getattr(cfg, "proxy_active", "")), None)
    if not prof or not prof.get("host"):
        return None
    scheme = (prof.get("scheme") or "http").lower()
    auth = ""
    if prof.get("user"):
        auth = "%s:%s@" % (quote(prof["user"], safe=""),
                           quote(prof.get("password") or "", safe=""))
    url = "%s://%s%s:%s" % (scheme, auth, prof["host"], prof.get("port") or "")
    return {"http": url, "https": url}


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
    base_wait = max(0.0, float(os.getenv("NET_RETRY_WAIT", "5")))
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
            response = requests.post(url, data=data, headers=headers,
                                     timeout=180, proxies=proxies_for(cfg))
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
            wait = faults.backoff_wait(attempt, base_wait)
            if on_problem:
                on_problem(last, attempt, retries, wait)
            time.sleep(wait)

    raise faults.FaultError(
        last, f"{last['label']} after {retries} attempts — {last['hint']} "
              f"(last error: {last['raw'][:200]})")


def upload_bytes(cfg: Config, object_name: str, data: bytes,
                 mime_type: str = "image/jpeg", retries=None,
                 on_problem=None) -> str:
    """Upload one crop object (the importer's only bucket write).

    Same retry policy as ``upload_file``, but the bytes are already in hand -
    a re-cut crop lives in the staging store, not in a file on disk.
    """
    return upload_file(cfg, object_name, data, mime_type, retries=retries,
                       on_problem=on_problem)


def delete_file(cfg: Config, object_name: str) -> bool:
    """Remove one bucket object. Missing is not an error; returns True if gone.

    Supabase's bulk-delete endpoint takes a prefix list under the bucket; a
    404 means there is nothing to delete (a first upload never calls this).
    """
    url = f"{cfg.supabase_url}/storage/v1/object/{cfg.supabase_bucket}"
    headers = {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Content-Type": "application/json",
    }
    resp = requests.delete(url, json={"prefixes": [object_name]},
                           headers=headers, timeout=60,
                           proxies=proxies_for(cfg))
    if resp.status_code in (200, 204):
        return True
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return True

def list_objects(cfg: Config, prefix: str = "", limit: int = 100,
                 offset: int = 0):
    """Read-only listing of one bucket's objects (Supabase Storage list API).

    Returns the raw JSON rows — each carries at least ``name`` (the object
    path, i.e. the ``source_id`` ``main.proxy_image`` expects), plus ``id``,
    ``metadata``, etc. POSTs to ``/storage/v1/object/list/{bucket}`` with the
    service key. No mutation, no bucket picker — one configured bucket."""
    url = f"{cfg.supabase_url}/storage/v1/object/list/{cfg.supabase_bucket}"
    headers = {
        "apikey": cfg.supabase_key,
        "Authorization": f"Bearer {cfg.supabase_key}",
        "Content-Type": "application/json",
    }
    body = {"prefix": prefix or "", "limit": int(limit), "offset": int(offset)}
    resp = requests.post(url, json=body, headers=headers, timeout=30,
                         proxies=proxies_for(cfg))
    resp.raise_for_status()
    rows = resp.json()
    return rows if isinstance(rows, list) else []
