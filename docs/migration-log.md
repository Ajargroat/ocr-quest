# Migration log — OCR-quest

Append-only record of every migration, backfill and bulk import touching the
live Supabase project. Each entry states its evidence, so a later reader can
tell what ran, when, and against which baseline. Never rewrite an entry; add a
new one.

Format: `## <UTC stamp> — <title>` then bullets, then an evidence block.

---

## 2026-10-25T12:45Z — P0 baseline: verified backup, counts, bucket manifest

- Baseline re-measured read-only against the live project
  `jhjwukpaegnbdflquuyk` (Supabase, ap-southeast-1 pooler, `sslmode=require`
  was needed for `psycopg2` where `.env` says `prefer`):
  - `sources` 2,089 · `questions` 3,931 · `answers` 3,042
  - `answers.question_id IS NULL` 1,008
  - `answers.review_status`: `pending` 2,034, `unlinked` 1,008
  - `questions.review_status`: `approved` 2, `pending` 3,927, `rejected` 2
  - `answers` carrying a non-empty `assets` array: 993
  - `answers.media_images` non-empty: 0
  - `answers.year` is free text in both digit systems — `۱۴۰۲-۱۴۰۳` 1,458,
    `1403-1404` 852, `۱۴۰۳-۱۴۰۴` 442, `۱۴۰۴-۱۴۰۵` 116, `1404-1405` 105,
    `1402-1403` 54, `1403_1404` 1, NULL 6 — so canonicalisation is mandatory
    before any matching.
- Backup set `backups/20261025-124513Z/` written by the guarded
  `pipeline.backups.run_backup` path (`trigger=manual-p0`): sources 2,089,
  questions 3,931, answers 3,042 = 9,062 rows / 194 MB including the 248
  source-tree files (148 MB). Set manifest records `server_version 17.11` and
  the per-table `.json` + `.sql` pair for rollback.
- Bucket manifest `backups/bucket-manifest-20261025.json`:
  **539 objects / 9,019,221 bytes** in `konkour-pages`, all under `crops/`.
  - `questions.media_images` references **479** distinct crop objects.
  - `answers.assets` references **0** crop objects (they still point at the old
    project; see the backfill entry when it runs).
  - **60 objects are unreferenced** (539 − 479). Preserved untouched, as
    instructed; no object was deleted by this work.
- Storage egress note: the Supabase Storage host refused TLS from this machine
  (Postgres on the same project answered), and listing succeeded only through
  the active proxy profile `V2ray/pattM` (`http://127.0.0.1:10808`). That is a
  network condition, not a credential fault.
- The old Supabase project was not touched; its source-page objects are still
  referenced by `sources.storage_url`.

```text
suite   : .venv/Scripts/python -m unittest discover -s tests
result  : Ran 811 tests ... OK   (2026-10-25, before staging P2 work)
backup  : backups/20261025-124513Z  rows 2089/3931/3042  total_bytes 194309026
manifest: backups/bucket-manifest-20261025.json  count 539
orphans : 60  (kept)
```

## P2 - staging store (complete)

`pipeline/staging.py` now holds the full local store: `st_sources`,
`st_questions`, `st_answers`, `st_batches`, `st_batch_rows`, with

- the identity contract (`source_id = sha1(rel_path)[:40]`, `<sid>-q<N>`,
  `<sid>-a<N>`);
- statuses `empty, staged, edited, ready, imported, skipped`, and the
  `PROTECTED = (edited, ready, imported, skipped)` guard, so re-OCR can never
  overwrite a human edit or a final state;
- match states `auto_ok, manual_ok, unlinked_ok, needs_manual` and batch
  statuses `staged, reviewed, importing, imported, failed, aborted`;
- per-source assets on disk (`p<N>.jpg`, deterministic crop names, a mirrored
  copy of the source file), written through a temp file + `os.replace`;
- in-place migration of an older `staging.db` (`_MIGRATIONS` adds the missing
  columns rather than dropping the store).

Evidence: `.venv/Scripts/python tests/test_staging.py` -> `Ran 31 tests ... OK`
(identity, empty state, transitions, protection, atomic writes, rollback,
duplicate ids, placeholder source, 40 parallel writers, batch lifecycle).

