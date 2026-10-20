# Konkour Studio - working guide

The Studio is the app's second, independent pipeline: it takes **one exam file**
(PDF, PNG, JPG or WEBP) - a single exam, a compiled booklet, a scan, a phone
photo - and walks it through seven stages until its rows are imported into the
database (or exported to JSON). It never touches the watched-folder queue:
uploading here does not start a Gemini run over `konkour-ocr/`.

Open it from the sidebar: **Studio**. One scrolling page - stage rail on top,
live strip under it, then the sections below in the order you use them.

## Where the bytes go

| thing | lives in |
|---|---|
| the uploaded file | `konkour-ocr/studio/<job>/` (the folder tree; `STUDIO_SOURCE_ROOT` moves it) |
| job state | `workspace/<job>/` - `profile.json`, `layer1.json`, `layer2.json`, `review.json`, `crops/`, `log.jsonl` |
| crop previews | rendered on demand from the page image into `workspace/<job>/crops/`, served at `/api/studio/jobs/<job>/crop/<key>` |
| published crops | bucket object `crops/<folder>/<key>.jpg`; a re-crop overwrites the same object id |
| imported rows | the lessons tables (`lessons`, `lesson_blocks`, `question_options`, ...) |

Everything before Import is files on disk: closing the browser loses nothing,
and re-opening a job reads the same artifacts back.

## The rail

**Intake - Probe - Extract - Enrich - Crop - Review - Import.** A stage's hub
fills in when its artifact exists (`profile.json`, `layer1.json`, `layer2.json`,
`geometry.json`, `review.json`, `imports.json`), so the rail is the answer to
"how far did this file get" at a glance. Under it, the live strip shows the
stage that is running right now, its message, and the page/chunk counters.

## 1. Jobs - start here

- **Choose a PDF or image...** picks the file; the upload streams it to disk and
  then runs Probe immediately, so the job is editable the moment the row appears.
- The table has one row per job: name + id, kind, subject, compilation, an
  **artifacts** row of chips (`profile` `layer1` `layer2` `geometry` `review`
  `imports` - lit = present), block count, created, and two actions:
  **open** (make it the active job) and **delete** (removes the job directory).
- Clicking anywhere on the row also opens it. **refresh** re-reads the jobs from
  disk - use it after editing `workspace/<job>/` by hand or after a crash.
- Re-uploading the same file makes a **new** job on purpose: a second pass is a
  legitimate thing to want. Old jobs do not interfere with new ones.

The active job is the one every other section below talks about; the breadcrumb
(`#stCrumb`, top of the toolbar) names it.

## 2. Profile - what this file is

Two columns plus three buttons.

**What the file is** - three selects and the probe stats:
- `source kind`: `text_pdf`, `image_pdf`, `mixed`, `images` (unknown is the
  default before Probe and stays only if nothing could be classified);
- `compilation`: `single_institute_multi_year`, `multi_institute_single_year`,
  `multi_institute_multi_year`, `single_subject_multi_chapter`, `multi_subject`
  - what is bundled into this one file;
- `answer layout`: `inline`, `separate_section`, `key_pages_at_end`, `none` -
  where the key sits relative to the questions.

The chips beside them are what Probe counted: pages, text pages, image pages,
answer-key pages, figure pages.

**Static fields (known - never guessed)** - the six facts that apply to the
whole file: `subject`, `corp` (institute), `year`, `grade`, `topic` (chapter),
`difficulty`. Type them once here instead of letting the model guess them per
question. A field with a value
is marked **pinned**, meaning extraction will not overwrite it; an empty field
is listed under *will be extracted* in the footer - that warning is the exact
to-do list for this file.

Buttons:
- **re-probe** - re-runs Stage 0 (keeps your edited profile).
- **Save profile** - writes `profile.json`. Always save before extracting.
- **Extract** - runs Stage 1 over the pages, then jumps to Bulk.

## 3. Bulk - the block table

One row per block the extractor found: `#` (number + block key), `Kind`
(question / prose / answer key entry / ...), `Text` (first ~70 chars), `Opts`,
`Status` (ready / warn / error), `Flags` (`split` when a question continues onto
the next page, `joined` when the halves were merged, plus the issue text),
`Crop` (cropped / review / -), `Import` (imported / -).

Footer stats: how many blocks are **ready / warn / error**. Buttons:
- **Enrich** - Stage 2: sort the blocks, repair the obvious damage, apply the
  tag vocabulary, health-check each row.
- **Locate** - the crop stage: asks the vision model for a bbox per question and
  caches the crop.

Click any row to jump into Crops with that block selected.

## 4. Crops - the review desk

The row of numbered pills at the top is the **crop queue** - every block that
needs a picture. Green = approved, amber = still waiting. Click a pill (or come
in from Bulk) and the desk shows:

