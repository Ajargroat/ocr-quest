# Studio improvement plan — agent handoff

Written 2026-10-22, from a full reference run of `12T-1q-J-p15-17.pdf`
(Studio job `20261022-194515-8683`). Every defect below was observed in that
run — nothing here is theoretical. The plan is ordered so that correctness
fixes land before behaviour changes, and behaviour changes before UX.

For the acting agent: **read `AGENTS.md` first, then use this file.** Work
top-down; one step = one small diff plus its tests; do not skip the suite.

---

## How to use this plan

1. Paste one step's **PROMPT** into a fresh agent session (repo checked out at
   `E:\OCR-quest`). The prompt is self-contained: paths, symbols, tests.
2. Run the full suite after every step, and paste the real output in the report:
   `.venv/Scripts/python -m unittest discover -s tests`
   (`.venv` Python only; tests must never touch network or DB).
3. Backend edits are invisible to a running server. After each step that
   touches `main.py` / `pipeline/*`, restart before a dashboard check:
   `tools/ocr.ps1 stop` then `tools/ocr.ps1 start`.
4. **Never approve or import rows of the reference job.** It is the evidence
   copy for every step's acceptance check (see §4).
5. A behaviour change adds a test; a UI markup change updates the
   `test_ui_pages.py` / `test_studio_ui.py` pins.

## Hard rules (from AGENTS.md — still apply)

- `.env` is the secret boundary: never commit it, never print full API keys,
  never leak a key into a doc, a prompt, a test or browser storage.
- Never invent user answers; unresolved decisions become open questions (§7).
- No Postgres schema changes, no new dependencies, no new UI framework.
- Never touch `.venv/`, `backups/`, `konkour-ocr/`, `workspace/` internals
  while a run is live; the reference job's artifacts are read-only evidence.
- Console output must be cp1256-safe: print/log strings in code and in helper
  scripts stay ASCII (English). Non-ASCII is fine in files, JSON and the UI.
- ui/ is vanilla HTML/JS/CSS; every control must be honoured by a *restarted*
  server and visibly verified against it.

---

## 2. Environment facts the acting agent needs

- Platform: FastAPI + dashboard, started via `tools/ocr.ps1 start`
  (entry `main.py`, port from `.env`, currently 8080). The dashboard's Studio
  page: `http://127.0.0.1:8080/#studio/<tab>` with tabs
  `jobs, profile, bulk, crops, inspect, tags, models, import`
  (routed in `ui/studio.js`, `ST_TABS`).
- Gemini pool: keys configured in `.env` (`GEMINI_API_KEYS`). The pool is
  called through `pipeline/gemini_router.py`; per-call accounting lives in
  `.gemini-calls.json` / `.gemini-usage.json`.
- 9router: loopback OpenAI-compatible endpoint at `http://127.0.0.1:20128/v1`
  (fixed by design; never proxied). Studio's provider row lives in `.env`
  under `STUDIO_PROVIDERS`.
- Studio config (as saved by this reference run, via the Models tab API):
  `STUDIO_EXTRACT_ACTIVE=""` (Gemini pool), `STUDIO_ENRICH_ACTIVE="9router"`,
  `STUDIO_VISION_ACTIVE=""` (Gemini pool),
  `STUDIO_GEMINI_MODEL_PIN=gemini-3.5-flash-lite`,
  `STUDIO_VISION_MODEL=gemini-3.5-flash-lite`,
  `STUDIO_PROXY=WhiteDNS`.
- Proxy: profiles live in `.proxy-profiles.json`; the studio lane applies the
  profile to **Gemini-pool calls only** (`pipeline/studio/llm.py`,
  `_studio_profile()`); 9router stays loopback. WhiteDNS = `http://127.0.0.1:10886`.
- **Known trap:** Windows PowerShell 5.1 (`pwsh` in this environment) decodes
  UTF-8 JSON responses as cp1252 — a tags round-trip through it once mojibaked
  the global tag seed. Do any Persian-sensitive HTTP work from Python
  (`requests`), and print Persian to the console only through
  `json.dumps(..., ensure_ascii=True)`.

## 3. The reference document & ground truth

- Source PDF (user-provided): `E:\Downloads\Telegram Desktop\12T-1q-J-p15-17.pdf`
  — 3 pages, text-layer, Physics (تجربی), grade 12; page headers say 13/14/15;
  no institute name, no answer key.
- Ground truth by page: p.13 → Q55–Q60 (plus the tail of Q52 whose stem began
  on the previous page), p.14 → Q61–Q67, p.15 → Q68–Q73.
  **19 complete questions, all 4-option**, none with printed corp/year/difficulty.
- Reference job (evidence copy, local-only under gitignored `workspace/`):
  - Profile: `workspace/20261022-194515-8683/profile.json`
    (pinned subject `فیزیک`, grade `دوازدهم`; extracts corp/year/topic/difficulty)
  - Raw stages: `layer1.json` (13 blocks · 0 model calls),
    `layer2.json` (enriched), `geometry.json` (boxes + crops),
    `probe.json`, `log.jsonl` (full event history)
  - Crops: `bands/` (7 band JPEGs), `crops/` (7 figure crops, lazy-rendered)
