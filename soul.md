# SOUL — Konkour OCR (OCR-quest)

> Agent handoff: explain intent and boundaries before editing. This file is
> concise orientation, not a substitute for checking the relevant code/tests.
> Code wins over this document; update this document when verified behaviour
> changes.

## Purpose
Turn scanned Iranian Konkour question booklets and answer keys into a
searchable, linked, human-reviewed question bank consumed by the Sapienstech
Laravel app. The quality bar is trustworthy data, not maximum extraction
throughput.

## Core principles
- Never guess missing question, option, answer, caption, table cell, or tag.
- Preserve Persian text and ZWNJ (U+200C), RTL semantics, and math notation.
- Deterministic code first; ambiguous OCR, metadata, and links go to a human.
- Human decisions are durable: do not overwrite `approved`/`rejected`
  review states or erase an existing answer link during re-import.
- Writes must be idempotent and failures resumable; never delete live data or
  storage objects as an unreviewed cleanup.
- Secrets stay in `.env`; tests should stub network/database boundaries.
- No schema change without explicit owner approval. Add JSON keys; do not
  repurpose them. See `docs/db-contract.md`.

## Current architecture (feature baseline now on main)
- `main.py`: FastAPI dashboard/API and run orchestration.
- `pipeline/scanner.py`: source tree discovery and path-derived metadata.
- `pipeline/runner.py`: serial/parallel OCR workflow and event reporting.
- `pipeline/questions.py`, `pipeline/answers.py`, `pipeline/prompts.py`:
  extraction prompts and row preparation.
- `pipeline/gemini_router.py`, `pipeline/gemini.py`, `pipeline/vision.py`:
  model routing and OCR/vision calls.
- `pipeline/db.py`: database boundary for the flat `sources`,
  `questions`, `answers` core.
- `pipeline/deferrals.py`: durable deferred writes.
- `pipeline/recrop.py`, `pipeline/uploads.py`: legacy crop/source handling.
- `pipeline/studio/`, `ui/studio.js`, and `/api/studio/*`: independent
  seven-stage Studio pipeline, job workspace, inspect/crop and commit/import
  workflow. It is currently part of the branch baseline; do not remove it
  without the staged-import plan's explicit port-and-test sequence.
- `ui/`: vanilla JavaScript/HTML/CSS; no build step.

## Database and consumer constraints
The live Supabase project `sapienstech` has the flat tables:
`sources` (2,089), `questions` (3,931), `answers` (3,042) as observed
2026-10-25. `questions.id` is numeric and `legacy_id` is the stable old
text identity. Preserve IDs and links because Sapienstech reads this DB.
The old Supabase project still holds source-page objects referenced by
`sources.storage_url`; do not treat it as disposable until source pages have
been archived and verified.

## Staging/import target
OCR should write to local SQLite staging, not Postgres. Human inspection edits
staged records and marks rows ready; only the import step writes live rows.
Inspected/edited rows enter as `approved`. Publish crops before rows that
reference them. Re-crops replace deterministic object names. Figures remain
pictures with their captions. Tables need human-verified pipe-table text (with
math in `$...$`) for HTML table rendering, while retaining the crop as
evidence/fallback. A table that cannot be transcribed confidently remains an
image and is flagged for human review.

## Matching rules
Normalize year digits/separators and institute spelling through an explicit
vocabulary/alias map before matching. First match only on a unique
(question number + canonical institute + year) key. Number-only matching is
allowed only when unique on both sides and non-contradictory. Otherwise require
manual review; never use first-match-wins across exams.

## Safety and verification
- Run `.venv\\Scripts\\python -m unittest discover -s tests` on Windows
  (or `python -m unittest discover -s tests` elsewhere).
- Restart the backend after `main.py` or `pipeline/*` changes.
- Keep console/log strings cp1256-safe; no emoji/check/arrows in printed logs.
- Never touch live `workspace/`, `backups/`, or source-tree contents during a
  running job. Do not auto-delete orphan crops or reconcile the 20 extra old
  answers without owner review.
- Before mass import: back up live DB, record counts, export a bucket manifest,
  run matching dry-run, reconcile old/live rows, and keep an audit log for
  every relink.