- **the crop preview**, rendered from the page image with the stored bbox;
- **`Row · <key>`**: `kind`, `number`, `label`, `text`, `options` (one per
  line), `subject`, `topic`, `corp`, `year`, `grade`, `difficulty` - all
  editable, plus the issues list at the bottom;
- **`Crop`**: the four `bbox` fields (`ymin, xmin, ymax, xmax`, integers on the
  project's 0-1000 scale), a `joined` switch when the block was split across
  pages, an approval chip (*approved* / *awaiting approval* / *no crop needed*)
  and the buttons.

Buttons:
- **Approve** / **Reject** - the crop decision, saved immediately.
- **Save** - writes the field edits and the bbox as typed.
- **Re-crop** - renders the crop again with the bbox currently in the fields and
  publishes it: the object keeps the same id (`crops/<folder>/<key>.jpg`), the
  previous bytes are replaced.

The bbox fields take plain integers; Persian digits are accepted and converted.

## 5. Tags - the shared vocabulary

Three columns: **Subjects**, **Institutes**, **Chapters · topics**. `add` appends
a row, `remove` deletes it; chapters carry subject + grade + number so the
extractor can map "فصل ۳" to one canonical topic. **Upload file** loads a whole
JSON vocabulary, **Save tags** writes it.

This list is **global** - one vocabulary for every job in the app, not a
per-job fork. Editing it here changes what later jobs get.

## 6. Models - studio-local providers

- **Providers**: the Studio keeps its own model list (separate from the
  pipeline's credentials). One row per provider - label, API key, default model,
  model list; `add 9router` appends the local 9router provider (fixed base URL
  `http://127.0.0.1:20128/v1`).
- **Stages**: `extract`, `enrich`, `locate` each pick a provider from that list;
  leaving `extract`/`locate` empty means "Gemini pool" and leaving `enrich`
  empty means "no enrichment".
- **Proxy**: the Studio's own proxy picker (`Direct` by default) - independent
  of the pipeline's proxy selector.
- **Save models** writes them into `.env` through the same env store the
  credentials tab uses; keys never reach the browser back as plaintext.

## 7. Import - into the database or to a file

- **target**: `Database` (the lessons tables) or `JSON export` (one JSON file
  inside the job directory).
- **dry run**: when on, nothing is written - you get the counts and the reason
  list only. Run it first.
- The **queued** chips explain what will be skipped and why: *not a question*
  (prose blocks), *flagged* (status error), *crop awaiting approval*.
- **Import** runs it; the table underneath is the run history (when, target,
  questions, answers, skipped, dry), newest first.

The import is idempotent per job: running it again updates the same rows instead
of duplicating them, and the approved crops are uploaded once and reused.

## A full pass, in order

1. Studio -> **Choose a PDF or image...** -> wait for the row (Probe already ran).
2. Click the row (**open**), fix **source kind / compilation / answer layout**,
   fill the static fields you know, **Save profile**.
3. **Extract** - it returns on the Bulk table.
4. Skim Bulk: anything left `error` is worth a look before you continue.
   **Enrich**, then **Locate**.
5. In **Crops**: walk the pills, fix a bbox where the picture is wrong
   (**Re-crop** republishes it), **Approve** the good ones.
6. In **Import**: pick the target, switch **dry run** on, **Import**, read the
   counts, switch dry off, **Import** again.
7. Check the rows in **QBank** (see below) - that is the user-facing view of what
   you just imported.

## Gotchas

- **Backend edits need a restarted server.** `main.py` / `pipeline/*` changes are
  invisible to a running process; `ui/` edits only need a browser hard-reload
  (Ctrl+F5) - the static files are served from disk, but the browser caches them.
- **The Studio's model calls are real** (Extract / Enrich / Locate). They run
  server-side and can take minutes on a large file; the live strip is the
  progress indicator, not a fake spinner.
- **A crop is rendered from the page image on disk**, so a job whose source file
  was moved out of `konkour-ocr/studio/<job>/` can no longer re-crop.
- **Crops are the only thing that goes to the bucket.** Source files stay in the
  folder tree (the DB references a source by folder id) - that is deliberate, not
  an omission.
- **Delete removes the job directory** (artifacts and crop cache). The uploaded
  file in the folder tree is the source pipeline's business.

## QBank vs Studio

`QBank` is the browser/reviewer for rows **already in the database**: filters,
status chips, the raw-OCR toggle, approve/reject, and the picture for each
question. `Studio` is the intake that *produces* such rows from a new file.

A question in QBank shows its stored crop (`media_images[0].object`) whole,
exactly as the Studio published it. Because the bucket holds the finished crop
and not the page, QBank has no page to re-crop against - fixing an image is a
Studio job (Crops -> **Re-crop**), not a QBank one.
