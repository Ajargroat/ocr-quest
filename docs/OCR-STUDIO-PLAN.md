# OCR Studio — Complete Design Plan

> Status: **draft for review** · Author: lead (this session) · Target app: **OCR-quest** (Python/FastAPI + `ui/`)
> Verified against the real file: `E:\Downloads\Telegram Desktop\Test hadaf Tajrobi.pdf`
> (528 pages, **text-selectable**, multi-subject: زیست / ریاضی / شیمی, آل این وان compilation, answer-key pages at the end).

---

## 0. The problem in one paragraph

The current pipeline assumes **one rigid shape**: `konkour-ocr/{subject}/{grade}/{topic}/{question|answer}/<file>`,
one rasterised page per file, one Gemini call, one row per question, and a picture cropped
*browser-side* from a **stored full-page JPEG** using a normalized `[ymin,xmin,ymax,xmax]` bbox.
Real-world files break every one of those assumptions:

- A single PDF is a **compilation** (one institute × many years, or many institutes × one year,
  or several subjects) — the metadata that "should" come from the folder path is actually
  written *inside* the document.
- The PDF may be **text-selectable**, not scanned images. Today's converter rasterises it anyway,
  then an LLM re-reads pixels it could have read as text (wasting tokens and accuracy).
- The bbox/crop contract depends on a stored page image and a CSS frame. For a text PDF that is
  both wasteful (store megabytes of pixels) and wrong (the box should come from the text layer).
- Questions and answers may be **combined**, **separate**, or **answer-key-only** (the Tajrobi file
  ends with dozens of pages of `۱۲ ۳  45 ۱  56 ۲` key tables — no explanations at all).
- Metadata (institute, year, subject, chapter/topic) is **inconsistent and typo-prone** in OCR
  (`قلمچی` / `قلم‌چی`, `دیاز` / `دیاژ`), and must be **canonicalised against a controlled tag list**.

**Goal:** one **OCR Studio** subsystem that can ingest *any* PDF/image, auto-determine which fields
are static (from the file) vs. which must be extracted, produce an inspectable **bulk intermediate**,
enrich/repair it with a second LLM layer, fix the crop geometry, let a human review, and finally
bulk-import — all driven from a new sidebar section with per-stage model pickers.

---

## 1. Decisions locked with the user

| # | Question | Decision |
|---|----------|----------|
| D1 | Where does the section live? | **OCR-quest dashboard** (`main.py` + `ui/index.html`), a new sidebar entry next to Pipeline / QBank / Revision. |
| D2 | bbox/storage strategy | **Hybrid**: text PDFs → exact **text-layer** boxes; image PDFs → **vision** boxes; both stored as a bbox over **one source artifact** (the PDF or page image), cropped **on demand server-side** into a cache. |
| D3 | Right-now deliverable | **Complete plan + a throwaway prototype of the hard stage.** (Prototype already run — see §9.) |
| D4 | Final destination | Must work with **different databases the user configures** → the intermediate is canonical; import is **pluggable** (Supabase today, Sapienstech DB / export file later). |
| D5 | Tag list source | Not needed now, but **build the seam** for later AI tagging of books/institutes; a curated **tag list** is loaded and compared against. |
| D6 | New subsystem vs. rework | **Brand-new parallel subsystem** (new modules + new tables), reusing shared infra (key pool, `gemini_router`, `faults`, `storage`, UI shell). The old pipeline keeps working untouched. |
| D7 | Where the bulk lives | **Plain files on disk** — a workspace dir (`source`, `layer1.json`, `layer2.json`, `crops/`). DB only at final import. Easy to re-inspect & cite. |
| D8 | Layer-2 linking | **Yes** — layer 2 links answer blocks to questions and sets the correct option (reuse existing pairing logic). |
| D9 | Human review screen | **In v1.** |

---

## 2. Architecture — the five stages

