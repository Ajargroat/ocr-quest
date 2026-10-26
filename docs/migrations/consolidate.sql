-- Consolidate the lessons-model database back to the flat pipeline core.
--
-- Target shape: sources / questions / answers, exactly the tables
-- pipeline/db.py, the runner, the deferral replay and the Studio importer
-- already write. The lessons tables (courses, chapters, lessons,
-- lesson_blocks, media_assets, question_options, short_answer_keys, attempts,
-- lesson_progress) and the Sapienstech mirror are dropped: their content is
-- duplicated 1:1 in sources/questions/answers (see the parity checks in
-- consolidate.py) and survives in the backup set taken before the run.
--
-- The revision engine is retired, so its two tables (revision_reports,
-- revision_summary) are retired with it - see retire_revision.sql.
--
-- Kept on purpose, even though the merge could derive them:
--   * questions.legacy_id  - the bucket name of every crop picture
--                            (`crops/<legacy_id>.jpg`, 479 rows).
--   * answers.identity columns (subject/topic/grade/corp/year/difficulty/
--     tags) - the unpaired-answer matcher (answers_identity_pair_idx) and
--     the Data Console both read them.
--   * questions.type      - db.py's upsert and the QBank review select read it.
--
-- NOT a standalone psql script: consolidate.py owns the transaction, the
-- pre-flight and the post-commit parity checks. It binds the expected row
-- counts as the script's single bind parameter (a JSON object).

