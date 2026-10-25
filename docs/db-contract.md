# Database contract — Sapienstech consumer

Status: initial contract, 2026-10-25. The live schema is the flat Supabase
pipeline core; the Laravel Sapienstech app reads this database and will be
adapted to future additive changes.

## Stable identifiers and ownership
- `sources.id` is the source identifier.
- `questions.id` and `answers.id` are database-owned numeric IDs.
- `questions.legacy_id` is the stable external identity (historically
  `<sha1>-q<N>`); keep it and all existing IDs unchanged.
- `answers.legacy_id` and `answers.legacy_question_id` preserve legacy
  identities. `answers.question_id` is the nullable numeric FK.
- Do not rename/drop columns or repurpose existing JSON keys without explicit
  owner approval and a compatibility plan.

## Current data contract
- Core tables: `sources`, `questions`, `answers`; crops live in the
  `konkour-pages` Supabase Storage bucket.
- `questions.media_images` is an array of objects with `bbox`, `page`,
  `object`; optional additive metadata may include `caption`, `asset_type`,
  and `order`.
- `answers.assets` preserves `asset_type`, `bbox`, `caption`, and
  `order`. Table assets may additionally carry `text` and `text_format`
  (`markdown_table` or `latex`). Keep picture crops as visual evidence and
  fallback, especially for figures needed to interpret an explanation.
- Answer-picture crops are represented in `answers.media_images` using the
  existing `{bbox,page,object}` shape and correlate to asset `order`.
- `review_status` is human workflow state. Inspected/edited rows ready for
  import enter as `approved`; re-imports must not overwrite `approved` or
  `rejected` status. Never silently replace a manual answer link with NULL.

## Compatibility rules
1. Add JSON keys; do not rename or reinterpret existing keys.
2. Preserve IDs, `legacy_id`, FK meaning, and existing row identity.
3. Canonicalize institute/year values before matching; ambiguous matches require
   human review. Never link by question number alone when candidates conflict.
4. Import crops before rows that reference them. A failed crop upload blocks
   that row's import.
5. Table text is proposed OCR until a human compares it with the source crop.
   Never invent unreadable cells.
6. Keep migrations and backfills idempotent, auditable, and reversible.
7. Verify the actual Sapienstech readers before changing a field's meaning.

## Initial live baseline (read-only observation, 2026-10-25)
- `sources`: 2,089
- `questions`: 3,931
- `answers`: 3,042
- The retired project has 3,062 answers; the 20-row discrepancy is unresolved.
- No schema migration is authorized by this document.