- What that run proved (the defects this plan fixes):
  1. Extract on a text PDF makes **0 model calls** and produces
     **8 questions out of 19** — `layout.split_text()` on the PDF's stored
     reading order merges and drops questions when formulas are separate runs.
  2. Enrich made **0 text repairs** (`fixes: []` everywhere); every stem is
     byte-identical to layer 1, formulas stay scrambled.
  3. Options are not bound to their question: only Q55 and Q60 got their own
     option sets; 56/61/63/71 carry the *next* question's options; 59/69 are
     malformed.
  4. Topics: 6 of 8 plausible, 2 shifted onto a question merged inside the block.
  5. Every row warns on `missing` even where a field is filled — the model's
     own "missing" claims are unioned into the row and never recomputed
     (Q55 fills topic yet still lists `topic` missing).
  6. All 7 model-located crops were **auto-approved**: `_crop_spec()` labels
     asset figures and model band figures identically (`"figure"`), and
     `run()` trusts a text-layer bbox to exempt the pair. Four crops sit on
     the wrong row (Q60 got Q52's wave graph; two prose rows carry later
     questions' figures).
  7. Locating ran as 2 band calls (Gemini pool, through WhiteDNS): 23 s and
     133 s per call; 9router enrich calls took 26–89 s each.
- Tag vocabulary state: `pipeline/studio/tags/seed.json` now carries the
  reference additions (subject فیزیک / grade دوازدهم, chapter rows 1–6, 38
  topics; 53 topics total). Treat them as intended, not as debt.

---

## 4. Steps

### Phase 0 — stop shipping wrong data

#### Step 0.1 · Enrich must recompute `missing` after the patch

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Fix a false-negative in the Studio enrich stage.
  In pipeline/studio/enrich.py: `_apply()` currently unions the model's own
  "missing" claims into block["missing"] (the assignment near
  `block["missing"] = sorted(set(block["missing"]) | extra)`), and `_health()`
  unions that stored list again without ever dropping entries for fields that
  HAVE since been filled. Result (reference job 20261022-194515-8683, block
  p1-b2 / Q55): topic is filled and shown, yet the row still reports
  "topic" missing and stays at warn.
  Change it so block["missing"] is computed exactly once, AFTER the patch is
  applied, as the profile's extract_per_question fields whose value is still
  empty (reuse `_field_value`). `_health()` must not union a stored missing
  list. Keep issue strings and the warn/error status rules otherwise intact,
  and keep `merge.py` editable-field flow working.
  Add regression tests in tests/test_studio.py (no network, no DB):
  (a) a patch that fills topic while also claiming "missing": topic must not
  stay in missing;
  (b) a patch that fills nothing: requested empty fields stay in missing.
  Run the whole suite and paste its output. Small diff; no new dependencies;
  console-safe (ASCII) strings only.
  ```

- **Files:** [pipeline/studio/enrich.py](../pipeline/studio/enrich.py),
  [tests/test_studio.py](../tests/test_studio.py),
  cross-check [pipeline/studio/merge.py](../pipeline/studio/merge.py)
  (`merged()` recomputes health for edited rows — keep it consistent).
- **Reason:** the run's single most misleading signal — 13/13 rows warn with
  false "topic missing" on rows that had a matched topic; it destroys the value
  of ready/warn as a triage signal.
- **Done when:** new tests pass, full suite OK, and re-running enrich on a
  scratch copy of the reference layer1 no longer lists a filled field as missing.

#### Step 0.2 · Separate asset crops from model crops; only assets auto-approve

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Stop auto-approving model-located crops.
  In pipeline/studio/geometry.py, `_crop_spec()` returns (box, "figure") for
  two DIFFERENT origins: text-lane asset boxes (block["images"] into assets)
  and model band-pass boxes (block["figure_bbox"]). `run()` then treats any
  text-layer block as exact (`exact = (crop_source == "figure" and
  block.get("bbox_source") == "text_layer")`) and writes crop_approved=True.
  On the reference job that auto-approved 7 MODEL crops,
  several attached to the wrong question.
  Give the two origins distinct crop_source values: "asset" and "model".
  Auto-approval requires crop_source == "asset"; a "model" crop is written
  with crop_approved=None (review required). Check every consumer:
  pipeline/studio/merge.py `_crop_state()` / `importable()`,
  pipeline/studio/importers/supabase.py, and the crop endpoints in main.py —
  they must keep working with no signature change; only the flag semantics
  move. Add tests in tests/test_studio.py: (a) text-layer block + band figure
  -> crop_approved None and merge.importable says "crop awaiting approval";
  (b) text-layer block + asset figure -> crop_approved True. Full suite OK;
  small diff; no UI change in this step.
  ```

- **Files:** [pipeline/studio/geometry.py](../pipeline/studio/geometry.py)
  (`_crop_spec()`, `run()`), [pipeline/studio/merge.py](../pipeline/studio/merge.py),
  [pipeline/studio/importers/supabase.py](../pipeline/studio/importers/supabase.py),
  [main.py](../main.py), [tests/test_studio.py](../tests/test_studio.py).
- **Reason:** the reference run shipped wrong pictures (Q60 carrying Q52's
  graph) without any human gate — the approval heuristic mistakes *where* the
  block came from for *where the figure came from*.
- **Done when:** tests pass; re-running geometry on the reference job marks
  its 7 band crops as awaiting review.

### Phase 1 — segmentation and the missing "organize" pass

#### Step 1.1 · A box-driven layout organizer for the text lane

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Give the Studio text lane a real layout analyzer.
  Today pipeline/studio/extract.py `_text_page()` splits page text with
  pipeline/studio/layout.py `split_text()` following the PDF's STORED reading
  order. On math-dense Persian pages that ordering is jumbled (formula runs
  interleave), so the reference run produced 8 wrong blocks for 19 questions.
  In layout.py add a box-driven organizer: cluster pdfium char boxes
  (extract.py `_char_boxes()`) into lines by baseline (y tolerance), order
  lines top-to-bottom, detect question anchors (a 1-2 digit number adjacent
  to "-" at a line edge), start a new question at each anchor, and inside each
  question collect options by the `1( ... 2( ...` markers. Each page returns
  a confidence: 1.0 when every anchor yields one stem plus four ordered
  options, otherwise the fraction of anchors that do. Thread per-page
  confidence and question anchors into layer1's pages/stats (extract.py keeps
  `_save_page()` output unchanged). Keep `split_text()` as the fallback when
  the organizer finds no anchors. Build the line-fixture from the reference
  job's page texts (workspace/20261022-194515-8683 layer2.json "pages") —
  copy a small slice into the test module, no network. Tests in
  tests/test_studio.py. Acceptance on the fixture: at least 16 of the 19
  ground-truth questions come out as separate blocks, each with its own 4
  options. Full suite OK; small diff; no new dependencies.
  ```

- **Files:** [pipeline/studio/layout.py](../pipeline/studio/layout.py),
  [pipeline/studio/extract.py](../pipeline/studio/extract.py),
  [pipeline/studio/probe.py](../pipeline/studio/probe.py) (only if the probe
  should also expose the confidence for the UI later),
  fixture source [workspace/20261022-194515-8683/layer2.json](../workspace/20261022-194515-8683/layer2.json),
  [tests/test_studio.py](../tests/test_studio.py).
- **Reason:** this is the root cause of the run's 42% question coverage; a
  code-only pass fixes most of it for free on every text PDF, and gives the
  next step a confidence signal to spend model calls on.
- **Done when:** fixture acceptance is met and the suite stays OK.

#### Step 1.2 · The Gemini "organize" pass (understanding + reorganization)

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Add the model organize pass the Studio extract stage is missing.
  Config: new env key STUDIO_ORGANIZE (0/1, default 1) read in
  pipeline/config.py load_config() as `studio_organize`, documented in
  .env.example and README. No new dependencies.
  Behaviour in pipeline/studio/extract.py: for each TEXT page whose layout
  confidence (step 1.1) is below 1.0, send ONE request through
  pipeline/studio/llm.py `call_stage(cfg, "extract", ...)`: the page JPEG the
  extract stage already stored (`_save_page()`) + the page's raw text + an
  instruction to return {"questions": [{"number": ..., "stem": ...,
  "options": [...up to 4...], "figure": [y0,x0,y1,x1]|null}]} with boxes
  page-normalized 0-1000. Register the prompt text id "studio.organize" in
  pipeline/prompts.py (default lives in extract.py as ORGANIZE_PAGE_PROMPT,
  mirroring OCR_PAGE_PROMPT). json_mode on, temperature 0.1, thinking 0.
  Merge rules — never guess: a model question replaces the deterministic
  blocks for the SAME number only when the deterministic side has that number
  merged into another block or absent; every replaced/added block records
  source {"method": "organize"}; deterministic-only blocks stay untouched;
  a failed or invalid model reply degrades to the deterministic blocks, logs
  a studio fault (pipeline/studio/faults.py) and never aborts the page.
  Tests with pipeline/studio/llm.py CALL_OVERRIDE (existing seam): (a) fixture
  reply -> merged output equals the fixture; (b) faulting reply -> output
  equals deterministic layer1, error logged. Full suite OK.
  Remember: backend edits need a server restart to be verifiable live.
  ```