```
                ┌──────────────────────────────────────────────────────────────┐
  any PDF/img → │  STAGE 0  INTAKE + PROBE                                      │
                │  · rasterise? no → detect text layer (per page char count)    │
                │  · detect language, page count, has-figures, answer-key pages │
                │  · pick the "profile" (see §3) → which fields are STATIC       │
                └───────────────────────────┬──────────────────────────────────┘
                                            ▼
                ┌──────────────────────────────────────────────────────────────┐
                │  STAGE 1  LAYER-1 EXTRACTION  (LLM #1, per model picker)      │
                │  · text PDF  → feed TEXT LAYER (cheap, exact)                  │
                │  · image PDF → feed rendered PAGES (vision)                    │
                │  · emit a raw, faithful BULK  →  workspace/layer1.json         │
                │  · each question carries {text, options, numbers, tags?, ...}  │
                │  · bbox NOT resolved here unless it's a text PDF (exact)        │
                └───────────────────────────┬──────────────────────────────────┘
                                            ▼
                ┌──────────────────────────────────────────────────────────────┐
                │  STAGE 2  LAYER-2 ENRICH + REPAIR  (LLM #2, own model picker) │
                │  · fill missing required fields (else STALL the question)      │
                │  · Persian typo/grammar repair                                 │
                │  · canonicalise corp/subject/topic against the TAG LIST         │
                │  · LINK answers→questions + set correct option                 │
                │  →  workspace/layer2.json  (the reviewed bulk)                 │
                └───────────────────────────┬──────────────────────────────────┘
                                            ▼
                ┌──────────────────────────────────────────────────────────────┐
                │  STAGE 3  GEOMETRY / CROP  (deterministic + optional AI)       │
                │  · text PDF → exact char-box union per question (PROVEN §9)    │
                │  · image PDF → vision bbox, then a "does it look right?" check │
                │  · write bbox into layer2.json; render a preview crop to cache │
                └───────────────────────────┬──────────────────────────────────┘
                                            ▼
                ┌──────────────────────────────────────────────────────────────┐
                │  STAGE 4  HUMAN REVIEW  (in-UI editor)                         │
                │  · side-by-side: source page + parsed row + crop preview       │
                │  · edit text/options/tags; drag the bbox; re-crop live         │
                │  · approve / stall / reject per row                            │
                └───────────────────────────┬──────────────────────────────────┘
                                            ▼
                ┌──────────────────────────────────────────────────────────────┐
                │  STAGE 5  BULK IMPORT  (pluggable target)                      │
                │  · Supabase tables today · Sapienstech DB / export file later  │
                │  · writes source artifact once, questions/answers many         │
                └──────────────────────────────────────────────────────────────┘
```

**The "bulk" is the contract.** `layer1.json` and `layer2.json` are the stable hand-off between
stages and between AIs. Every stage reads and writes files; the UI cites the same files. No stage
is obliged to *select* rows from a DB to do its work — which is exactly the property you asked for.

---

## 3. Stage 0 — Intake, probe, and the "profile"

This is the **starter point** you described: *an agent briefly inspects the file and decides which
fields are static.* We formalise it as a **profile** (a JSON blob saved in the workspace).

### 3.1 Probe (deterministic first, cheap)

`pipeline/studio/probe.py` reads, per PDF:

- `page_count`, per-page **char count** (text layer present?) → `text_pdf` vs `image_pdf` (mixed allowed).
- page size(s), language guess (Persian ratio), figure count (pdfium object types).
- heuristics for **answer-key pages** (dense number/option grid, e.g. the `پاسخ کلیدی:` pages).
- optional **sample OCR** of 2–3 pages (one small vision call) to let an LLM label the compilation.

### 3.2 Profile (the static-vs-extracted decision)

```jsonc
{
  "profile_id": "…",
  "source_kind": "text_pdf",              // text_pdf | image_pdf | mixed | images
  "compilation": "single_institute_multi_year", // see enum below
  "static": {                              // fields we KNOW and pin, not extract
    "subject": "زیست‌شناسی",
    "corp": null,
    "year": null,
    "grade": null
  },
  "extract_per_question": ["corp", "year", "topic", "difficulty"],
  "answer_layout": "key_pages_at_end",     // inline | separate_section | key_pages_at_end | none
  "tag_list_ref": "tags/default.json"
}
```

