# Konkour OCR

OCR pipeline and live dashboard for building a searchable question/answer bank
from scanned exam material. PDFs are rasterised to images, a FastAPI server
scans the watched folder tree, sends each item to the Gemini API for
extraction/revision, stores the results in PostgreSQL (or Supabase), and
streams progress to a browser dashboard over WebSocket.

## Features

- **FastAPI server + dashboard** (`main.py`, `ui/`) — start/stop OCR and
  revision runs, review extracted rows, live WebSocket progress, image proxy.
- **Gemini extraction** (`pipeline/`) — separate key/model ladders for
  questions and answers, multi-credential routing with usage caps
  (`pipeline/gemini_router.py`), retry and deferral handling for offline or
  failing runs (`pipeline/deferrals.py`).
- **Revision pipeline** (`pipeline/revision/`) — batch audit, polish,
  completeness, and pairing passes over extracted content.
- **Data console + backups** (`pipeline/dataconsole.py`, `pipeline/backups.py`)
  — guarded SQL console and weekly database snapshots kept in `backups/`
  (never committed).
- **PDF → image converter** (`converter/`) — optional Docker watcher that
  rasterises PDFs into the same tree the Python pipeline reads.
- **Sample tree** — a small `konkour-ocr/` example is included so the scanner
  tests run out of the box.

## Requirements

- Python 3.14 (`start.bat` bootstraps `.venv` with the `py` launcher)
- PostgreSQL (or Supabase Postgres) reachable via `POSTGRES_*` env vars
- Google Gemini API keys
- Docker (only for the PDF converter)

## Quick start

Windows: double-click or run `start.bat`. It creates `.venv`, installs
requirements, copies `.env.example` to `.env` (and opens it for editing), then
launches the dashboard at `http://localhost:8080`.

Manually:

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt
cp .env.example .env   # then fill in credentials
.venv/Scripts/python main.py
```

## Configuration

All settings live in `.env` (git-ignored; `.env.example` documents every key).
Main groups:

| Group | Keys | Notes |
|-------|------|-------|
| Input tree | `INPUT_ROOT`¹, `MIN_AGE_SECONDS` | watched folder root, defaults to `./konkour-ocr` |
| Database | `POSTGRES_HOST/PORT/DB/USER/PASSWORD/SSLMODE`, `SUPABASE_URL/SERVICE_KEY/BUCKET` | Supabase is used for image storage when set; blank `SSLMODE` keeps the driver default |
| Gemini | `GEMINI_API_KEY`, `GEMINI_API_KEY_QUESTIONS/ANSWERS`, `GEMINI_MODEL_QUESTIONS/ANSWERS` | per-lane keys and models |
| Router | `ROUTER_BASE_URL/API_KEY/MODEL` | optional OpenAI-compatible front for Gemini |
| Providers / proxy | `EXTRACTION_PROVIDERS`, `EXTRACTION_ACTIVE`, `REVISION_PROVIDERS`, `REVISION_ACTIVE`, `PROXY_PROFILES`, `PROXY_ACTIVE` | written by the dashboard; legacy `ROUTER_*` / `OCR_*` / `GEMINI_API_KEY*` are still read when absent |
| Server | `HOST` (default `0.0.0.0`), `PORT` (default `8080`) | set `HOST=127.0.0.1` on shared networks — the dashboard has no authentication |
| Network retries | `NET_RETRIES`, `NET_RETRY_WAIT` | |
| Revision | `REVISION_BATCH_LIMIT`, `REVISION_CHUNK_SIZE` | |
| Converter | `CONVERTER_DPI`, `CONVERTER_RASTER_QUALITY`, `CONVERTER_JPEG_QUALITY`, `CONVERTER_MIN_AGE_MINUTES`, `CONVERTER_SCAN_INTERVAL_SECONDS` | also read by `docker-compose.converter.yml` |
| Backups | `BACKUP_INTERVAL_DAYS`, `BACKUP_KEEP` | |

¹ `INPUT_ROOT` is read from the environment but not listed in `.env.example`; the
default (`./konkour-ocr`) is used when it is unset.

## Input tree layout

```text
konkour-ocr/
└── {subject}/{grade}/{topic}/{question|answer}/
    ├── meta.json
    └── images (.jpg/.jpeg/.png/.webp) or PDFs
```

The grade level is optional. Processed sources move to `done/` (failures to
`failed/`) and are skipped by later scans.

## PDF converter (optional)

```bash
docker compose -f docker-compose.converter.yml up -d --build
```

See [converter/README.md](converter/README.md) for the folder flow and tuning
notes. Keep `MIN_AGE_SECONDS` at its default so scans don't race the converter.

## Tests

```bash
.venv/Scripts/python -m unittest discover -s tests
```

166 tests, no network or database access required (DB-dependent paths are
stubbed or skipped).

## Project layout

```text
main.py            FastAPI app, WebSocket hub, dashboard endpoints
pipeline/          config, scanner, db, gemini, runner, deferrals,
                   dataconsole, backups, revision/ (audit, polish, batch, …)
ui/                dashboard front-end (static, served by main.py)
converter/         Docker PDF→JPEG watcher
tests/             unittest suite
tools/ocr.ps1     PowerShell controller (ocr start/stop/status/open)
```

## Local state (intentionally not committed)

`.env`, `backups/`, `.gemini-usage.json`, `.pending-deferrals.json*`,
`.venv/`, and `__pycache__/` are git-ignored. The pending-deferrals file is a
write-cache replayed into Postgres at the start of the next run.