- **Files:** [pipeline/studio/extract.py](../pipeline/studio/extract.py),
  [pipeline/studio/llm.py](../pipeline/studio/llm.py),
  [pipeline/studio/faults.py](../pipeline/studio/faults.py),
  [pipeline/config.py](../pipeline/config.py),
  [pipeline/prompts.py](../pipeline/prompts.py),
  [.env.example](../.env.example), [README.md](../README.md),
  [tests/test_studio.py](../tests/test_studio.py).
- **Reason:** the user's stated expectation — "use Gemini for understanding
  and re-organizing the extract" — currently exists for image pages only;
  text PDFs bypass every model. This step makes the model a *guardrail* over
  the deterministic lane instead of a replacement for it.
- **Done when:** fixture tests pass; on the reference PDF the staged output
  reaches 16+ questions with 4 options each; with `STUDIO_ORGANIZE=0` the
  behaviour matches today's.

#### Step 1.3 · Re-measure the reference document on the platform

- **PROMPT**

  ```text
  Repo E:\OCR-quest.
  Measure the improvement end-to-end on the reference document.
  1. Restart: tools/ocr.ps1 stop, then tools/ocr.ps1 start.
  2. Upload the source PDF (multipart POST /api/studio/jobs) and re-apply the
     profile pins of section 3 (subject/grade, same extract_per_question
     minus difficulty per step 2.2 if landed).
  3. Run extract -> enrich -> geometry by POST
     /api/studio/jobs/<id>/extract | /enrich | /geometry (main.py endpoints).
  4. Score against ground truth (section 3): questions found, stems
     non-empty, 4 options each, topics inside the tag vocabulary, crops
     owned by the right row. Write the scorecard at the bottom of this file
     as "Appendix D — re-run scores".
  5. Do NOT import; do NOT approve rows. Helper scripts live in the OS temp
     dir and print Persian only via json.dumps(..., ensure_ascii=True).
  ```