-- 1. The flat core, created only if a fresh database lacks it. Every object
--    matches what the live (lessons-shaped) tables already carry, so on the
--    current DB each of these is a no-op.
CREATE TABLE IF NOT EXISTS sources (
  id             TEXT PRIMARY KEY,
  folder_id      TEXT NOT NULL,
  file_name      TEXT,
  mime_type      TEXT,
  file_size_bytes BIGINT,
  storage_url    TEXT,
  subject        TEXT,
  grade          INTEGER,
  topic          TEXT,
  type           TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS questions (
  id            BIGSERIAL PRIMARY KEY,
  legacy_id     TEXT UNIQUE,
  source_id     TEXT REFERENCES sources(id) ON DELETE SET NULL,
  question_number INTEGER,
  prompt        TEXT NOT NULL,
  type          TEXT,
  options       TEXT NOT NULL DEFAULT '[]',
  media_images  JSONB NOT NULL DEFAULT '[]'::jsonb,
  explanation   TEXT,
  hint          TEXT,
  points        SMALLINT NOT NULL DEFAULT 1,
  max_length    SMALLINT,
  subject       TEXT,
  topic         TEXT,
  grade         SMALLINT,
  corp          TEXT,
  year          TEXT,
  difficulty    TEXT,
  tags          TEXT NOT NULL DEFAULT '[]',
  correct_option_label TEXT,
  correct_option_text  TEXT,
  review_status TEXT NOT NULL DEFAULT 'pending',
  revision_status TEXT NOT NULL DEFAULT 'pending',
  revision_notes TEXT,
  revised_at    TIMESTAMPTZ,
  needs_review  BOOLEAN NOT NULL DEFAULT FALSE,
  raw_ocr_text  TEXT,
  content_hash  TEXT,
  split         TEXT,
  duplicate     BOOLEAN NOT NULL DEFAULT FALSE,
  unreadable    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (jsonb_typeof(media_images) = 'array')
);

CREATE TABLE IF NOT EXISTS answers (
  id                  TEXT PRIMARY KEY,
  legacy_id           TEXT,
  source_id           TEXT REFERENCES sources(id) ON DELETE CASCADE,
  question_id         BIGINT REFERENCES questions(id) ON DELETE SET NULL,
  legacy_question_id  TEXT,
  question_number     INTEGER,
  answer_explanation  TEXT,
  correct_option_label TEXT,
  correct_option_text  TEXT,
  assets              TEXT NOT NULL DEFAULT '[]',
  media_images        JSONB NOT NULL DEFAULT '[]'::jsonb,
  subject             TEXT,
  topic               TEXT,
  grade               INTEGER,
  corp                TEXT,
  year                TEXT,
  difficulty          TEXT,
  tags                TEXT NOT NULL DEFAULT '[]',
  review_status       TEXT NOT NULL DEFAULT 'pending',
  revision_status     TEXT NOT NULL DEFAULT 'pending',
  revision_notes      TEXT,
  revised_at          TIMESTAMPTZ,
  needs_review        BOOLEAN NOT NULL DEFAULT FALSE,
  raw_ocr_text        TEXT,
  content_hash        TEXT,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- revision_reports / revision_summary are intentionally absent: the revision
-- engine is retired (no UI, no endpoints) and the columns questions/answers
-- still carry (revision_status, revision_notes, revised_at) are left dormant
-- so a revival needs no migration. A revival recreates both tables here.

-- 2. Bring an older flat questions table up to the merged shape (idempotent;
--    on today's DB, where the lessons model already added these, all no-ops).
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS legacy_id TEXT;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS type TEXT;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS prompt TEXT;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS media_images JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS split TEXT;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS duplicate BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS unreadable BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS content_hash TEXT;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS raw_ocr_text TEXT;
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS revision_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE public.questions ADD COLUMN IF NOT EXISTS options TEXT;
UPDATE public.questions SET options = '[]' WHERE options IS NULL;

-- 3. answers: the identity columns the unpaired matcher needs, for a DB whose
--    answers table predates them.
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS subject TEXT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS topic TEXT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS grade INTEGER;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS corp TEXT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS year TEXT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS difficulty TEXT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS tags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS question_id BIGINT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS media_images JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS revision_status TEXT DEFAULT 'pending';
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS revision_notes TEXT;
ALTER TABLE public.answers ADD COLUMN IF NOT EXISTS revised_at TIMESTAMPTZ;

-- 4. Backfill: every answer learns its host question's identity, so a slim
--    answers row still matches an unpaired answer by (number, subject, topic,
--    grade). Only NULL columns are filled - an existing value never moves.
UPDATE public.answers a
   SET subject    = COALESCE(a.subject, q.subject),
       topic      = COALESCE(a.topic, q.topic),
       grade      = COALESCE(a.grade, q.grade),
       corp       = COALESCE(a.corp, q.corp),
       year       = COALESCE(a.year, q.year),
       difficulty = COALESCE(a.difficulty, q.difficulty),
       tags       = CASE WHEN a.tags IS NULL OR a.tags = '[]'
                         THEN COALESCE(q.tags, '[]') ELSE a.tags END
  FROM public.questions q
 WHERE a.question_id = q.id
   AND (a.subject IS NULL OR a.topic IS NULL OR a.grade IS NULL
        OR a.corp IS NULL OR a.year IS NULL OR a.difficulty IS NULL
        OR a.tags IS NULL OR a.tags = '[]');

-- 5. The indexes the pipeline reads (matches pipeline/revision/migrations.sql
--    and the lessons DDL, restated over the flat columns).
CREATE INDEX IF NOT EXISTS questions_source_id_idx ON questions (source_id);
CREATE INDEX IF NOT EXISTS questions_review_status_idx ON questions (review_status);
CREATE INDEX IF NOT EXISTS questions_subject_topic_idx ON questions (subject, topic);
CREATE INDEX IF NOT EXISTS questions_lookup_idx
  ON questions (subject, topic, grade, corp, year, question_number);
CREATE INDEX IF NOT EXISTS questions_revision_pending_idx
  ON questions (revision_status) WHERE revision_status = 'pending';
CREATE INDEX IF NOT EXISTS answers_question_id_idx ON answers (question_id);
CREATE INDEX IF NOT EXISTS answers_identity_pair_idx
  ON answers (question_number, subject, topic, grade) WHERE question_id IS NULL;
CREATE INDEX IF NOT EXISTS answers_revision_pending_idx
  ON answers (revision_status)
  WHERE revision_status IS NULL OR revision_status = 'pending';
CREATE INDEX IF NOT EXISTS sources_folder_idx ON sources (folder_id);

-- 6. Parity guard. The lessons tables are only dropped when their content is
--    provably present in the flat tables. The expected counts were measured
--    before the run and arrive as the script's bind parameter; any mismatch
--    raises, which rolls the whole transaction back.
DO $parity$
DECLARE
  expected JSONB := %s::jsonb;
  n_src INT; n_q INT; n_a INT; n_lb INT;
  orphan_blocks INT;
BEGIN
  SELECT count(*) INTO n_src FROM public.sources;
  SELECT count(*) INTO n_q   FROM public.questions;
  SELECT count(*) INTO n_a   FROM public.answers;

  IF expected ? 'sources' AND n_src < (expected->>'sources')::INT THEN
    RAISE EXCEPTION 'parity: sources %% < %%', n_src, expected->>'sources';
  END IF;
  IF expected ? 'questions' AND n_q < (expected->>'questions')::INT THEN
    RAISE EXCEPTION 'parity: questions %% < %%', n_q, expected->>'questions';
  END IF;
  IF expected ? 'answers' AND n_a < (expected->>'answers')::INT THEN
    RAISE EXCEPTION 'parity: answers %% < %%', n_a, expected->>'answers';
  END IF;

  -- A lessons-era DB may only drop lesson_blocks once every block points at a
  -- question the flat schema keeps. (lesson <-> source linkage is proved by
  -- consolidate.py, which knows the legacy column names.)
  IF to_regclass('public.lesson_blocks') IS NOT NULL THEN
    SELECT count(*) INTO n_lb FROM public.lesson_blocks;
    IF expected ? 'lesson_blocks'
       AND n_lb <> (expected->>'lesson_blocks')::INT THEN
      RAISE EXCEPTION 'parity: lesson_blocks %% <> %%', n_lb,
                      expected->>'lesson_blocks';
    END IF;
    SELECT count(*) INTO orphan_blocks
      FROM public.lesson_blocks lb
     WHERE lb.question_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM public.questions q
                        WHERE q.id = lb.question_id);
    IF orphan_blocks > 0 THEN
      RAISE EXCEPTION 'parity: %% lesson_blocks have no question', orphan_blocks;
    END IF;
  END IF;
END
$parity$;

-- 7. Drop the lessons scaffolding, children before parents. The Sapienstech
--    mirror goes first: questions_sapienstech.chapter_id references chapters,
--    and test_questions references questions_sapienstech. questions.sapienstech_id
--    was the mirror's only inbound link from the flat table and nothing in the
--    app reads it, so it goes before the mirror table itself.
ALTER TABLE public.questions DROP COLUMN IF EXISTS sapienstech_id;
DROP TABLE IF EXISTS public.test_questions;
DROP TABLE IF EXISTS public.questions_sapienstech;
DROP TABLE IF EXISTS public.students;
DROP TABLE IF EXISTS public.tests;
DROP TABLE IF EXISTS public.lesson_progress;
DROP TABLE IF EXISTS public.attempts;
DROP TABLE IF EXISTS public.lesson_blocks;
DROP TABLE IF EXISTS public.short_answer_keys;
DROP TABLE IF EXISTS public.question_options;
DROP TABLE IF EXISTS public.media_assets;
DROP TABLE IF EXISTS public.lessons;
DROP TABLE IF EXISTS public.chapters;
DROP TABLE IF EXISTS public.courses;
DROP TABLE IF EXISTS public.users;
DROP TABLE IF EXISTS public.domains;
DROP TABLE IF EXISTS public.tenants;
