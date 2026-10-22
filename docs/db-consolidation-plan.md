# DB consolidation plan (v3 shape) — for the executing agent

> Status: **decisions confirmed, nothing applied yet.** This document is the
> single brief to execute. It was authored 2026-10-22 after a full code survey
> and a live read-only probe of the database. Do not re-derive the decisions;
> they are locked (§2). Evidence is in §1; the rollout is §5.
>
> Operating rules of the repo (AGENTS.md) stay in force: no schema change
> without user OK (**given — see §2**), whole test suite green, console output
> cp1256-safe (no `→`, `≈`, `✓`, emoji in prints), secrets only in `.env`,
> never touch `.venv/ backups/ __pycache__/ konkour-ocr/ workspace/` while a
> run is live, backend edits need a server restart to be visible.

---

## 1. Evidence base (measured, not assumed)

Live shape probe (read-only SELECTs on the configured DB, 2026-10-22):

| table | rows | total_kb | notes |
|---|---|---|---|
| sources | 2,089 | 848 | file register; folder-tree counterpart |
| questions | 3,931 | 9,480 | 613 dead tuples; 479 rows have crops (media_images non-empty) |
| answers | 3,042 | 7,000 | ~11 duplicated identity columns from questions |
| lesson_blocks | 3,931 | 712 | 1:1 with questions, every row type='question' |
| question_options | 4,888 | 592 | duplicates `questions.options` (only 8 questions have empty options) |
| short_answer_keys | 858 | 1,144 | 1:1 with short_answer questions |
| lessons | 2,089 | 688 | 1:1 with sources; summary/reading_min placeholders unused |
| revision_reports | 3,823 | 2,016 | |
| revision_summary | 2 | 16 | per-run aggregate |
| chapters / courses | 18 / 6 | 48 / 48 | hierarchy |
| questions_sapienstech / students / tests / test_questions | 30 / 20 / 12 / 20 | — | Sapienstech mirror |
| tenants / domains / users | 2 / 2 / 2 | — | FK-target stubs |
| media_assets / attempts / lesson_progress | **0 / 0 / 0** | — | never written by any code path |

Code inventory — the **entire SQL surface is 5 files**; everything else reaches
the DB through `pipeline/db.py` method seams:

- `pipeline/db.py` — every handwritten statement (~60 `_execute` sites:
  upserts, QBank reads/filters, review + revision v2, media bbox, period
  counts over `sources/questions/answers/revision_*`).
- `pipeline/dataconsole.py` — guarded admin console; its table allowlist is
  **imported from `pipeline/backups.TABLES`** (one edit serves both).
- `pipeline/backups.py` — python dump (no pg_dump): per-table `<table>.json`
  + `<table>.sql` (mogrified INSERT replay) + `manifest.json` per set, plus
  the input-folder snapshot; `TABLES` allowlist (14 names) drives it.
- `pipeline/studio/migrate_lessons.py` — the completed one-off flat→lessons
  migration (writes 12 tables, mirrors `ACCOUNT_TABLES` = tenants, domains,
  users, students, tests, questions_sapienstech, test_questions).