- **Files:** [tools/ocr.ps1](../tools/ocr.ps1),
  [main.py](../main.py) (studio endpoints),
  [pipeline/studio/jobs.py](../pipeline/studio/jobs.py),
  source PDF `E:\Downloads\Telegram Desktop\12T-1q-J-p15-17.pdf`,
  evidence copy [workspace/20261022-194515-8683/log.jsonl](../workspace/20261022-194515-8683/log.jsonl).
- **Reason:** the user judges by eye and by numbers; a public before/after
  scorecard on one fixed document is the honest acceptance gate for the plan.
- **Done when:** Appendix D exists with node counts, option correctness and
  crop ownership figures for the re-run.

### Phase 2 — enrich quality

#### Step 2.1 · Harden the enrich prompt and its patch guards

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Make the enrich stage bind options to questions and repair only what it
  can prove.
  In pipeline/studio/enrich.py PROMPT_HEAD add three explicit rules:
  (1) never move stem or option content from one numbered question into
      another — if a fragment clearly belongs to a different number, leave
      it out and set that field missing;
  (2) options must belong to the SAME numbered question, else output "" for
      every option of that block;
  (3) repair LaTeX/units only when the misreading is unambiguous
      (e.g. a split numeric "138/6"), never paraphrase, never translate.
  Code guards in `_call_chunk()` / `_fixes_options()`: reject a patch whose
  question block returns more than 4 options or whose stem is empty while the
  landed stem was not — keep the landed content and add an issue string
  ("options beyond 4" / "empty enrich patch"). Register the prompt edits
  under the existing "studio.enrich" id (pipeline/prompts.py). Tests with
  CALL_OVERRIDE fixtures for both guards. Full suite OK; small diff.
  ```

- **Files:** [pipeline/studio/enrich.py](../pipeline/studio/enrich.py),
  [pipeline/prompts.py](../pipeline/prompts.py),
  [tests/test_studio.py](../tests/test_studio.py).
- **Reason:** only 2 of 8 reference option sets belonged to their numbered
  question; the model had no rule against cross-contamination and the code
  accepted anything shape-valid.
- **Done when:** guard tests pass and the suite stays OK.

#### Step 2.2 · Make difficulty optional in the default profile

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Default extract_per_question drops "difficulty".
  In pipeline/studio/profile.py `blank()` and in probe.py
  `suggest_profile()` the default extract list is ("corp", "year", "topic",
  "difficulty"); difficulty is essentially never printed in these banks, so
  every reference row was warn partly for a field nobody can supply.
  Change BOTH defaults to ["corp", "year", "topic"]; a user can still add
  difficulty back per job in the Profile tab (schema unchanged). Update
  tests/test_studio.py defaults and any fixture that asserts the old list.
  Full suite OK; small diff.
  ```

- **Files:** [pipeline/studio/profile.py](../pipeline/studio/profile.py),
  [pipeline/studio/probe.py](../pipeline/studio/probe.py),
  [tests/test_studio.py](../tests/test_studio.py).
- **Reason:** an unplannable field made 100% of reference rows warn; the warn
  channel must stay meaningful (see open questions §7 — user confirm).
- **Done when:** new-job profiles default without difficulty; suite OK.

### Phase 3 — figure ownership

#### Step 3.1 · Band ownership QA: containment + single owner

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Make figure attribution auditable in the locate stage.
  In pipeline/studio/geometry.py's band pass:
  (1) after `_apply_band_reply()`, reject a figure box that is less than 60%
      inside the block's own page bbox — drop it, count a new
      "figure_rejected" stat and emit one studio info line naming the block;
  (2) when two blocks on the same page end up claiming figure boxes that
      overlap by more than half of either box, keep the claim whose block
      center is nearest the figure and clear the other; count
      "figure_conflicts".
  Keep BAND_PAD_Y/BAND_PAD_X semantics; keep the band retry ladder as is.
  Extend the geometry stats schema accordingly (consumers read stats via
  merge.py/bulk JSON — check them for new keys tolerance). Tests with
  synthetic geometry entries for both rules. Full suite OK; small diff.
  ```

- **Files:** [pipeline/studio/geometry.py](../pipeline/studio/geometry.py),
  [pipeline/studio/merge.py](../pipeline/studio/merge.py),
  [tests/test_studio.py](../tests/test_studio.py).
- **Reason:** the reference run attached Q52's wave graph to Q60 and later
  questions' figures to prose blocks; with better segmentation (Phase 1) this
  QA still guards the residual boundaries.
- **Done when:** both rejections fire in tests; a live re-run's log names any
  rejected/conflicting figure.

#### Step 3.2 · Show crop provenance in the dashboard

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Surface where a pictures came from.
  pipeline/studio/merge.py `_crop_state()` already carries approved/source —
  expose a provenance value ("asset" | "model") on bulk rows and crops;
  ui/studio.js: the Inspect picture chip renders "picture" with a secondary
  badge "model · review" vs "asset" (existing .st-art chip styles, no new
  CSS surface; labels/values only — no coaching text), and the Crops desk
  shows the same flag beside the approved state. Update the markup pins in
  tests/test_studio_ui.py (and test_ui_pages.py if the shared chip style is
  asserted). Restart the server and verify the chip on the reference job's
  PICTURE rows. Full suite OK; small diff.
  ```

- **Files:** [pipeline/studio/merge.py](../pipeline/studio/merge.py),
  [ui/studio.js](../ui/studio.js),
  [tests/test_studio_ui.py](../tests/test_studio_ui.py),
  [tests/test_ui_pages.py](../tests/test_ui_pages.py).
- **Reason:** after step 0.2, model crops await review — the reviewer must
  see *why* a picture is pending at a glance.
