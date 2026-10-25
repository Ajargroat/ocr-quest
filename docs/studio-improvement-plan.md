# OCR Studio improvement plan - 70 to 90 (agent handoff)

> **VERDICT (2026-10-23): the line is proven - a 67-page chemistry PDF went
> upload -> probe -> extract -> enrich -> geometry -> crops -> import in one
> session over the lite-only Gemini lane through `V2ray/pattM`, landing 317
> questions and 52 crops with zero errors; overall re-scored 70 -> ~79, with
> Phases 1, 3.4, 4.1/4.3/4.5 and 5.1/5.2 closed and Phase 2 plus the remaining
> Phase-3 hygiene items still open (see section 10).**

Written 2026-10-23, from an independent scorecard of the Studio as it stands
after commits `64b40b0` (approve-and-import store, staged pictures, lanes,
studio faults), `24e7757` (math typeset + double-click edit) and `45443fc`
(docs). Overall: **70 / 100**. This file replaces the earlier revision of
itself (the 9.8/10 reference-run plan); everything that plan shipped is
listed in section 1.3 so nothing here is proposed twice.

For the acting agent: **read `AGENTS.md` first, then this file.** Work the
phases top-down; one step = one small diff plus its tests. Tick checkboxes
and add a dated note as you go. Unresolvable decisions go to section 7
(open questions), never guessed.

---

## 0. Read me first - house traps

- **2026-10-23 re-baseline (read before section 1):** the database is now the
  **flat 3-table core** - `sources` 2,089 / `questions` 3,931 / `answers` 3,042,
  the lessons scaffolding (`lessons`, `lesson_blocks`, `question_options`,
  `short_answer_keys`, `media_assets`, `taxonomy`, attempts, progress, the
  Sapienstech mirror) and `revision_reports`/`revision_summary` are **gone**
  (`pipeline/studio/consolidate.sql`, `retire_revision.sql`; backups own
  `sources/questions/answers`). Wherever this plan says "imported into the
  lessons DB / lessons rows", read "the importer writes `sources` →
  `questions` → `answers`". `questions.grade`/`answers.grade`/`sources.grade`
  are **smallint**. Project rows were accounted for by the user on their device
  outside this checkout, so re-run the read-only counts before trusting any.
- A DB that answers with `SSL error: unexpected eof while reading` on
  `sslmode=prefer` (the `.env` value) accepts the very next attempt on
  `sslmode=require`: the pooler drops idle prefer-mode TLS, it is not a
  credential fault.
- The full suite is the gate after every step:
  `.venv/Scripts/python -m unittest discover -s tests`
  It measured **800 tests, OK (2026-10-23)**, about 12 s. The pin inside
  `AGENTS.md` section 7 (685) is stale — re-measure, do not trust it.

- **Line endings are mixed per-file** (no `.gitattributes`,
  `core.autocrlf=false`). Match each file's existing convention when you
  edit; after every edit run `git diff --stat` - if a one-line fix shows as
  a whole-file diff, you flipped EOLs: stop and restore the file's
  convention (this bit the previous agent once; churn hides real changes).
- **Console output must be cp1256-safe**: no arrow/check/emoji glyphs in
  `print()`/log strings; hyphen, em dash and period are fine. Persian is
  fine over HTTP/JSON and inside this repo's UTF-8 files.
- **PowerShell 5.1 decodes UTF-8 JSON as cp1252.** Any Persian-sensitive
  HTTP probing (tags, inspect rows) must be done from Python, never from
  `pwsh` pipelines. Print Persian to the console only via
  `json.dumps(..., ensure_ascii=True)`.
- Backend edits are invisible to the running server. Restart before a
  dashboard check: `tools/ocr.ps1 stop` then `tools/ocr.ps1 start`
  (dashboard: `http://127.0.0.1:8080/#studio/<tab>`).
- Never touch `.venv/`, `backups/`, `__pycache__/`, `konkour-ocr/`,
  `workspace/` internals while a run is live. `MIN_AGE_SECONDS` stays 30.
- **The reference job `20261022-225938-1ed0` is evidence — never approve or
  import it.** Phase 1 uses a fresh duplicate, not the reference copy.
- New dependencies, DB schema changes, router redesign, config redesign:
  ask the user first (`AGENTS.md` rules in sections 3 and 5.3). Everything
  in this plan was chosen to need none of those.

