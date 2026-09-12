-- Revision system v2: the answers table joins the revision lifecycle.
-- Additive only; safe to re-run. Apply once against the pipeline database:
--   python -c "from pipeline.config import load_config; from pipeline.db import Database; \
--              Database(load_config())._execute(open('pipeline/revision/migrations.sql').read())"

ALTER TABLE public.answers
  ADD COLUMN IF NOT EXISTS revision_status text,
  ADD COLUMN IF NOT EXISTS needs_review    boolean NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS revision_notes  text,
  ADD COLUMN IF NOT EXISTS revised_at      timestamp with time zone;

-- Speed up the scan's pending lookups and the identity-based pairing joins.
CREATE INDEX IF NOT EXISTS answers_revision_pending_idx
  ON public.answers (revision_status)
  WHERE revision_status IS NULL OR revision_status = 'pending';

CREATE INDEX IF NOT EXISTS answers_identity_pair_idx
  ON public.answers (question_number, subject, topic, grade)
  WHERE question_id IS NULL;

CREATE INDEX IF NOT EXISTS questions_revision_pending_idx
  ON public.questions (revision_status)
  WHERE revision_status IS NULL OR revision_status = 'pending';
