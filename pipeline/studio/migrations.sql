-- Lessons model (root PDF: Database Design - Lessons with Images, MCQs and
-- Short-Answer Questions) + the re-shaped OCR model.
-- Idempotent: every object is guarded, so re-running is safe. NEVER applied
-- automatically by the app - the user runs it explicitly:
--   python -c "from pipeline.config import load_config; from pipeline.db import Database; \
--              Database(load_config())._execute(open('pipeline/studio/migrations.sql').read())"
-- If that one-liner reports a syntax error on this driver, apply with
--   psql "$TARGET_DSN" -f pipeline/studio/migrations.sql
-- Order inside the file matters: FK targets -> lessons -> OCR side -> indexes.

-- 0. FK targets (Laravel/Sapienstech side). Column subsets only: if a real
--    Laravel dump is restored first, every statement below is a no-op.
CREATE TABLE IF NOT EXISTS tenants (
  id BIGSERIAL PRIMARY KEY, name TEXT, slug TEXT, status TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS domains (
  id BIGSERIAL PRIMARY KEY, tenant_id BIGINT REFERENCES tenants(id),
  domain TEXT, is_primary INTEGER NOT NULL DEFAULT 0, verified_at TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY, tenant_id BIGINT REFERENCES tenants(id),
  domain_id BIGINT REFERENCES domains(id), name TEXT, email TEXT,
  password TEXT, role TEXT, avatar TEXT, preferences JSONB,
  email_verified_at TIMESTAMP, remember_token TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS tests (
  id BIGSERIAL PRIMARY KEY, tenant_id BIGINT REFERENCES tenants(id),
  test_title TEXT, lesson TEXT, description TEXT, exam_type TEXT,
  time_limit_minutes INTEGER, total_marks NUMERIC, question_count SMALLINT,
  created_by_user_id BIGINT REFERENCES users(id),
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now());

-- 1. Enums (PostgreSQL has no CREATE TYPE IF NOT EXISTS).
DO $$ BEGIN
  CREATE TYPE block_type AS ENUM
    ('heading','paragraph','list','image','image_group','equation','callout','question');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TYPE image_layout AS ENUM ('start','end','wide','duo');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
  CREATE TYPE question_type AS ENUM ('mcq_single','mcq_multi','short_answer');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 2. courses + chapters. NOT in the PDF's DDL block; derived from its own
--    entity table ("courses: a subject or grade", "chapters: groups lessons")
--    and from the lessons DDL's shape (BIGSERIAL id, slug UNIQUE per parent,
--    position, timestamps, locale).
CREATE TABLE IF NOT EXISTS courses (
  id         BIGSERIAL PRIMARY KEY,
  slug       TEXT NOT NULL UNIQUE,          -- 'zist-10'
  title      TEXT NOT NULL,
  subject    TEXT,                          -- canonical vocabulary (seed.json)
  grade      SMALLINT,
  locale     TEXT NOT NULL DEFAULT 'fa',
  position   INT NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chapters (
  id         BIGSERIAL PRIMARY KEY,
  course_id  BIGINT NOT NULL REFERENCES courses(id) ON DELETE CASCADE,
  slug       TEXT NOT NULL,
  title      TEXT NOT NULL,
  position   INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (course_id, slug)
);

-- 2b. Sapienstech read-side mirror: students + the tests bridge. Placed after
--     chapters because questions_sapienstech.chapter_id references it; topic_id
--     points at Laravel's topics, which this migration does not mirror.
CREATE TABLE IF NOT EXISTS students (
  id BIGSERIAL PRIMARY KEY, tenant_id BIGINT REFERENCES tenants(id),
  domain_id BIGINT REFERENCES domains(id), name TEXT, email TEXT,
  password TEXT, grade TEXT, gender TEXT, major TEXT, avatar TEXT,
  preferences JSONB, email_verified_at TIMESTAMP, remember_token TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS questions_sapienstech (
  id BIGSERIAL PRIMARY KEY, tenant_id BIGINT REFERENCES tenants(id),
  supabase_id TEXT, chapter_id BIGINT REFERENCES chapters(id),
  topic_id BIGINT, question_text TEXT, question_image_path TEXT,
  question_image_bbox TEXT, solution_text TEXT, solution_image_path TEXT,
  question_number_in_book TEXT, difficulty TEXT, question_type TEXT,
  created_at TIMESTAMP NOT NULL DEFAULT now(),
  updated_at TIMESTAMP NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS test_questions (
  id BIGSERIAL PRIMARY KEY, tenant_id BIGINT REFERENCES tenants(id),
  test_id BIGINT NOT NULL REFERENCES tests(id),
  question_id BIGINT NOT NULL REFERENCES questions_sapienstech(id),
  position INTEGER NOT NULL, points NUMERIC);

-- 3. lessons: the PDF's DDL, verbatim.
CREATE TABLE IF NOT EXISTS lessons (
  id          BIGSERIAL PRIMARY KEY,
  chapter_id  BIGINT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
  slug        TEXT NOT NULL,
  title       TEXT NOT NULL,
  summary     TEXT,
  reading_min SMALLINT,
  position    INT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'draft',   -- draft | published
  locale      TEXT NOT NULL DEFAULT 'fa',
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (chapter_id, slug)
);

-- 4. media_assets: the PDF's DDL, verbatim.
CREATE TABLE IF NOT EXISTS media_assets (
  id          BIGSERIAL PRIMARY KEY,
  url         TEXT NOT NULL,
  alt_text    TEXT NOT NULL,
  caption     TEXT,
  width       INT,
  height      INT,
  mime_type   TEXT,
  uploaded_by BIGINT REFERENCES users(id)
);

-- 5. sources: the folder-tree file the DB points at (Q8). id stays the scanner's
--    sha1 of the relative path; folder_id is the folder that file sits in.
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

-- 6. questions = the PDF's lessons questions (BIGSERIAL id, type/prompt/...)
--    extended with the OCR columns. legacy_id keeps the old '<sha1>-q<N>' id
--    (C2 "keep ids"); content_hash is NO LONGER unique (it was a false-unique
--    on duplicated questions); options is NOT NULL; the continuation/duplicate/
--    unreadable flags ride here so QBank can chip them.
CREATE TABLE IF NOT EXISTS questions (
  id            BIGSERIAL PRIMARY KEY,
  -- PDF columns
  type          question_type NOT NULL,
  prompt        TEXT NOT NULL,
  explanation   TEXT,
  hint          TEXT,
  points        SMALLINT NOT NULL DEFAULT 1,
  max_length    SMALLINT,
  -- OCR extension
  legacy_id     TEXT UNIQUE,
  sapienstech_id BIGINT REFERENCES questions_sapienstech(id),
  source_id     TEXT REFERENCES sources(id) ON DELETE SET NULL,
  question_number INTEGER,
  options       TEXT NOT NULL DEFAULT '[]',
  media_images  JSONB NOT NULL DEFAULT '[]'::jsonb,
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
  split         TEXT,                        -- null | tail | head | both
  duplicate     BOOLEAN NOT NULL DEFAULT FALSE,
  unreadable    BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  CHECK (review_status IN ('pending','approved','rejected')),
  CHECK (split IS NULL OR split IN ('tail','head','both')),
  CHECK (jsonb_typeof(media_images) = 'array')
);

CREATE TABLE IF NOT EXISTS question_options (
  id          BIGSERIAL PRIMARY KEY,
  question_id BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  label       TEXT NOT NULL,
  is_correct  BOOLEAN NOT NULL DEFAULT FALSE,
  position    SMALLINT NOT NULL
);
CREATE INDEX IF NOT EXISTS question_options_q_idx ON question_options (question_id, position);

CREATE TABLE IF NOT EXISTS short_answer_keys (
  question_id  BIGINT PRIMARY KEY REFERENCES questions(id) ON DELETE CASCADE,
  model_answer TEXT,
  keywords     TEXT[]
);

CREATE TABLE IF NOT EXISTS lesson_blocks (
  id          BIGSERIAL PRIMARY KEY,
  lesson_id   BIGINT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
  position    INT NOT NULL,
  type        block_type NOT NULL,
  body        TEXT,
  layout      image_layout,
  media_id    BIGINT REFERENCES media_assets(id),
  question_id BIGINT REFERENCES questions(id),
  extra       JSONB NOT NULL DEFAULT '{}',
  UNIQUE (lesson_id, position),
  CHECK (type <> 'image' OR (media_id IS NOT NULL AND layout IS NOT NULL)),
  CHECK (type <> 'question' OR question_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS attempts (
  id           BIGSERIAL PRIMARY KEY,
  user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  question_id  BIGINT NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
  option_ids   BIGINT[],
  answer_text  TEXT,
  is_correct   BOOLEAN,
  score        NUMERIC(5,2),
  graded_by    TEXT,
  submitted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS lesson_progress (
  user_id      BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  lesson_id    BIGINT NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
  percent      SMALLINT NOT NULL DEFAULT 0,
  completed_at TIMESTAMPTZ,
  PRIMARY KEY (user_id, lesson_id)
);

-- 7. OCR side, re-shaped: answers / revision bookkeeping.
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

CREATE TABLE IF NOT EXISTS revision_reports (
  id          TEXT PRIMARY KEY,
  run_id      TEXT NOT NULL,
  entity      TEXT NOT NULL,
  entity_id   TEXT NOT NULL,
  field       TEXT,
  issue       TEXT,
  severity    TEXT NOT NULL,
  source      TEXT NOT NULL,
  before_value TEXT,
  after_value TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS revision_summary (
  run_id      TEXT,
  total_flags BIGINT,
  errors      BIGINT,
  ai_flags    BIGINT,
  code_fixes  BIGINT,
  last_at     TIMESTAMPTZ
);

-- 8. Indexes (the OCR lookup patterns the live DB has today, minus the UNIQUE
--    on content_hash and minus the pg_trgm index - the new DB may not carry the
--    extension; the console's ILIKE search works without it).
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
CREATE INDEX IF NOT EXISTS lesson_blocks_lesson_idx ON lesson_blocks (lesson_id, position);
CREATE INDEX IF NOT EXISTS attempts_user_question_idx
  ON attempts (user_id, question_id, submitted_at DESC);