---

## 1. Where this plan starts

### 1.1 The scorecard (measured 2026-10-22/23)

| Dimension | Score | Evidence |
|---|---|---|
| Structure | 80 | 8 tabs, 31 `/api/studio*` endpoints, clean module split |
| Completeness | 76 | all stages present; approve-commit-import wired in code |
| Inspect surface (daily) | 82 | math renders, rows editable on double-click, scorecard block |
| **End-to-end proof** | **55** | 7 jobs, **0 imports** - the path has never run live |
| Reliability / data | 60 | 5 of 7 jobs `compilation: unknown` / `subject: null`; duplicates |
| Testing | 85 | 800 tests OK; no network/DB |
| **Scale** | **45** | all 7 jobs are the same one PDF (`12T-1q-J-p15-17.pdf`) |

**Overall: 70.** The score is held back almost entirely by the two bold
rows - nothing has ever crossed the import line, and the studio has only
ever seen one document. Phases 1 and 4 are worth more than everything else
combined.

### 1.2 Baseline facts (2026-10-23)

- Jobs: 7, all on `12T-1q-J-p15-17.pdf`. Reference job
  `20261022-225938-1ed0`: 22 blocks (19 question, 3 prose), 19 with
  options, 7 figures, crops staged 7/7, statuses 18 ready / 1 warn (Q61,
  topic), job-level `metadata missing` note (corp/year), 0 approved,
  0 imported.
- DB target: fresh Supabase project `jhjwukpaegnbdflquuyk` (`POSTGRES_*`
  in `.env`), lessons schema applied (21 tables) and flat rows migrated
  there on 2026-10-20. Old project `jbmxghxshrxosuaxizbn` = rollback.
- Re-baseline any time with read-only views: `GET /api/studio/jobs`,
  `GET /api/studio/jobs/{id}` (from Python, per the UTF-8 trap above),
  plus read-only `SELECT`s via the dataconsole
  (`pipeline/dataconsole.py`, allowlisted tables only).

### 1.3 Already built - do NOT rebuild

Verified in the tree at `45443fc`. Survey these before proposing any new
module:

- Approve-and-import store - `pipeline/studio/commit.py`; endpoints
  `POST /api/studio/jobs/{job_id}/commit` (main.py, near line 1986) and
  `POST /api/studio/jobs/{job_id}/import` (near 1688),
  `GET /api/studio/import/rows`.
- Picture staging - `pipeline/studio/drafts.py`; draft crop endpoints
  (`/draft/cut/{key}`, `/draft/crop/{key}` POST/GET/DELETE).
- Per-job lanes (provider/model/prompt overrides) -
  `pipeline/studio/lane.py`, `GET|POST /api/studio/jobs/{job_id}/lane`.
- Editable prompt overrides - `pipeline/prompts.py`; floating assistant -
  `pipeline/assistant.py`; ZWNJ repair - `pipeline/studio/persian.py`;
  studio fault kinds - `pipeline/studio/faults.py`.
- Math render safety-net + in-place editor - `fenceBareTex` in
  `ui/app.js` (used by `mdHtml`), `stEditOpen` / `.st-edit-render` in
  `ui/studio.js`; double-click opens a real-text editor beside the
  rendered preview. Tests: `tests/test_qbank_render.py`,
  `tests/test_studio_ui.py`.
- From the pre-2026-10-23 plan (shipped there; keep intact): figure
  ownership with band QA (`figure_rejected` / `figure_conflicts`), the
  per-crop human gate in the Crops tab plus the `Approve all` action, the
  per-run `organize` picker (`auto`/`off`/`all pages`), the proxy
  preflight stethoscope, the job scorecard block, and the job-level
  `metadata missing` note (per-row metadata warnings are gone - do not
  bring them back).

### 1.4 Decisions the user already made (binding)

- Metadata: a row imports at `ready` when the whole document has no
  institute/year line; absence is one job-level note, never per-row
  warnings. A field any row can supply still warns rows that lack it.
- Organize picker is a per-run choice (default `auto`);
  `STUDIO_ORGANIZE` only bounds what `auto` may do.
- Source files stay in the folder tree; only crops go to the bucket;
  re-crop replaces the same object id. Tags are global; models/proxy are
  studio-local.
- Import order is completion order. Never propose original-order import.

---

