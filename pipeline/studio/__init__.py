"""OCR Studio — a parallel subsystem for OCR-ing *any* exam file.

Unlike the rigid `konkour-ocr/{subject}/{grade}/{topic}/{question|answer}/`
tree the main pipeline scans, the studio takes one file at a time, works out
what it actually is (Stage 0 profile), extracts a faithful bulk (Stage 1),
enriches and repairs it against a controlled tag list (Stage 2), fixes the
crop geometry (Stage 3), lets a human review it (Stage 4) and bulk-imports it
(Stage 5).

The intermediate bulk lives as PLAIN FILES in a workspace directory
(``workspace/<job>/``) so every stage and every AI can re-inspect and cite it
without selecting rows from a database. The DB is touched only at final
import — and even then through a pluggable importer, so the target can be
Supabase, a Laravel DB or an export file.

This package is deliberately standalone: it reuses shared infra
(``gemini_router``, ``faults``, ``vision``, ``storage``, ``utils``) but never
changes the behaviour of the existing pipeline.
"""