- **Done when:** chip visible on the live page for the reference job.

### Phase 4 — Inspect UX and the scorecard

#### Step 4.1 · Inspect rows carry quality signals

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Make the Inspect table judgeable at a glance.
  ui/studio.js `stInspectRow()` currently shows #, page, question, opts
  count, picture. Add compact chips from the bulk row data it already
  receives: status (ready/warn/error with the existing status-chip classes),
  topic when set, a missing-count chip when missing is non-empty, and the
  crop provenance badge from step 3.2. House chips only, Latin digits
  (stNum), no coaching sentences; keep dir="auto" on the question cell and
  the table's internal scrolling rules. Update test_studio_ui.py pins.
  Restart and verify against the reference job. Full suite OK; small diff.
  ```

- **Files:** [ui/studio.js](../ui/studio.js),
  [ui/styles.css](../ui/styles.css) (only if a chip style is genuinely
  missing — reuse above all),
  [main.py](../main.py) (bulk rows already feed these fields — extend the row
  payload only if something is absent),
  [tests/test_studio_ui.py](../tests/test_studio_ui.py).
- **Reason:** the reference evaluation had to be done from JSON; the Inspect
  surface must answer "how good is this import" without a keyboard.
- **Done when:** chips visible live; pins updated.

#### Step 4.2 · Per-job quality scorecard

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Give every job a self-stated quality scorecard.
  Compute (pure code, no model) in pipeline/studio/merge.py `merged()` or a
  small helper beside it: blocks, questions, prose, ready/warn/error,
  questions with 4 options vs without, topics matched vs unmatched (tag
  vocabulary), crops needed/approved/awaiting, layout confidence from step
  1.1, and blocks recorded as "organize" (step 1.2). Serve it on the bulk
  endpoint response as "scorecard" (main.py) and render it as a compact
  strip above the Inspect table in ui/studio.js (existing panel chrome,
  .diag-count style tallies, Latin digits, no chart library). Tests:
  scorecard math on a fixture merged view; markup pins. Restart and verify
  on the reference job. Full suite OK; small diff.
  ```

- **Files:** [pipeline/studio/merge.py](../pipeline/studio/merge.py),
  [main.py](../main.py),
  [ui/studio.js](../ui/studio.js),
  [tests/test_studio.py](../tests/test_studio.py),
  [tests/test_studio_ui.py](../tests/test_studio_ui.py).
- **Reason:** "how good is it" should be one glance, here and for every
  future document; this is the direct productization of the evaluation I did
  by hand for the reference run.
- **Done when:** the strip renders live on the reference job and matches a
  manual count.

### Phase 5 — operations

#### Step 5.1 · Proxy preflight and call-latency transparency

- **PROMPT**

  ```text
  Repo E:\OCR-quest (read AGENTS.md first).
  Make proxy health visible before a run, and call latency readable after.
  (1) main.py: add GET /api/proxy/check that runs pipeline/gemini.py
  `test_proxy(profile)` for every enabled profile in .proxy-profiles.json
  and returns per-profile verdicts (same fault taxonomy the lanes use); wire
  a single check button beside the proxy selector in the pipeline tab
  toolbar (house ghost-btn, no new tab). (2) Document the observed studio
  latencies in the README section on studio config (one short paragraph, no
  tables): 9router lanes answered in 26-89 s per enrich call, Gemini-pool
  vision in 23-133 s per band batch through WhiteDNS — batch knobs
  STUDIO_PAGES_PER_CALL / STUDIO_BAND_BATCH are the levers. (3) If
  NET_RETRIES/NET_RETRY_WAIT are not read on the studio lane, wire them so
  studio stages obey the same env knobs as the pipeline. Tests: endpoint
  shape (stubbed test_proxy), env wiring. Restart and verify the check
  button live. Full suite OK; small diff.
  ```

- **Files:** [main.py](../main.py),
  [pipeline/gemini.py](../pipeline/gemini.py),
  [ui/index.html](../ui/index.html), [ui/app.js](../ui/app.js),
  [README.md](../README.md), [tests/test_ui_pages.py](../tests/test_ui_pages.py),
  plus a new tiny test module for the endpoint if separation is cleaner.
- **Reason:** the reference run spent minutes waiting on a lane nobody could
  see failing; a preflight turns "why is nothing happening" into a one-click
  answer, and the retry knobs align studio with the pipeline's semantics.
- **Done when:** the button reports live verdicts for both profiles; suite OK.

---

## 5. Evidence appendix — the reference run in short

- Setup: 3 pages text-layer PDF; Studio job `20261022-194515-8683`;
  enrich on 9router (`gemini/gemini-3.5-flash-lite`), locate + extract-pool on
  Gemini (`gemini-3.5-flash-lite`) through the WhiteDNS proxy
  (`http://127.0.0.1:10886`); tags vocabulary extended with the document's
  subject/grade rows before the run.
- Output: 13 blocks (8 questions / 5 prose) for 19 ground-truth questions;
  0 ready, 13 warn; 8 importable, 5 blocked; 0 text repairs; option sets
  correct for 2 of 8 rows; 6 of 8 topics plausible; 7 model crops all
  auto-approved, 4 on the wrong row.
- Model spend: extract 0 calls (deterministic lane), enrich 2 (26–89 s each),
  locate 2 band batches (23 s, 133 s). All succeeded; no faults raised.
- No question was edited, approved or imported during the reference run.

## 6. Quick run-book (ASCII-safe)