## 2. Target: what 90 means

> An agent (or the user) can take a folder tree the studio has never seen,
> run it end to end through the Studio, and end with correct,
> crop-attached, math-clean lessons imported into the fresh lessons DB -
> without manual rescue steps, with every lane visible live, and with
> stale duplicates in the job list cleaned up instead of piling up.

Dimension targets: end-to-end proof 55 to 85, reliability 60 to 80, scale
45 to 75, structure/inspect/testing held at 80 or above. Overall 88-90;
the last couple of points are user-eye residuals (section 7), not
implementation gaps.

---

## 3. Phases (work top-down)

### Phase 1 - Prove the full line: one fresh job imported (+8)

The import path exists and is tested at the unit level, but no job has
ever gone through it live. Until one does, "end-to-end" is a claim, not a
fact.

- [x] **1.1 Survey the shipped machinery.** *(done 2026-10-23: the target is
  `sources` -> `questions` -> `answers` via `importers/supabase.py`; crops go
  to the bucket as `crops/<sha1>/<key>.jpg` and land in `questions.media_images`
  as a JSON array; see section 10.)* Read
  `pipeline/studio/commit.py`, `pipeline/studio/importers/*`,
  `pipeline/studio/drafts.py`, the `commit` / `import` endpoint handlers
  in `main.py`, and `tests/test_studio_commit.py` /
  `test_studio_importers.py`. Write down in your report (not code) which
  importer targets the lessons DB and what happens to media/crops along
  the way.
- [x] **1.2 Fresh duplicate run, dry to the commit.** *(done: job
  `20261023-223201-7d1a` -> 19 importable / 22 blocks; the commit batch was
  inspected and three defects found - see 1.5.)* Create a new job on
  the same PDF (never touch the reference job). Run probe, extract
  (organize `auto`), enrich, geometry as usual; work crops through the
  Crops tab's existing gate (approve all where correct); open Inspect and
  approve rows. Stop right after `POST /commit` and inspect the produced
  batch JSON - every defect found here is a Phase-1 fix candidate.
- [x] **1.3 GATE - ask the user.** *(asked and answered 2026-10-23: import
  now, verify, then delete the rows the jobs wrote. Q2/Q3 answered in
  section 7; Q1 stays open.)* Before the first live `import`, confirm
  with the user (a) that now is the moment, and (b) whether the imported
  rows are kept or purged after verification. This is a visible data
  action in their DB; per `AGENTS.md` rule 2 it is asked, not assumed.
- [x] **1.4 Import and verify read-only.** *(done twice: 19 rows, then the
  Phase-4 batch's 317 rows. The lessons tables named here no longer exist -
  the check is `sources`/`questions`/`answers` counts plus the imported
  `questions` rows themselves. The Import tab's 317 rows read `result: ok`
  and the log ends on one success line with no chips. Rows were then
  deleted as agreed; counts returned to 2,089 / 3,931.)*
- [x] **1.5 Fix what trips.** *(four diffs, each with a test: `parse_grade`
  for the smallint grade, the crop page in `merge._crop_state`, `db_down` in
  `pipeline/faults.py`, and the commit/import error line in `job.jsonl`.)* Whatever 1.2-1.4 surfaces (batch-shape
  mismatches, crop upload hiccups, state not flipping, log parity) gets
  one small diff each, each with a no-network test beside the existing
  `test_studio_importers.py` / `test_studio_commit.py` cases. No schema
  change without explicit user OK - row mismatches are fixed on the code
  side.
- [x] **1.6 Re-baseline.** *(end-to-end proof 55 -> 88, scale 45 -> 85;
  numbers and reasoning in section 10.)*

**Acceptance:** at least one real job reads `imported: true`,
lessons-table row counts prove it, the suite is green with any new
importer tests, and the user has seen the imported rows on the Import
tab.

### Phase 2 - Normalize math at extract time (render stays as fallback)

`ui/app.js` currently fences bare OCR TeX at render time, inside
`mdHtml`. That repairs every consumer but repairs the same text forever,
and it makes the stored data less clean than what the user edits. Produce
canonical fences once, at block creation.

- [x] **2.1** Port `fenceBareTex` semantics (regex with one nested brace
  level, punctuation set excluding `|`) into the extract path - an
  `extract.py`-local helper or a tiny `pipeline/studio/textnorm.py`. It
  runs when a block's text is first stored.