## P3 - OCR stages locally (complete)

`pipeline/stage.py` (new) is the only bridge an OCR result crosses:

- `page_images()` splits a PDF into one JPEG per page BEFORE OCR, so every
  `diagram_bbox` is measured in the frame of the page it was read from;
- `stage_file()` mirrors those pages, writes the source row and the
  question/answer rows (with the raw OCR JSON kept beside the parsed fields),
  cuts each bbox into a deterministically named crop, and marks an empty
  extraction as `empty` instead of hiding it;
- `candidate_questions()` feeds the answer prompt from the staged bank AND the
  live one, so a fresh unimported batch can link its own answers.

`pipeline/runner.py` no longer uploads to the bucket, upserts Postgres, or
parks writes:

- `_process_item` = render pages -> OCR (one call per page) -> stage -> archive;
  `empty` is a reported outcome, not a success;
- the parallel engine renders pages in the producer and `_produce_round` no
  longer uploads; the import step stages in completion order on the run thread;
- the pre-flight no longer demands `SUPABASE_SERVICE_KEY`, and an unreachable
  Postgres now WARNS instead of aborting the run (the importer needs the DB,
  the OCR run does not);
- `store_failed` now means "the local staging store is unusable" (OSError /
  sqlite3.Error / ValueError), which is the new circuit-breaker signal.

Rewritten pins: `tests/test_deferrals.py::ProcessItemTest` (was: DB writes and
parked writes during OCR; now: zero live writes, staged row + page kept,
`empty` reported, a broken store stops as `store_failed`),
`tests/test_parallel_run.py` (lane phases `ocr/ocr/ocr/staging`, `staged` /
`not staged` terminal phases), and
`tests/test_stage.py::ZeroLiveWriteTests` greps the OCR path for
`upload_file` / `db.upsert_*` / `storage.` / `supabase` so the absence is
enforced, not just observed.

```text
suite   : .venv/Scripts/python -m unittest discover -s tests
result  : Ran 666 tests ... OK   (2026-10-25, after P2 + P3 + P5 matching engine)
```

## P5 - matching engine (complete, offline)

`pipeline/matching.py` decides every link with an explicit ladder: one
candidate matching number + canonical institute + canonical year is `auto_ok`;
a unique number with no contradiction is `unlinked_ok`; anything ambiguous or
contradicted is `needs_manual`. A "first candidate wins" pick is refused by
design. Canonicalisation reuses `pipeline.textnorm.canonical_year` (Persian
digits, `_` separators) and `pipeline.vocab.fold` (ZWNJ/spelling variants).

Evidence: `.venv/Scripts/python tests/test_matching.py` -> `Ran 16 tests ... OK`.
The live dry-run report over the 1 008 unlinked answers is left for the owner
to run (read-only, no writes).

## P1b - Studio retired (complete)

The subsystem is gone, not merely hidden: `pipeline/studio/` (29 files),
`ui/studio.js`, every `/api/studio/*` route in `main.py`, the `studio_*`
config keys, the `ui/index.html` Studio view and its sidebar button, and the
three studio-only test classes in `tests/test_config_default.py`. What was
still worth keeping moved rather than vanished:

- the applied schema migrations now live in `pipeline/migrations/`
  (`consolidate.py`, `consolidate.sql`, `retire_revision.sql`) and are still
  pinned by `tests/test_consolidate.py` (25 tests) - the contract of a
  migration already applied to the live bank must stay verifiable;
- the tag vocabulary, text/math normalisation and the crop/page helpers were
  already salvaged into `pipeline/vocab.py`, `pipeline/textnorm.py`,
  `pipeline/mathnorm.py`, `pipeline/crops.py`, `pipeline/pages.py` in P2;
- `pipeline/converter.py` no longer guards a Studio source tree (the guard
  existed only because Studio owned PDFs inside `INPUT_ROOT`).