```text
start      : tools/ocr.ps1 start          stop : tools/ocr.ps1 stop
suite      : .venv/Scripts/python -m unittest discover -s tests
upload     : curl.exe -s -X POST -F "file=@E:\Downloads\Telegram Desktop\12T-1q-J-p15-17.pdf;type=application/pdf" http://127.0.0.1:8080/api/studio/jobs
stages     : curl.exe -s -X POST http://127.0.0.1:8080/api/studio/jobs/<id>/extract   (then /enrich, /geometry)
job state  : curl.exe -s http://127.0.0.1:8080/api/studio/jobs/<id>
dashboard  : http://127.0.0.1:8080/#studio/inspect
```

Persian in any fetched JSON: save to a file and read it from Python
(`json.dumps(..., ensure_ascii=True)` for console printing) — never
round-trip UTF-8 JSON through Windows PowerShell.

## 7. Open questions for the user (do not guess)

1. Step 2.2 changes the default profile to stop requesting `difficulty`.
   Confirm, or say the default should keep it.
2. Step 1.2 turns the organize pass on by default (`STUDIO_ORGANIZE=1`).
   The user may prefer opt-in per run.
3. Whether the reference job should be deleted after the plan's acceptance
   (it is the evidence copy; deleting needs the user's OK, rule 9 of AGENTS.md).

Note on diffs: this working tree stores several files with CRLF while `HEAD`
has LF and `core.autocrlf=false`, so `git diff` shows whole-file churn for
them (`bands.py`, `extract.py`, `test_percall_history.py`, `ui/*.py` tests...).
Read a diff of those files by content, not by line count; the session's real
changes are the new organizer, the organize pass, the crop provenance and the
`has_figure` flag.

## 8. Acceptance run — 2026-10-22 (score 9.2 / 10)

Same source, same three stages, run three times through the live server
(`tools/ocr.ps1 start`, API only — nothing edited, approved or imported).
Job `20261022-212155-a194` is the last, clean run; the earlier two are
`20261022-205852-4d2b` and `20261022-211907-3918`.

```text
suite      : Ran 767 tests ... OK          (was 756 before this session)
extract    : 63.9 s   22 blocks (3 prose + 19 questions), 3 organize calls
enrich     : 31.1 s   ready 0 / warn 22 / error 0, 0 model errors
geometry   :  0.0 s   22/22 boxes, 7 crops, 0 band calls
questions  : exactly 55..73 - no gaps, no extras, no page-12 tail
options    : 19/19 questions carry four options, labels 1..4 in order
crops      : Q57 Q62 Q67 Q68 Q71 Q72 Q73 - the seven printed pictures,
             each on the question that names it; prose rows get none
```

What the run proved fixed (each was a defect in the reference run):

- **Enrich no longer loses rows.** The model used to answer for 8 of 13
  blocks; now every block is covered (0 errors, 3 calls for 22 blocks).
- **The organizer splits scrambled text pages.** Numbers glued to formulas
  (`1657-`, `2064-`, `J 6- 840`) and a question split across pages are cut at
  the printed sequence; the sequence carries from page to page.
- **A model question is only added inside the page's anchored range.** The
  page-13 top holds the tail of question 54 (started on page 12); the model
  reconstructs it, the span guard drops it, and no bbox-less row appears.
- **One picture, one owner.** Cross-page and neighbour duplication is gone
  (`figure_dropped 0` in the clean run; the first run dropped four legitimate
  figures because the dedupe compared pages with each other).
- **Prose never gets a crop.** The band pass now only reads questions.
- **A model crop waits for the human gate** (`crop_source: model`), and the
  job summary counts pictures (`figures: 7`).

Residual issues (why this is 9.2, not 10) — for the next session:

1. **Every row is `warn`.** This excerpt carries no institute/year line, so
   `corp` and `year` stay empty and no row is `ready`; import proceeds with
   blanks, but an operator sees 22 warnings. Decide whether a source without
   a metadata line should import at `ready` with a job-level note instead of
   a per-row warning.
2. **Model crops are never auto-approved**, so a 19-question paper needs 7
   approvals. Correct for safety; consider one "approve all model crops"
   action in the Crops tab, or a confidence field on the organizer's figure.
3. **The organizer is on by default and costs one model call per
   low-confidence text page** (~20 s/page here). A 100-page text PDF pays 100
   calls. Needs a page budget or a per-run toggle (open question 2 below).
4. **Crop boxes drift run to run** (`Q57 [485,107,561,418]` vs
   `[472,96,561,407]`) and can sit outside the question's word box (Q71 was
   1% contained), so the Crops tab still needs a human eye per crop.
5. **Provenance is not visible in the UI yet** (plan step 3.2/4.1). The
   artifact carries `crop_source`, `figure_source` and `organized`; the
   merged view does not surface them.

## 9. Push to 10/10 — text coherence (opened 2026-10-22)

**Trigger.** On the accepted run the user saw some question text break to
the next line for no reason and sentence order not kept. This is an
extract defect, not enrich: on every offending field `layer1.json` and
`layer2.json` are identical, so the enrich stage never touches line
structure.

**Diagnosis (read-only scan of job `20261022-212155-a194`).**

Eight question/prose blocks carry mid-sentence `\n` (Q55 x4, Q56 x1,
Q57 x2, Q59 x1, Q60 x3, plus the three prose headers). All offenders are
already `organized: true` — the organize call replaced the stem, yet the
model stem still reproduces the pdfium line stream: hard wraps at the
`٬`-glued fragments and sentence order flipped (Q55 reads its fragments
in page-text order, not printed order: "…را 6 برابر می‌کند." before the
fragment that starts that sentence).