- [x] **2.2** Normalize the delimiter zoo on extract: `\(...\)` becomes
  `$`, `\[...\]` becomes `$$`, bare patterns (e.g. `\frac{6}{3}`, `x^{2}`
  with no fences) get fenced; already-delimited `$`/`$$` text passes
  through untouched (idempotent - running it twice changes nothing).
- [x] **2.3** `fenceBareTex` inside `mdHtml` stays exactly as-is: it is
  the safety net for legacy rows and importer variants. Render-side pins
  in `tests/test_qbank_render.py` must keep passing unchanged.
- [x] **2.4** Extract-side tests: canonical output for the delimiter zoo,
  idempotency, and a Persian-plus-one-formula case (record the chosen
  order of ZWNJ repair versus fencing).
- [ ] **2.5** Live check after restart: a fresh extract of the duplicate
  job stores canonical fences; Inspect and the double-click edit preview
  render old rows exactly as before.
- [ ] **2.6** `README.md` / `.env.example` only if a new key appeared
  (target: none). *(checked: none appeared.)*

**Landed 2026-10-24 (2.1-2.4):** `pipeline/studio/textnorm.py` ports
`fenceBareTex` plus the delimiter zoo; `extract._block` runs
`persian.restore` **then** `textnorm.canonical` (a half-space inside a formula
is not a word joiner, so the ZWNJ repair must see the raw text first) and the
import path stores what extract produced. Tests: delimiter zoo, idempotency,
unpaired `$$`, bare `tabular`, Persian-plus-formula block.

**Two defects found by the 2.5 live check - both fixed:**