`compilation` enum (mirrors your point #1):
`single_institute_multi_year` · `multi_institute_single_year` · `multi_institute_multi_year` ·
`single_subject_multi_chapter` · `multi_subject` · `unknown`.

**How the profile is decided:** Stage 0 runs a **short inspection agent** (small/cheap model,
2–3 page sample + the probe stats) that returns the profile. The user can **override every field**
in the UI before Stage 1 runs. Anything left `null` in `static` becomes an
`extract_per_question` target → if it can't be filled in Stage 2, the question **stalls** (your point #2).

> Why a profile and not just "ask the LLM to fill everything"? Because *known* facts must never be
> guessed. Pinning `subject=زیست` for a زیست-only file removes a whole class of OCR errors before
> they happen — the same reason `scanner.py` already says *"Metadata comes from the folder, never
> from Gemini."* We keep that principle; we just make the "folder" a per-file profile.

---

## 4. Stage 1 — Layer-1 extraction (LLM #1)

**Output: `workspace/<job>/layer1.json`** — a faithful bulk, no repair, no enrichment.

```jsonc
{
  "job_id": "…", "source": "Test hadaf Tajrobi.pdf", "profile": {…},
  "blocks": [
    {
      "kind": "question",            // question | answer_block | answer_key_entry
      "page": 2, "order": 1,
      "number": 1,
      "text": "کدام مورد …",          // verbatim, Persian preserved, ZWNJ kept
      "options": [{"label":"1","text":"…"}, …],
      "metadata_line": "(داخل۱۴۰۱)",   // raw, un-parsed
      "corp": null, "year": null, "topic": null, "difficulty": null,
      "has_figure": false,
      "bbox": null,                  // filled in Stage 3 (or exact here for text PDF)
      "raw_ocr_text": "…",
      "stall": null                  // set by Stage 2 when a required field is unfillable
    }
  ]
}
```

Key choices:

- **Text PDF path (the big win).** We do **not** rasterise first. We extract the PDF text layer
  (pdfium `get_textpage()`), split into question/option spans with the deterministic
  question-number detector, and hand **text** to LLM #1 — cheaper, faster, and it *never* mis-reads
  a glyph it can read exactly. Numbers/digits are handled with the existing
  `utils.to_english_digits` / `to_persian_digits` discipline.
- **Image PDF / images path.** Render pages (reuse `converter._render_pdf`, 200 KB cap) and send
  page images to a vision model, exactly like today — but with the profile-aware prompt below.
- **Profile-aware prompt.** `questions.QUESTION_PROMPT` is extended: *"The subject is known to be
  X; do not guess it. Extract corp/year/topic only when printed."* This is the "statically filled
  rather than asking LLM" behaviour.

**Deterministic-first principle** (already the house style in `revision/`): anything code can settle
(question numbering, option splitting, page attribution) is settled by code; the LLM only reads
what code cannot.

---

## 5. Stage 2 — Layer-2 enrich + repair (LLM #2)

**Output: `workspace/<job>/layer2.json`** — the reviewed bulk. One block in → one block out, plus
optional link/correct-option annotations.

Responsibilities, in order:

1. **Enrich** — fill any field the profile marked as *extract* that is still empty. If a **required**
   field cannot be filled with confidence, set `"stall": "<reason>"` and mark the row
   `needs_human`. (This is your point #2, first example: *"stalls the question and rules it out for
   another editing session."*)
2. **Repair Persian** — fix typos/grammar in `text` and `options` (your second example). We reuse
   the **hard-rules** discipline from `revision/audit.py`: the model may rewrite text but must
   preserve meaning, keep numbers/LaTeX intact, and never invent content. A diff is recorded so the
   human sees what changed.
3. **Canonicalise against the TAG LIST** (your third, most important example). The prompt embeds the
   controlled vocabulary:
   - **Subjects** (existing `QuestionBankMeta.SUBJECTS`).
   - **Corps / institutes** (existing `QuestionBankMeta.CORPS` spelling-merge table: `قلمچی`≡`قلم‌چی`,
     `ماراثون`≡`مارآتون`, `دیار`≡`دیاژ`…). The LLM must map the OCR string to an existing tag, or
     propose a **new** tag (which the human approves, then it joins the list).
   - **Topics / sub-subjects** (e.g. `از یاخته تا جانوران`, `ساختار شیمیایی حیات`) grouped under a
     chapter. This is the new controlled list (§7).
   Comparison is done **fuzzy-first in code** (fold Arabic→Persian forms, strip ZWNJ — reuse
   `QuestionBankMeta::corpKey` logic ported to Python) and only the *residue* goes to the LLM, so we
   don't spend tokens on obvious matches.
4. **Link answers → questions + correct option** (D8). Reuse `revision/pairing.py` + `claims.py`
   semantics: match by number (with corp/year tolerance), then by unique option-text substring;
   derive `correct_option_label` from an explanation, or from an answer-key entry.

---

## 6. Stage 3 — Geometry / crop

### 6.1 The crop contract, fixed

Today: `questions.diagram_url` (a **full page JPEG** in Supabase) + `diagram_bbox`
(`[ymin,xmin,ymax,xmax]` normalized 0–1000) + `cropped-image.blade.php` frames it with CSS.
The Sapienstech side already parses exactly this in `App\Support\BboxParser` (`frame='yxxy'`).

**New rule (hybrid, D2):** the bbox is always expressed **over one source artifact**, and the crop
is produced **server-side on demand**:

- **Text PDF** → bbox = union of the **character boxes** of the question's text span, converted to
  the same normalized `yxxy` frame. **Exact, zero AI, zero ambiguity.** *(Proven — §9.)*
- **Image PDF / images** → bbox = the vision model's box, then an **auto QA re-roll** (§6.3).
- **Storage**: store the **source artifact once** (the PDF, or a page image if the source was loose
  images). Store **bboxes only** per question. Crop on demand into a **local cache** (`workspace/crops/`)
  and serve via a new `/api/studio/crop/{job}/{n}` endpoint — *not* one image per question in the
  bucket. This directly answers your "saving a lot of pictures consumes a lot of space" concern.
- The existing Sapienstech `BboxParser` + `cropped-image` keep working unchanged for **old** rows;
  new rows simply carry a bbox over a PDF-derived page image, and we add a server-crop path for the
  studio. No breaking change.

### 6.2 Why server-side crop, and what changes downstream

The browser-CSS crop needs the full page image **client-side**. For a 528-page PDF that means 528
page JPEGs in the bucket even though we only need ~600 small crops. Server-side cropping lets us
**store the PDF once** and generate small JPEGs on demand (and cache them). If you later want to
keep the pure-CSS approach, the same bbox still works — the studio just *also* knows how to bake the
crop. We keep both doors open.

### 6.3 The "bad bbox" fix (your point #3)

For vision boxes only, Stage 3 runs a **verify-and-re-roll** loop:

1. Render the candidate crop.
2. A cheap check decides "good enough": does the crop contain the expected text (compare against
   `layer2.json` text via a fast OCR/`get_text`-overlap or a tiny vision call)? Is it non-degenerate
   (not mostly blank, not a sliver, not the whole page)?
3. If bad → re-prompt the vision model **with the failed crop + the target text** ("re-box so the
   full question is inside"), up to N re-rolls; keep the best by the check score.
4. Record `bbox_source: "text_layer" | "vision" | "vision_reroll"` and a confidence so the human
   sees which boxes to distrust.

---

## 7. The tag list (controlled vocabulary)

A versioned file `workspace/tags/<name>.json` (and a canonical seed in the repo):

```jsonc
{
  "version": 1,
  "subjects": ["زیست‌شناسی", "شیمی", "فیزیک", "ریاضی", "زمین‌شناسی"],
  "corps": [{"tag": "قلم‌چی", "variants": ["قلمچی", "قلم چی"]},
            {"tag": "دیار",  "variants": ["دیاژ", "دیّار"]}, …],
  "chapters": [
    { "subject": "زیست‌شناسی", "grade": "دهم",
      "chapter": 1, "topics": ["سطوح سازمانیابی حیات", "…"] }, … ]
}
```

- **Seed** from the existing `QuestionBankMeta` (subjects + corps) + the topics already in the bank.
- **Grow** when the LLM proposes a new tag the human approves in the review screen (D5 — the seam
  for later AI tagging of books/institutes).
- The list is the **single source of truth** compared in Stage 2 and validated at import.

---

## 8. Data model, workspace layout, and import

### 8.1 Workspace (files = source of truth, D7)

```
workspace/
└── <job_id>/
    ├── source.pdf                 (or source/ page images)
    ├── profile.json               (Stage 0)
    ├── layer1.json                (Stage 1 raw bulk)
    ├── layer2.json                (Stage 2 enriched bulk  ← the reviewed artifact)
    ├── review.json                (human edits/approvals, overlays layer2)
    ├── crops/                     (on-demand cache)
    └── log.jsonl                  (per-stage events, cite-able)
```

### 8.2 New tables (new subsystem, D6) — only used for status/index + final import

```
studio_jobs      (id, source_path, kind, status, stage, profile_json, created_at, …)
studio_stages    (job_id, stage, model, status, started_at, finished_at, stats_json)
studio_tags      (id, kind, tag, variants_json, subject, grade, chapter)   -- the tag list, queryable
studio_imports   (job_id, target, status, rows, created_at)                -- audit of Stage 5
```

The **question/answer payload itself stays in files** until import; these tables are the index the
UI lists and the audit trail. (Matches D7: DB only at final import for payload.)

### 8.3 Stage 5 — pluggable import (D4)

`pipeline/studio/importers/`:
- `supabase.py` — writes the same `sources`/`questions`/`answers` shape the current pipeline writes,
  so Sapienstech's existing `bank:import` consumes it unchanged.
- `file_export.py` — emits JSON/SQL for any DB you configure.
- `sapienstech_db.py` (later) — direct.

One `layer2.json` → one importer call. Re-runs are idempotent via a stable per-row key
(like today's `supabase_id`).

---

## 9. The prototype (already run) — proving the hard stage

I prototyped the **text-layer exact bbox** on the real Tajrobi PDF (page 2, question 1):

- Read `pdfium` text page → per-char boxes (2157 chars on that page).
- Unioned the char boxes of the question-1 span → PDF-point rect `(63,5)–(533,722)`.
- Normalised to the existing `yxxy` 0–1000 frame → `[142, 106, 994, 896]`.
- Rendered at 2× and cropped → **a pixel-perfect question image** (verified visually).

**Conclusion:** for text PDFs the crop needs *no AI at all* and is exact. This de-risks the single
hardest requirement and validates D2. (Script was throwaway; artifacts cleaned up.)

---

## 10. UI — the new sidebar section

A new `data-view="studio"` entry (`ui/index.html`) with sub-panels, mirroring the Database tab's
segmented idiom:

| Panel | Purpose |
|-------|---------|
| **Jobs** | list workspaces, stage progress, start/stop, open a job |
| **Profile** | show the Stage-0 profile; let the user override static fields before Stage 1 |
| **Bulk** | the layer-1/layer-2 table: every question, editable; stall flags highlighted |
| **Crops** | source page + crop preview; drag-to-adjust bbox; live re-crop |
| **Tags** | the controlled vocabulary: view/add/approve tags |
| **Models** | **per-stage model pickers** (Stage 0 probe, Stage 1 OCR, Stage 2 enrich, Stage 3 vision) — same pattern as the Credentials tab, so surveillance/debug is easy (your requirement) |
| **Import** | choose target, dry-run, import, see results |

**Per-stage model config** is stored in `.env` (new keys, same `_providers` machinery in
`config.py`), e.g. `STUDIO_PROBE_PROVIDERS/_ACTIVE`, `STUDIO_OCR_*`, `STUDIO_ENRICH_*`,
`STUDIO_VISION_*`. Each resolves to the existing `call_gemini` / `call_ocr_openai` lanes, so no new
network code — just new pickers.

---

## 11. Module plan (new subsystem, D6)

```
pipeline/studio/
├── __init__.py
├── probe.py          Stage 0: text/image detection, page stats, sample
├── profile.py        profile schema + defaults + static/extract decision
├── layout.py         text-layer extraction: char boxes, question/option spans
├── extract.py        Stage 1: LLM #1 call + prompt building (text or vision)
├── enrich.py         Stage 2: LLM #2 repair/enrich + tag canonicalisation
├── link.py           Stage 2: answer↔question linking + correct-option (reuses revision/*)
├── geometry.py       Stage 3: exact text boxes, vision boxes, re-roll, crop render
├── tags.py           tag-list load/compare/propose (ports QuestionBankMeta folding)
├── workspace.py      job dirs, layer1/layer2/review read-write, log
├── jobs.py           run orchestration + status (mirrors revision/runner.py)
├── importers/        supabase.py · file_export.py · sapienstech_db.py (later)
└── migrations.sql    studio_jobs / studio_stages / studio_tags / studio_imports
```

Reuse (no duplication): `gemini_router`, `faults`, `vision`, `storage`, `utils`,
`revision/pairing.py`, `revision/claims.py`, `converter._render_pdf`, `db.Database`.

---

## 12. Phased delivery

| Phase | Deliverable | Why this order |
|-------|-------------|----------------|
| **P1** | Stage 0 probe + profile + workspace + Jobs/Profile UI | The "starter point"; everything keys off the profile. |
| **P2** | Stage 1 text-PDF path (exact, cheap) + layer1.json + Bulk (read-only) | Biggest win, lowest risk (text layer proven). |
| **P3** | Stage 3 text-layer geometry + Crop preview | Closes the bbox question for text PDFs end-to-end. |
| **P4** | Stage 1 image-PDF path (vision) + Stage 3 vision boxes + re-roll | Extends coverage to scanned files. |
| **P5** | Stage 2 enrich/repair + tag list + linking | The quality layer; needs the bulk to exist first. |
| **P6** | Human review editor (edit/drag/approve/stall) | In v1 per D9; most useful once P2–P5 exist. |
| **P7** | Stage 5 importers + per-stage model pickers polish | Ships value to the DB. |

Each phase is independently testable and leaves the old pipeline untouched.

---

## 13. Risks / open questions (please confirm or correct)

1. **Text-layer extraction quality on this PDF** — the PDF text layer is visually correct but
   sometimes **reordered** (RTL + two-column). Prototype confirms box math; a robust
   question/option **span splitter** is the real work in `layout.py`. *Acceptable?*
2. **Answer-key-only pages** (no explanations) — do we import questions with just a correct-option
   label and no explanation? *I assume yes, with `solution_text=null`.*
3. **Re-render vs. reuse** the existing `converter` — the studio needs its own rasteriser settings
   (maybe higher DPI for cropping). *I assume the studio renders its own, leaving the old converter
   for the old pipeline.*
4. **Workspace location** — under `OCR-quest/workspace/` (gitignored), one dir per job. OK?
5. **Old rows / migration** — no change to existing rows; studio rows are new. Confirm no backfill
   is expected now.
6. **Which models** for each stage — you'll pick in the UI; I'll default Stage 0/3 to the cheapest
   and Stage 1/2 to the strongest available.
7. **Tag list** — seed from the bank + `QuestionBankMeta` now, or wait until you supply the list?
   *I'll build the seam + a seed so it works either way.*

---

## 14. What "done" looks like

- Drop **any** PDF (text or image, single or compiled) into the studio.
- The studio **tells you what the file is** and **which fields are static**, and lets you correct it.
- Stage 1 produces a **faithful bulk**; Stage 2 **enriches/repairs** it against the **tag list** and
  **stalls** what it cannot fill.
- Every question gets an **exact or verified bbox**; crops are **re-rollable** and **cheap**
  (source stored once).
- A human **reviews** in the UI, then **bulk-imports** to any configured target.
- **Every stage has its own model picker**, so debugging is one dropdown away.

---

*End of plan.*

---

*Status (2026-10-17): the chain is live end to end — intake/probe/profile, Stage 1
(`extract`: deterministic text lane, ≤200 KB image lane → raw-text OCR, figures
stored once by content id), Stage 2 (`enrich`: fields, flags, tag folding, answer-key
links), Stage 3 (`geometry`: exact or model-verified bbox + crop cache), human review
(vision crops gated, text crops exact), continuous import (Database / JSON export,
idempotent). Code: `pipeline/studio/`; routes: `/api/studio/*`; UI: `ui/studio.js`.*