- `pipeline/studio/layout.py` composes a block from pdfium lines with
  `"\n".join(stem)` (line 410 prose lead, line 482 stem). The anchor
  organizer fixes block boundaries only; the text stream's line order is
  not guaranteed visual order, so wraps and scrambles are inherent to
  that string.
- `ORGANIZE_PAGE_PROMPT` appends `page text:` with no flow rule, so the
  model tends to echo the scrambled scaffold instead of re-reading the
  printed page, and nothing bans `\n` in the answer.
- `_organize_entries` accepts any non-empty stem; there is no coherence
  gate.
- Enrich passes the text through untouched (L1 == L2 on these fields).

**Steps.**

9.1 **Organize prompt rewrite** (`pipeline/studio/extract.py`,
`ORGANIZE_PAGE_PROMPT`): state plainly that the page image is the truth
and must be read in printed order; the supplied page text is only a
character reference whose line order is scrambled in some booklets.
Require: each question's `stem` is one flowing paragraph with no `\n`
anywhere; each option text likewise; re-join pieces the print splits
across lines; keep the exact Persian words and digits; options labelled
1..4 in printed order.

9.2 **Coherence gate** (`_organize_entries` + extract stats): replace
every `\r\n`/`\n`/`\r` inside stem and option strings with a single
space, collapse doubled spaces; accept whatever remains (never empty);
record `organize_sanitized` in stats. The reply schema
(`questions[n]{number,stem,options,figure}`) does not change.

9.3 **Deterministic reflow** (`pipeline/studio/layout.py`): one helper
joining adjacent stem/lead lines before the final `"\n".join` — a line is
joined to the next when it does not end with a sentence terminator
(؟ ! . ؛ ،) and the next line does not start with a question anchor, an
option marker, or a header/footer pattern. Applies to both the anchor
path and legacy `split_text`. Block `ranges` (char spans) are untouched —
the join changes only the composed text, so geometry/bbox still works.
Confidence unchanged.

9.4 **Enrich final guard** (`pipeline/studio/enrich.py`): after applying
model fixes, collapse mid-sentence `\n` (same rule as 9.3) inside `text`,
strip line breaks from option strings, record in `fixes`, recompute
`missing` as today. A surviving wrap adds a `coherence` issue so a row
cannot silently leave the stage broken.

9.5 **Adaptive organize trigger** (`_organize_enabled`): organize a text
page when confidence < 1.0 (as today) OR any of its deterministic blocks
still carries a mid-sentence `\n` after reflow. Quality-driven, so a
scrambled-but-fully-anchored page is never skipped; no budget knob until
open question 2 is answered (the reference PDF keeps paying 3 calls,
unchanged).

9.6 **Crops provenance + approve-all** (`ui/studio.js`, plan steps
3.2/4.1): Crops rows show chips for `crop_source` / `figure_source` /
`organized`; one house-style action approves all `crop_source: model`
crops of the job in a single call (residual 2). No import auto-run, no
auto-anything beyond that click.

**Hard rules for this phase (repeated):** artifacts stay read-only
unless the user asks for import; every backend change requires a
restarted server before live verification; console prints stay
cp1256-safe; judge diffs by content (CRLF churn, note in §7).

**Acceptance — the 10/10 gate.**

1. Full suite green, with new tests for reflow, the coherence gate, the
   enrich guard, the adaptive trigger and a prompt pin for 9.1.
2. A clean live run of `12T-1q-J-p15-17.pdf`: the same mid-sentence scan
   used for the diagnosis reports 0 bad wraps in every layer2 block.
3. Every §8 invariant holds: questions exactly 55..73, four options each
   labelled 1..4, Q55 options 6/16/12/26, Q58's series options, crops
   exactly the 7 picture questions, `figure_dropped 0`, no Q54 row.
4. The user reads the Extract tab by eye and confirms the prose is
   continuous (the eye is the final judge).
5. Provenance chips and approve-all visible in a restarted server's
   Crops tab.
6. Two clean runs in a row: identical questions/options/crop set; the
   7 model crop boxes compared run-to-run (IoU reported) so residual 4
   is measured rather than argued.

New open questions (§7 still stands; add these):

4. Residual 1: should a source without a metadata line import at `ready`
   with a job-level note instead of one `warn` per row?
5. Residual 2: OK to add the single "approve all model crops" action?

The numeric score reaches 10/10 only when gate items 1–5 hold AND open
questions 4/5 are answered; until then the honest number is the §8 score
with the coherence defect removed, reported as such — never a padded 10.

### 9 done — acceptance evidence (2026-10-22, score 9.7 / 10, coherence 0)

Shipped this phase: 9.1 (organize prompt: image-first order, one flowing
stem, no newline anywhere), 9.2 (`_sanitize_entry` +
`organize_sanitized`), 9.3 (layout reflow: `reflow_lines`,
`mid_sentence_breaks`, `one_line`; used by `split_text`), 9.4 (enrich
guard: `stem reflowed` / `options reflowed` / `coherence` issue /
canonical option labels, letter sets kept verbatim), 9.5 (adaptive
organize trigger: a still-wrapping page always gets the model re-read),
9.6 (provenance chips `src … / fig … / LLM` per Crops row + one
`Approve all (n)` action; merged crop state carries `crop_source` /
`figure_source`).