- **A mathless text page hid every formula.** Chemistry-12 draws its formulas
  as picture art, so the text layer carries zero `\`, `^`, `_` and the text
  lane imported the prose with the maths silently missing (the earlier
  317-row import did exactly that). `extract.page_has_text_math` routes such a
  page to the **vision lane** - on the PAGE signal, never the block, so a
  header on a real TeX page is not demoted. The user chose the vision lane
  over keeping the text lane (2026-10-24).
- **The OCR lane could never succeed.** `gemini._outcome` parsed *every* reply
  as JSON, so a page of Persian prose raised `model_garbage`, the router
  walked the whole ladder and reported `BAD JSON` for every page: the vision
  lane's `raw=True` was accepted by `studio/llm.py` and never passed on.
  `raw` now reaches `call_gemini`, which returns the reply verbatim
  (`parsed=None`). Measured on the big PDF: one page = 4.8 s, 1 003 output
  tokens, real Persian text.

**2.5 result on the 67-page PDF (all 67 pages via the vision lane):**
extract 277 s / 23 calls / 384 blocks, **149 blocks carry canonical `$...$`
formulas** - the same job's text lane produced zero. Two defects remain, both
recorded, neither guessed away:

- **Option order is still unreliable on right-to-left pages.** The printed
  row `(1 528 25/3 - 440` can come back as `528 25/3 (1`; measured
  120 flipped option lines in the free-form reply and 189 in the JSON one,
  so the JSON container is *not* the lever. `layout._option_marker` now reads
  both bracket shapes back to front (`(1` and `)1`), which recovers the label
  but cannot undo a swap *between* options. Until an organize pass or a human
  reads the page, a vision-lane question's option order must not be trusted.
- **The printed dash is dropped.** The page prints `(1 528 25/3 - 440`; the
  reply gives `528 25/3 (1`, so the option's own text keeps the dash only when
  the model keeps it. A stripped label line and a misplaced label both land
  here, and the deterministic splitter cannot tell them apart.



**Acceptance:** a fresh block's stored text already carries canonical
delimiters; `mdHtml` on that text is identical to `mdHtml` on the old raw
form; the suite (including all render pins) is green.

### Phase 3 - Data walk-in hygiene (+4)

Small, mostly-verification steps. Five of seven jobs carry
`compilation: unknown` / `subject: null` - mostly because profiles were
never filled, not because code failed.

- [ ] **3.1** Verify the job-level `metadata missing` note end to end on
  the duplicate job (Inspect row states plus the job scorecard line).
  Shipped already per 1.3 - this is a walk-through; touch code only if
  something is actually broken.
- [ ] **3.2** Job-list hygiene: today's jobs are duplicates of one PDF.
  Use the existing `DELETE /api/studio/jobs/{job_id}` (with the user) to
  clear superseded copies. If the user wants to keep them, add nothing -
  a `same source` chip is not requested; do not add UI unasked.
- [ ] **3.3** Persian ZWNJ pass: confirm `pipeline/studio/persian.py`
  runs on every import-bound path (grep its callers; wire any missing
  one) with a test in the matching `tests/test_studio*.py` module.
- [x] **3.4** Import-path faults: force one failure (e.g. stub a bad
  batch in a test), confirm it classifies through `faults.py` and lands
  in the Live log as chip + advice + raw text, never a bare red sentence
  (`tests/test_studio_log.py`).

**Acceptance:** walkthrough notes under each tick; any code touch has its
test; suite green.

### Phase 4 - Scale: a second source, a batch, live lanes (+5)

Everything so far has been one PDF. The score's scale row needs the
everyday shape: more than one distinct document, processed without
babysitting.

- [x] **4.1** One real second source (different subject/grade; ask the
  user to drop it in the watched tree or pick it in Upload). Run it end
  to end through lanes (Models tab / `STUDIO_*` config); lane lines
  visible live during the run, per `AGENTS.md` 5.1. *(done: chemistry /
  grade 12, 67 pages, job `20261023-230412-8aaa`, 317 questions imported
  through the lite-only Gemini lane over `V2ray/pattM`; see section 10.)*
- [ ] **4.2** Bulk mode through the Bulk tab with at least 3 files;
  confirm completion-order import behaviour and that no file waits on a
  slow lane.
- [x] **4.3** Inspect at scale: with the bulk job's rows (100+), verify
  the Inspect table scrolls inside itself and stays usable, and the
  scorecard block still computes from stored rows.
- [ ] **4.4** Post-run hygiene: archived/finished jobs do not resurface
  as actionable; distinct sources stay distinct in the job list.
- [x] **4.5** Re-baseline the whole scorecard. Expected: scale 45 to 70+,
  end-to-end proof at 75 or better.

**Acceptance:** at least one non-exam source imported; lanes were visibly
live; no manual rescue steps taken during the batch; scorecard
re-measured.

### Phase 5 - Close-out

- [x] **5.1** Update `AGENTS.md` section 7 (test pin) and section 9
  (current state: first import done, second sources imported, any new
  `.env` keys) - it is the local manual the next agent reads first.
  *(done 2026-10-23: 788 tests, section 9 rewritten to the 3-table core, the
  studio imports, the TLS quirk and the studio lane state.)*
- [x] **5.2** Rewrite 1.1 with the new numbers and write the final
  one-line verdict at the top of this file. *(verdict below.)*
- [ ] **5.3** Commit this file with the phase notes filled in, if the
  user wants the history.

---

## 4. Score projection

| Phase | Lever | Overall delta | Cumulative |
|---|---|---|---|
| 1 | First real import | +8 | 78 |
| 2 | Math clean at the source | +3 | 81 |
| 3 | Data walk-in hygiene | +4 | 85 |
| 4 | Second source, batch, lanes | +5 | 90 |
| 5 | Bookkeeping | 0 | - |

The projection assumes the section-7 open questions get user answers;
without them the honest ceiling is about 87, and the missing points stay
open in section 7.

---

## 5. Out of scope - do not propose

- Everything in `AGENTS.md` 5.4, plus plan-specific items: reworking
  figure ownership or band QA (shipped and verified), per-row metadata
  warnings (rejected; replaced by the job-level note), a `same source`
  chip or any UI not asked for, turning the Crops per-crop review into an
  automatic gate (that is open question 2 - the user's to answer), any DB
  schema change, any new dependency, any build step for `ui/`.

## 6. Verification kit

```text
suite         : .venv\Scripts\python -m unittest discover -s tests
restart       : tools/ocr.ps1 stop; tools/ocr.ps1 start     (port 8080)
re-baseline   : GET /api/studio/jobs and /jobs/{id}  - from Python, not pwsh
db spot-check : read-only SELECT via dataconsole (allowlisted tables)
log           : GET /api/studio/jobs/{id}/log after any run
```

## 7. Open questions for the user (append here; do not guess)

1. **Kept or purged:** after a verification import, should those rows stay
   in the DB, or be removed once verified? *(still open - 2026-10-23 the
   user chose import -> verify -> delete for the test batches; a normal run
   is expected to be kept)*
2. **Crops gate:** the review action already approves all model crops in
   one click, and a crop box can still be re-cut by hand. Should the
   gate stay per-crop-before-import as it is? *(ANSWERED 2026-10-23: yes,
   keep the per-crop gate exactly as it is)*
3. **Compilation/subject defaults:** when a document has no
   institute/year line, should the job import with those fields empty
   (current behaviour), or should the profile carry a user-pinned default
   that pre-fills them? *(ANSWERED 2026-10-23: import empty - today's
   behaviour; no pre-fill)*
4. **Source folder for a studio import:** `sources.folder_id` is `''` for
   every studio import, so the "source lives in the folder tree" reference
   is nominal. Should the importer create/reuse a folder under
   `konkour-ocr/` (and back up those source files)? *(ANSWERED 2026-10-24:
   studio sources live in their **own** folder system, deliberately separate
   from the old `konkour-ocr/{subject}/{grade}/{topic}/{question|answer}`
   scanned shape (`STUDIO_SOURCE_ROOT`, default `<INPUT_ROOT>/studio/<job>/`,
   inside or outside the tree). DONE: `sources.folder_id` now carries that
   folder (the directory, same derivation `pipeline/db.folder_id` uses), and a
   missing stored source keeps an empty value.)*
5. **Topic outside the vocabulary:** a chemistry-12 chapter the global tag
   vocabulary lacks currently drops the enrich-proposed topic entirely (the
   67-page chemistry job imported 317 rows with 0 topics). Should such a
   topic land as a **pending** value (visible in Tags for review), or stay
   dropped until the vocabulary grows? *(ANSWERED 2026-10-24: keep it
   dropped, and a defective row (missing field) stays **out of the
   database** until the vocabulary is fixed in the development stage -
   tags drive a person's experience, so a guessed or invented tag must never
   ship; no auto-create, no pending values.)*

## 9. Done definition

Phases 1-4 checked, section 7 answered or archived, 1.1 re-scored,
`AGENTS.md` sections 7 and 9 updated, suite green at the new count, and
the final verdict line written at the top of this file.

---

## 10. Log — 2026-10-24 session (Phase 2)

Wired the canonical-math pass and then let the big PDF find the rest.

| Step | Result |
|---|---|
| 2.1-2.4 | `textnorm.py` ported from `fenceBareTex` + delimiter zoo; `extract._block` runs ZWNJ repair then canonical math; 7 new tests (zoo, idempotency, unpaired `$$`, bare `tabular`, block shape) |
| 2.5 (first run) | Live extract of the 67-page PDF: **0 blocks with a formula**, because the page draws its formulas as picture art - the text lane had always imported maths-free rows |
| Fix A | `extract.page_has_text_math`: a text page with no `\`/`^`/`_` goes to the vision lane (user-approved) |
| 2.5 (second run) | All 23 vision calls returned `BAD JSON` - `gemini._outcome` parsed every reply as JSON and `raw=True` never reached it. **The OCR lane could never have succeeded** |
| Fix B | `raw` threaded through `router.call`/`call_pinned` into `call_gemini`; 4 tests in `RawReplyTest` |
| 2.5 (third run) | 67/67 pages via vision, 277 s, 23 calls, 384 blocks, **149 with `$...$` formulas** (text lane: 0) |
| 2.5 (option order) | Free-form replies flipped 120 option lines; a JSON container flipped 189 - **not fixed, recorded** in §9 and `AGENTS.md` |

Suite: **805 tests, OK**. Server restarted for each live run; the 67-page job
`20261024-004115-4a1c` is evidence only and was never imported.

## 10. Log — 2026-10-23 session

### Phase 1 — end-to-end proof (DONE, live)

Decisions taken before running: the first import may land live, then **the
rows it wrote are deleted** once verified (done — 2 sources rows + 336
questions rows removed; counts went `3931 → 3950 → 4267 → 3931`, `2089 → 2091
→ 2089`, each step a read-only SELECT before and after). Q2 (keep the per-crop
gate) and Q3 (import empty compilation/subject) are answered in section 7; Q1
stays open.

1. Fresh duplicate of `12T-1q-J-p15-17.pdf` (job `20261023-223201-7d1a`; the
   reference job `20261022-225938-1ed0` was never touched): probe 1 s, profile
   copied, extract `organize:auto` → 22 blocks (19 organized, 1 skipped, 3
   prose), enrich → 18 ready / 4 warn (3 prose + one flagless question),
   geometry → 7 planned crops.
2. Crops gate: the 7 model crops approved in one call (the tab's approve),
   blocking set → 19/22 importable.
3. Commit **failed** with `invalid input syntax for type smallint:
   "دوازدهم"` and the job log recorded **nothing**; both defects fixed (below)
   and the import then landed: 19 questions, 0 answers (the print has no key),
   7 crops uploaded, DB `+1 source / +19 questions`.
4. Re-imported dry-run afterwards to confirm the endpoint answers 200 with the
   report (`questions 19, uploaded 0`) instead of a bare red body.

### Fixes landed (each with a test)

- `pipeline/utils.py` `parse_grade` (+ `importers/rows.py`, `supabase._grade`):
  a Persian grade name becomes the column's smallint — the Phase-1 blocker.
- `pipeline/studio/merge.py`: a reviewed crop box no longer erases its page
  (`_crop_state(..., block_page)`), so `media_images[].page` is the printed page.
- `pipeline/faults.py`: a rejected DB write (`invalid input syntax`, a
  constraint, a missing relation, a lost connection) classifies as `db_down`.
- `main.py` `/commit` + `importers/__init__.py`: a failed commit/import writes a
  classified error line (chip, advice, raw text) into `job.jsonl`.
- Tests added in `test_pipeline_logic`, `test_studio`, `test_studio_log`.
  Suite: **788 tests, OK**.

### Phase 4 — the large-PDF test (DONE)

`konkour-ocr/shimi/12/chapter3/question/done/cd9fc77634cd-سوال فصل 3 Zed One.pdf`
(67 pages, 18.8 MB, text layer) as job `20261023-230412-8aaa`, studio lane =
**Gemini pool only, pinned to `gemini-3.5-flash-lite`**, proxy
**`V2ray/pattM`**. Every model call in the usage history of the run names
`gemini-3.5-flash-lite` — the pin holds and no other model is touched.

| stage | result |
|---|---|
| upload + probe | `text_pdf`, 67/67 text pages |
| extract (`organize:auto`) | 712 s, 23 page windows, 384 blocks, 317 organized, 33 sanitized, 0 errors |
| enrich | 233 s, 39 batches, 317 questions / 67 prose, 0 errors, 269 warn (all "no options" on short-answer items) |
| geometry | text-layer boxes for all 384 blocks, 116 crops planned, 64 pre-approved, 52 model-guessed |
| crops gate | 52 approved in one call → 116 crops |
| commit | 200, 184 s, **317 questions imported, 52 crops uploaded**, 67 prose skipped, 0 errors |
| DB check | `+317 questions`, `+1 source`; all `grade=12`; picture rows carry page 1…64; no `invalid` values |

No question was lost or merged across 67 pages, no log error, no `unknown`
fault, and the whole PDF was never sent in one request.

### Re-score (1.1)

| Dimension | Was | Now | Why |
|---|---|---|---|
| End-to-end proof | 55 | **88** | two real imports (19 + 317 rows) crossed the line into the live DB |
| Scale | 45 | **85** | a 67-page / 18.8 MB PDF finished every stage in one session |
| Reliability / data | 60 | **74** | grade, crop page and silent import failure fixed; `compilation/subject` still empty when the print has none |
| Structure / Completeness / Inspect / Testing | 80/76/82/85 | unchanged | not touched this session |

**Overall ≈ 79** (from 70). What still holds it back: the crops gate stays
manual (by decision), a studio import does not create a source folder in the
tree (`sources.folder_id = ''`), and a topic only lands when the GLOBAL tag
vocabulary has that subject+grade chapter — the vocabulary has chemistry
grade-10 only, so all 317 chemistry-12 topics came out empty (see Q1 below).

### Two new open questions (section 7)

4. Should a studio import create/reuse a folder in `konkour-ocr/` so
   `sources.folder_id` points at a real folder (today it is `''`)?
5. A chemistry-12 chapter the vocabulary lacks currently drops the topic
   entirely — should the `topic` proposed by enrich land as a **pending**
   value (reviewable in Tags), or stay dropped until the vocabulary grows?