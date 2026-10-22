"""Durable parking for pipeline results that were computed but cannot be
written to Postgres right now.

The expensive half of a run is the Gemini call; losing its output because
the *database* blipped at the last step is unacceptable. When a DB write
still fails after DB_SAVE_RETRIES attempts, the runner parks the finished
write-batch here (a JSON file next to .env), marks the file 'cached', and
moves on to the next file. At the end of the run every parked batch is
re-imported; anything that still cannot be written stays on disk and is
retried again at the end of the next run.

The runner additionally stops the whole progress when
MAX_CONSECUTIVE_DB_FAILURES files in a row end with parked writes — that
pattern means the database is not blipping, it is down, and continuing
would only pile up cache and burn Gemini quota.
"""
import json
import os
import time
from datetime import datetime

from . import faults
from .config import ENV_PATH
from .scanner import SourceItem

CACHE_PATH = os.path.join(os.path.dirname(ENV_PATH), ".pending-deferrals.json")


def _num(name, default):
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


def save_retries():
    """Attempts per write-batch before it is parked (like NET_RETRIES)."""
    return max(1, int(_num("DB_SAVE_RETRIES", 3)))


def retry_wait():
    """Base seconds between those attempts; waits grow exponentially."""
    return max(0.0, float(_num("DB_SAVE_RETRY_WAIT", 5)))


def max_consecutive_failures():
    """Files in a row ending in parked writes before the run halts."""
    return max(1, int(_num("MAX_CONSECUTIVE_DB_FAILURES", 3)))


def load(on_warn=None):
    """All parked batches, oldest first. A corrupted cache is preserved
    (renamed aside) rather than silently destroyed."""
    if not os.path.exists(CACHE_PATH):
        return []
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        quarantine = CACHE_PATH + ".corrupt"
        try:
            os.replace(CACHE_PATH, quarantine)
        except OSError:
            pass
        if on_warn:
            on_warn(f"Unreadable write-cache moved aside to {os.path.basename(quarantine)}")
        return []


def _write(entries):
    """Atomic rewrite; the cache file disappears when fully imported."""
    if entries:
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(entries, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    elif os.path.exists(CACHE_PATH):
        os.remove(CACHE_PATH)


def park(steps, item=None):
    """Append one finished write-batch to the cache (durable until flushed).

    ``steps`` are JSON-safe operation dicts replayed in order:
    {"op": "upsert_source", "item": {...}, "storage_url": ...} /
    {"op": "upsert_questions" | "upsert_answers", "rows": [...]}.
    Order matters: the sources row must exist before its question rows.
    """
    entry = {
        "deferred_at": datetime.now().isoformat(timespec="seconds"),
        "source_id": getattr(item, "source_id", ""),
        "rel_path": getattr(item, "rel_path", ""),
        "steps": steps,
    }
    entries = load()
    entries.append(entry)
    _write(entries)
    return entry


def apply_entry(db, entry):
    """Replay one parked batch against a live Database."""
    for step in entry["steps"]:
        op = step["op"]
        if op == "upsert_source":
            db.upsert_source(SourceItem(**step["item"]), step["storage_url"])
        elif op == "upsert_questions":
            db.upsert_questions(step["rows"])
        elif op == "upsert_answers":
            db.upsert_answers(step["rows"])
        else:
            raise faults.FaultError(
                faults.fault("unknown"), f"Unknown cached op: {op}")


def flush(db, log=None, on_applied=None):
    """Re-import every parked batch with fresh retries.

    Returns ``(applied, still_parked)``. Failed entries keep their place in
    the file, so the next run tries them again.
    """
    def warn(message):
        if log:
            log("warn", message)

    entries = load(on_warn=warn)
    if not entries:
        return 0, 0
    if log:
        log("info", f"Write-cache: importing {len(entries)} parked batch(es)…")
    retries, base = save_retries(), retry_wait()
    applied, parked = 0, []
    for entry in entries:
        done, last = False, None
        for attempt in range(1, retries + 1):
            try:
                apply_entry(db, entry)
            except Exception as exc:
                last = getattr(exc, "fault", None) or faults.classify(exc)
                last["raw"] = last.get("raw") or str(exc)[:400]
                if attempt < retries:
                    wait = faults.backoff_wait(attempt, base)
                    warn(f"  ↳ cached batch {entry.get('rel_path', '?')}: {last['label']} "
                         f"· attempt {attempt}/{retries} failed — retrying in {int(wait)}s")
                    time.sleep(wait)
            else:
                done = True
                break
        if done:
            applied += 1
            if on_applied:
                on_applied(entry)
        else:
            parked.append(entry)
            if log:
                log("error", f"  ↳ still cannot write {entry.get('rel_path', '?')} after "
                             f"{retries} attempts — it stays parked for the next run. "
                             f"{(last or {}).get('hint', '')}")
    _write(parked)
    return applied, len(parked)