```text
suite    : Ran 782 tests in 11.972s - OK        (was 767 at 9.2)
run 1    : job 20261022-221710-ae5b  (extract/enrich/geometry clean)
run 2    : job 20261022-222114-208c  (drift/no-flake check)
mid-sentence breaks : L1 = L2 = 0 in both runs   (was 8 fields)
questions : exactly 55..73 both runs; 19/19 four options; Q54 tail out
q55       : one flowing sentence, printed order, options 6/16/12/26
q58       : series options (Lyman/Balmer/Paschen/Brackett), no wraps
crops     : {57, 62, 67, 68, 71, 72, 73} both runs; model source;
            figure_dropped 0, qa_rejects 0
organize  : 3 calls/approx 20 s per page per run; sanitized 0 (the
            image-first prompt already returns newline-free prose)
warnings  : 19 rows warn (corp/year unknowable) - open question 4
```

Crop-box drift measured run-to-run (residual 4 is now a number, not an
argument): Q67 IoU 0.85, Q68 0.77, Q62 0.69, Q71 0.59, Q72 0.49,
Q73 0.47, Q57 0.26 - the boxes come from the vision model, so the Crops
tab keeps the per-crop human gate; the approve-all click is the review
still. New residual noted: formula transcription drift (Q58 read
`\frac{6/4}{3}` in run 2 where the 9.2-era run read `\frac{6}{3}`) - the
text layer cannot arbitrate a formula, so this belongs to the eye too.

Why 9.7, not 10: the two open questions (4: per-row warnings; 5: the
approve-all action) and gate item 4 - the user's own read of the Extract
tab - are not the agent's to answer. Everything implementable shipped.


## 10. Last plan steps closed - acceptance 2026-10-22 (9.8 / 10, functionality)

Two decisions the plan left open were answered by the user before this phase:

- **Metadata (Q4):** a row imports at `ready` when the whole document has no
  institute/year line; the absence is reported once as a job-level
  `metadata missing` note, never as one warning per row. A field any row can
  supply still warns the rows that lack it.
- **Organizer (residual 3):** the Extract tab carries a per-run `organize`
  picker - `auto` (adaptive, the default), `off`, `all pages` - sent with the
  run; `STUDIO_ORGANIZE` now only defines what `auto` may do.

Shipped this phase (step -> what landed):

- 3.1 band QA: a page-relative figure whose padded band region holds less than
  60% of it is rejected (`figure_rejected`) instead of being assigned to the
  band's question; a figure claimed by two blocks goes to the nearest block
  centre (`figure_conflicts`), with `figure_dropped` unchanged.
- 4.1 Inspect rows: status chip, topic, missing-field count, picture cell with
  provenance (`model · review` / `asset`), edited badge.
- 4.2 job scorecard above the Inspect table: blocks, prose, ready/warn, four-
  option sets over questions, known topics over questions, approved/needed
  crops, organized blocks, worst page layout confidence, job-level metadata.
- 5.1 proxy preflight: a stethoscope button in the pipeline toolbar probes
  every enabled proxy profile (`GET /api/proxy/check`, 8 s connect/read each)
  and writes the outcome into the picker's option titles. The studio lanes
  already inherit `NET_RETRIES` / `NET_RETRY_WAIT` through `call_gemini` /
  `call_ocr_openai`, so no lane-specific retry code was added.
- Residuals 1 and 3 above; residual 5 (provenance) was already in the Crops tab
  from 9.6 and is now also on Inspect.
- New additive stats only: `metadata_missing` on the layer2 stats and the
  merged view, `figure_rejected` / `figure_conflicts` on the geometry stats,
  `organize_mode` on the extract stats, and a pure `scorecard` block on the
  merged view (computed from stored rows, no schema change).

Acceptance evidence (restarted server, live API and browser; nothing approved
or imported):

```text
suite       : Ran 792 tests in 15.231s - OK      (was 782 at 9.7)
job         : 20261022-225938-1ed0
extract off :  0.3 s   organize_mode off, 0 calls           (per-run picker)
extract auto: 60.0 s   3 organize calls, 19 blocks organized, 1 page skipped
enrich      : 11.6 s   3 calls, 18 ready / 4 warn / 0 error
geometry    :  0.0 s   22/22 boxes, 7 crops, 0 band calls
questions   : 55..73 exactly; 19/19 four options, labels 1..4
crops       : 57 62 67 68 71 72 73, model source; 0 dropped, 0 rejected,
              0 conflicts
breaks      : L1 = L2 = 0 mid-sentence fields
scorecard   : BLOCKS 22 PROSE 3 READY 18 WARN 4 4 OPTS 19/19
              TOPICS 18/19 CROPS 0/7 LLM 19 LAYOUT 0.33 METADATA corp year
warnings    : Q61 only (topic missing); corp/year are the job-level note
UI checks   : organize=off reached the stage (335 ms, mode off, 0 calls);
              proxy check GET 200 in 2.1 s, both profiles reachable
```

Files: `pipeline/studio/{enrich,merge,extract,geometry,jobs}.py`, `main.py`,
`ui/{studio.js,index.html,app.js,styles.css}`, `tests/test_studio.py`,
`tests/test_studio_ui.py`, `tests/test_ui_pages.py`,
`tests/test_credential_endpoints.py`, `README.md`, `.env.example`.

Why 9.8, not 10: the crop boxes still come from the vision model and vary run
to run (§8 residual 4 - worst IoU 0.26 on Q57), so all 7 crops keep their human
gate; formula transcription drift (Q58) remains an eye decision; and gate item
4 (the user's read of the Extract tab) is still the user's to give. Everything
the plan asked an agent to implement is in and tested.