- `pipeline/studio/migrations.sql` — the 21-table DDL (+ tiny
  `pipeline/revision/migrations.sql` for answers' revision columns).

Callers with **zero internal SQL** (`upsert_source/upsert_questions/
upsert_answers` seams): `main.py`, `pipeline/runner.py`,
`pipeline/deferrals.py` (replays ops `"upsert_source"/"upsert_questions"/
"upsert_answers"`), `pipeline/revision/runner.py` + `batch.py`,
`pipeline/studio/importers/supabase.py` (writes sources→questions→answers in
that order; first importer step is `_ensure_source`). `storage.py`,
`recrop.py`, `file_export.py` touch no SQL. Dashboard never reads the lesson
hierarchy (`main.py` has no reference to lessons/chapters/courses/lesson_blocks).

Key redundancy facts proven by the probe:
- lesson_blocks has exactly one block per question, no dangling links; its
  only unique payload is `lesson_id + position`. All 8 `block_type` enum
  values except 'question' are unused.
- Options are stored three times: `questions.options` (complete at runtime,
  this is the only source the app reads), `question_options` rows, and
  `correct_option_label/_text` on the question row.
- lessons duplicates sources 1:1.
- The 7 mirror tables (88 rows total) have no runtime reader anywhere.

## 2. Locked decisions (user-confirmed 2026-10-22)

1. **Reshape approved.** The merged design supersedes the root PDF's
   21-table DDL as the official schema.
2. **Sapienstech mirror: deleted** from this DB (7 tables). If a Laravel
   platform needs them later it is re-added on demand, not mirrored by default.
3. **attempts / lesson_progress: deleted** (empty, no writers; re-create when
   a real consumer exists).
4. **answers: slim side table** — NOT folded into questions. Keeps revision
   model + unpaired-answer matching intact.
5. **Conversion route: in place** on the current Supabase project. No fresh
   project; the undo point is a manual backup + single-transaction apply (§4).
6. **courses + chapters: merged** into one `taxonomy` table.
7. `migrate_lessons.py` and `pipeline/revision/migrations.sql`: **retired /
   deleted** with this change (their targets no longer exist).

## 3. Target schema (21 → 5 tables + 1 view)

All tables keep the `public` schema. `BIGSERIAL`/`TEXT` id conventions and
timestamps as today. Enums `block_type` and `image_layout` retire with
lesson_blocks; `question_type` stays on questions.

**`taxonomy`** (24 rows = 6 courses + 18 chapters)
- `id BIGSERIAL PK`, `parent_id BIGINT REFERENCES taxonomy(id) ON DELETE
  CASCADE` — NULL means course level; level is derived, not stored
- `slug TEXT NOT NULL`, `title TEXT NOT NULL`, `position INT NOT NULL DEFAULT 0`,
  `locale TEXT NOT NULL DEFAULT 'fa'`, `created_at`, `updated_at`
- `UNIQUE (parent_id, slug)` with NULL-parent treated rightly (use a generated
  coalesce key or two partial unique indexes; implementer's call)

**`sources`** (2,089 rows; files and lessons fused)
- Existing: `id TEXT PK (sha1)`, `folder_id TEXT NOT NULL`, `file_name`,
  `mime_type`, `file_size_bytes`, `storage_url`, `subject`, `grade`,
  `topic`, `type`, `created_at`
- Added from lessons: `section_id BIGINT REFERENCES taxonomy(id) ON DELETE
  SET NULL`, `position INT NOT NULL DEFAULT 0`, `title TEXT`, below-fold
  placeholders `summary TEXT`, `reading_min SMALLINT`, `status TEXT NOT NULL
  DEFAULT 'published'` (imported lessons are live), `updated_at`

**`questions`** (3,931 rows; the one content table)
- Existing columns stay (type, prompt, explanation, hint, points, max_length,
  legacy_id UNIQUE, source_id FK→sources, question_number, subject, topic,
  grade, corp, year, difficulty, tags, review_status, revision_status,
  revision_notes, revised_at, needs_review, raw_ocr_text, content_hash,
  split, duplicate, unreadable, created_at + the two revision columns ordered
  before any drop).
- `sapienstech_id` **dropped** (mirror gone).
- Changed: `options TEXT` → `options JSONB NOT NULL DEFAULT '[]'` —
  `[{label, text, correct, order}]`; the normaliser (§6) derives it from the
  old text options + question_options rows; when correct==true the entry's
  label equals `correct_option_label`.
- Added: `lesson_id BIGINT REFERENCES sources(id) ON DELETE SET NULL`
  (from lesson_blocks), `position INT NOT NULL DEFAULT 0` (block order), and
  `answer_key JSONB NULL` = `{"model_answer": ..., "keywords": [...]}` from
  short_answer_keys (NULL unless type='short_answer'; enforce by CHECK).
- `correct_option_label` stays (dropdown-friendly); `correct_option_text`
  **dropped** (derivable from options by label).
- CHECK: `jsonb_typeof(options)='array'` (mirror the existing media_images CHECK).

**`answers`** (3,042 rows; slim side table)
- Keep: `id TEXT PK`, `legacy_id`, `source_id FK`, `question_id FK`,
  `question_number` (pairing), `answer_explanation`, `assets`, `media_images`,
  `review_status`, `revision_status`, `revision_notes`, `revised_at`,
  `needs_review`, `raw_ocr_text`, `content_hash`, `created_at`.
- Dropped: `subject, topic, grade, corp, year, difficulty, tags`.
  Unpaired matching must not regress: `answers_identity_pair_idx` and the
  `fetch_answers_matching_numbers` logic key on (question_number, subject,
  topic, grade) — after the slim-down the host/subject comes from the joined
  `sources` row (`answers.source_id`). **The implementer must verify the
  pairing code (`pipeline/revision/pairing.py`, `batch.py`) and db.py's
  matching query against the new join; that is the one subtle migration step.**

**`revision_reports`** — unchanged. **`revision_summary`** — convert to a
VIEW (`run_id, total_flags, errors, ai_flags, code_fixes?…` derived by
GROUP BY run_id) **only if every counter is derivable from reports**;
otherwise keep the 2-row table. Backup TABLES must exclude the view.

**Dropped outright:** lessons, lesson_blocks, question_options,
short_answer_keys, media_assets, attempts, lesson_progress, tenants, domains,
users, students, tests, questions_sapienstech, test_questions, revision_summary
(and enums block_type, image_layout; column questions.sapienstech_id).

**Indexes (replaces the current ones):**
- `questions (source_id)`, `(review_status)`, `(subject, topic)`,
  `(subject, topic, grade, corp, year, question_number)`, partial
  `(revision_status) WHERE revision_status='pending'` — carry over
- NEW `questions (lesson_id, position)` — the single-query lesson view
- `options` GIN? Only if a scan needs it; start without (normalise later)
- `answers (question_id)`, partial identity-pair index on unpaired rows —
  carry over, restated over the slim columns
- `sources (folder_id)`, `sources (section_id)`, `taxonomy (parent_id)`

## 4. Undo design for the in-place route

1. **Phase 0 manual backup** (`POST /api/db/backup/run`, trigger "manual")
   produces a full set: per-table `.json` + `.sql` replay files + manifest +
   input-folder snapshot. Manifest must list all 21 tables with the counts in
   §1 before anything changes. This set is the rollback point.
2. **The reshape runs as ONE transaction** on the live DB: add columns /
   backfill / build taxonomy / fold JSONs / drop old tables / create indexes,
   then commit only if every in-transaction validation SELECT passes
   (row parity checks: lessons→sources 2,089/2,089; blocks→questions 3,931/3,931;
   options-norm 4,888 entries ≡ folded labels; keys 858; taxonomy 24 rows
   with identical (slug,parent) pairs; revision 3,823 unchanged).
   Any failure mid-transaction rolls the entire operation back on its own.
3. After commit, the Phase-0 backup set remains the restore path
   (`backups/<set>/*.sql` replayed with psql, or per-table restore via the
   DataConsole). Document, do not improvise, the restore order in the run log.

## 5. Rollout (do phases strictly in order; run + paste test output each time)

**Phase 0 — undo point**
- Trigger the manual backup; verify manifest (all 21 tables, row counts above,
  input-folder snapshot present). Stop if anything is missing.

**Phase 1 — write, never run yet**
- New file `pipeline/studio/consolidate.sql` (the §3 DDL as one transactional
  script wrapped per §4:2) plus a one-off runner module in the same style as
  the retired `migrate_lessons.py`: dry-run by default, `--apply` to commit,
  prints cp1256-safe ASCII progress, refuses to run if the Phase-0 manifest is
  absent or incomplete.
- Port `db.py`: rewrite the affected statements behind **unchanged method
  signatures** (the six seam-callers must need zero edits):
  - `upsert_source` gains lesson fields (section_id from the tree path,
    title/status defaults)
  - `upsert_questions` gains the options/answer_key JSONB normaliser and
    lesson_id/position (lesson_id = source_id of the row's source)
  - `upsert_answers` writes the slim column set
  - `fetch_review_rows` / QBank reads lose the duplicated-join work
  - `fetch_period_counts` (sources) and revision reads/writes keep behaviour
- Verify unpaired-answer matching against the slim `answers` (join sources
  for identity); fix the query, not the callers.

**Phase 2 — tests for the new shape, BEFORE applying**
- Update pinned suites: `tests/test_backups.py` (TABLES = the 6 names),
  `tests/test_db_model.py`, `tests/test_lessons_schema.py` → re-point at
  consolidate.sql, `tests/test_lessons_migration.py` → replaced by the
  consolidator tests; retire `migrate_lessons` targets from all suites.
- New norm roundtest: `questions.options` JSONB round-trips every legacy
  variant (label list, label→text map, empty `[]`, the 8 empty rows) and
  correct-flag derivation from `correct_option_label`.
- Full suite: `.venv/Scripts/python -m unittest discover -s tests` green.

**Phase 3 — apply**
- Run the consolidator `--apply`; paste the in-transaction validation output.
- Then delete: `pipeline/studio/migrate_lessons.py`, old
  `migrations.sql` content replaced or retired per repo doc rules,
  `pipeline/revision/migrations.sql` (columns now native), the Sapienstech
  mirroring expectations from docs.
- `backups.py`: `TABLES` = ("taxonomy", "sources", "questions", "answers",
  "revision_reports") (+ `revision_summary` only if kept as a table);
  `TS_COL` loses its `last_at` special-case if the view was taken. The
  DataConsole allowlist follows automatically — verify one console overview.

**Phase 4 — restart + verify (user's eye is the gate)**
- Restart the server (backend edits are invisible to the running process).
- The user checks: PeriodStats unchanged (Total files 2,089 · Question files
  · Answers 3,042 / 3,931 · etc.), QBank renders Markdown/`$$`/crops
  (`/api/qbank/page`, `/api/qbank/recrop` still work — sources.storage_url
  untouched), DataConsole overview lists exactly the new tables, and one
  fresh manual backup produces a small 6-file set.

**Phase 5 — close-out**
- README key-table untouched (no new env keys). `docs/` note only if the user
  wants one. Report final suite count + the actual outputs.

## 6. Option normaliser (the one fiddly routine)

Input variants seen in legacy `questions.options` (verify each against the
live DB during Phase 1): JSON list of labels; JSON object label→text; empty
`[]`; NULL; CRM-style single label. Output: array of
`{"label","text","correct","order"}`. `correct` comes from
`question_options.is_correct` (authoritative), cross-checked against
`correct_option_label`; mismatch rows are logged, not silently resolved.
The 8 empty-option questions keep `[]` (they are figure/MathJax-only items).

## 7. Risks & hard rules for the executor

- **In-place rollback = the Phase-0 backup.** No other undo. Never apply
  without it.
- One transaction only; autocommit OFF for the migration connection (db.py's
  runtime class is autocommit — the runner must open its own connection).
- `db._execute` requires a non-empty params tuple (plain `()` raises) — known
  repo quirk; the runner uses raw psycopg2 anyway.
- `lessons.slug` collides across chapters; the migration maps lessons by id
  from the old `lessons` rows, never by slug.
- Pooler note: `.env` uses port 5432 pooler, `sslmode=prefer` (require fails).
- cp1256-safe prints in the runner (`-`, `—`, `·` only).
- Do not let any new "helpful" UI reshaping sneak in — this change is DB-side
  only; `ui/` needs nothing (verify the three surfaces instead).
- Total end-state: **5 tables + 0-1 view**, ~9k row-objects, ~14 MB smaller
  surface on disk; the app's borders (runner/importers/deferrals/revision)
  compile and behave identically.