Evidence:
- `.venv/Scripts/python tests/test_consolidate.py` -> `Ran 25 tests ... OK`
- `.venv/Scripts/python tests/test_config_default.py` -> `Ran 9 tests ... OK`
- `.venv/Scripts/python tests/test_converter.py` -> `Ran 10 tests ... OK`
- a TestClient probe of the running app: `/` 200, `/api/staging/counts` 200,
  `/api/staging/vocab` 200, `/api/gemini/pin` 200, `/api/stats` 200
  (questions 3931, answers 3042 - live reads still work), and
  `/api/studio/state`, `/api/studio/jobs` -> 404
- `tests/test_staging_api.py` pins the four facts above as tests

## P4 - Inspect UI + /api/staging (complete)

`ui/inspect.js` + the `viewInspect` section in `ui/index.html` replace the
Studio page: a three-stage rail (Sources -> Review -> Import) over the staging
store only. Sources lists every staged source with its counters; Review shows
the page image with the stored bbox drawn in the same 0-1000 frame the OCR
used (drag to re-draw, Re-cut re-renders the crop), the row's text, caption,
table text, subject/topic/corp/year with vocabulary warnings plus alias
acceptance, a live preview (text + crop + table, MathJax typeset) and the human
`ready` / `skip` transitions; Import is the only control that writes live.

Two defects were found by writing the tests, and fixed:
- `/api/staging/vocab` crashed with `TypeError: string indices must be
  integers` - the shipped seed mixes plain strings (subjects) with
  `{"tag": ...}` objects (corps), so the picker could never have been built;
- `imported` was not a one-way door: a record could be dragged back to
  `staged`, which would let a second import overwrite a live row the owner had
  already reviewed. The status route now refuses any transition out of
  `imported`.

Evidence: `.venv/Scripts/python tests/test_staging_api.py` -> `Ran 7 tests ... OK`
(route-level, through the real app: counts, vocabulary shape, the 404s, the
served dashboard, an edit+ready round trip persisted across requests, an
unknown status refused, and the `imported` door closed).

## P6 - crop-first importer (complete, offline)

`pipeline/importer.py` is the only module that writes live rows and bucket
objects, and it is reachable only from a human's Import press. Order per
record is load-bearing: **upload the crop, then write the row, then flip
staging to `imported`**. A row is therefore never created pointing at a
picture that failed to land, and a crashed import leaves `ready` rows to
retry rather than half-written live rows. `ready` becomes `approved`;
deterministic crop keys make a second run reuse the same object instead of
uploading a variant. `storage.upload_bytes` is the single bucket write.

Evidence: `.venv/Scripts/python tests/test_importer.py` -> `Ran 11 tests ... OK`
- only `ready` rows are imported; the crop upload is recorded BEFORE the row
  write; a failed upload leaves the row `ready` and the batch `failed`; an
  imported row is frozen; a second run imports nothing and uploads once;
  `to_live` keeps the contract id in `legacy_id`, derives the question type,
  converts the grade to a smallint, and points at no picture when no crop
  exists.

No live import has been run. Nothing in P4/P6 wrote Postgres or the bucket:
the only live reads are the existing `/api/stats` and the answer-bank lookup.

### P4/P6 follow-up - defects found by driving the real UI

A browser pass against a live server with a real staged source exposed four
things the route tests alone could not:

- `/sources/{id}` returned RAW columns (`bbox_json`, `options_json`), so the box
  editor had nothing to draw and no box ever appeared. Each record is now shaped
  by the same `_view` the record routes use.
- the bbox was positioned against the centring wrapper, so a centred page drew
  the box at the container's left edge. The image is now the offset parent
  (`.ins-page-stage`).
- opening a row whose `page` equalled the displayed page drew no box (the
  "page changed" branch was the only draw path). The row's page now always
  drives the show, plus an explicit draw.
- `list_sources` returned no tallies, so every source read "Questions 0 /
  Answers 0" while rows existed. The counts are now part of the query, pinned by
  `tests/test_staging.py::SourceTallyTests`.

Also confirmed live: an edit (text, caption, pipe table - Persian intact) then
`Ready` persists and moves the counters (`st_questions: ready 1`).

Evidence: `.venv/Scripts/python tests/test_staging_api.py` -> `Ran 9 tests ... OK`;
`.venv/Scripts/python tests/test_staging.py` -> `Ran 33 tests ... OK`;
`.venv/Scripts/python -m unittest discover -s tests` -> `Ran 676 tests ... OK`.
