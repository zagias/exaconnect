-- Languages, AI quality and governance, and the remaining inbox features
-- (ADR 0026). Idempotent; applied at start-up after the earlier files.

-- ---- languages -----------------------------------------------------------------------

-- Each person's interface language. Offered only when the language is switched
-- on in the go-live registry (kind 'language'); en-GB is the source and always on.
CREATE TABLE IF NOT EXISTS commai_user_locale (
  user_id     uuid PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  locale      text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- A reviewer's sign-off of one catalogue version. A catalogue whose content
-- hash has no sign-off is shown as machine-drafted.
CREATE TABLE IF NOT EXISTS commai_catalogue_reviews (
  locale          text NOT NULL,
  catalogue_hash  text NOT NULL,
  reviewed_by     text NOT NULL,
  note            text NOT NULL DEFAULT '',
  reviewed_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (locale, catalogue_hash)
);

-- ---- notifications (mentions, reminders, staff chat, quality flags) ------------------

CREATE TABLE IF NOT EXISTS commai_notifications (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id          uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind             text NOT NULL CHECK (kind IN ('mention', 'reminder', 'staff_chat', 'quality_flag')),
  title            text NOT NULL,
  body             text NOT NULL DEFAULT '',
  conversation_id  uuid REFERENCES conversations(id) ON DELETE CASCADE,
  ref              text NOT NULL DEFAULT '',
  read_at          timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_notifications_user ON commai_notifications (user_id, customer_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS commai_notifications_ref ON commai_notifications (user_id, kind, ref) WHERE ref <> '';

-- ---- files on private notes ----------------------------------------------------------
-- Their own table, never channel_files: no widget, channel or customer path reads it.
CREATE TABLE IF NOT EXISTS note_files (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  note_id          uuid NOT NULL REFERENCES commai_notes(id) ON DELETE CASCADE,
  uploaded_by      text NOT NULL,
  name             text NOT NULL,
  content_type     text NOT NULL,
  size             int NOT NULL,
  data             bytea NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS note_files_note ON note_files (note_id);

-- ---- saved inbox views ---------------------------------------------------------------
CREATE TABLE IF NOT EXISTS commai_saved_views (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  owner_user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name           text NOT NULL,
  filters        jsonb NOT NULL DEFAULT '{}',
  shared         boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, owner_user_id, name)
);

-- ---- satisfaction surveys ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS csat_surveys (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  contact_id       uuid REFERENCES contacts(id) ON DELETE SET NULL,
  channel          text NOT NULL,
  resolved_event   text NOT NULL,
  status           text NOT NULL CHECK (status IN ('sent', 'blocked', 'skipped', 'answered')),
  reason           text NOT NULL DEFAULT '',
  token_hash       text UNIQUE,
  message_id       uuid,
  handled_by       text NOT NULL DEFAULT '',
  rating           int CHECK (rating BETWEEN 1 AND 5),
  comment          text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now(),
  answered_at      timestamptz,
  UNIQUE (conversation_id, resolved_event)
);
CREATE INDEX IF NOT EXISTS csat_surveys_customer ON csat_surveys (customer_id, created_at);

-- ---- staff chat (internal only; no customer path reads these tables) ------------------
CREATE TABLE IF NOT EXISTS staff_chats (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('direct', 'group')),
  name         text NOT NULL DEFAULT '',
  direct_key   text,
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  last_message_at timestamptz,
  UNIQUE (customer_id, direct_key)
);
CREATE TABLE IF NOT EXISTS staff_chat_members (
  chat_id   uuid NOT NULL REFERENCES staff_chats(id) ON DELETE CASCADE,
  user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  last_read_at timestamptz,
  PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS staff_chat_messages (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  chat_id         uuid NOT NULL REFERENCES staff_chats(id) ON DELETE CASCADE,
  author_user_id  uuid REFERENCES users(id) ON DELETE SET NULL,
  author          text NOT NULL,
  body            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS staff_chat_messages_chat ON staff_chat_messages (chat_id, created_at);

-- ---- what the AI read from attachments and voice notes -------------------------------
CREATE TABLE IF NOT EXISTS attachment_readings (
  file_id      uuid PRIMARY KEY REFERENCES channel_files(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('text', 'pdf', 'image', 'voice', 'other')),
  status       text NOT NULL CHECK (status IN ('read', 'refused', 'failed')),
  text         text NOT NULL DEFAULT '',
  reason       text NOT NULL DEFAULT '',
  reader       text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- ---- quality review ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quality_criteria (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  text         text NOT NULL,
  enabled      boolean NOT NULL DEFAULT true,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS quality_criteria_customer ON quality_criteria (customer_id, created_at);

CREATE TABLE IF NOT EXISTS quality_reviews (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'done', 'failed')),
  sample_size   int NOT NULL,
  days          int NOT NULL,
  criteria      jsonb NOT NULL DEFAULT '[]',
  summary       jsonb NOT NULL DEFAULT '{}',
  error         text NOT NULL DEFAULT '',
  requested_by  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS quality_reviews_customer ON quality_reviews (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS quality_results (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_id        uuid NOT NULL REFERENCES quality_reviews(id) ON DELETE CASCADE,
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  handled_by       text NOT NULL CHECK (handled_by IN ('ai', 'human')),
  score            numeric NOT NULL,
  results          jsonb NOT NULL DEFAULT '[]',
  flagged          boolean NOT NULL DEFAULT false,
  flag_status      text NOT NULL DEFAULT 'none' CHECK (flag_status IN ('none', 'open', 'reviewed')),
  reviewed_by      text NOT NULL DEFAULT '',
  review_note      text NOT NULL DEFAULT '',
  reviewed_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (review_id, conversation_id)
);

-- ---- knowledge gaps turned into draft articles ---------------------------------------
ALTER TABLE knowledge_gaps ADD COLUMN IF NOT EXISTS draft_source_id uuid REFERENCES knowledge_sources(id) ON DELETE SET NULL;
ALTER TABLE knowledge_sources ADD COLUMN IF NOT EXISTS drafted_from_gaps boolean NOT NULL DEFAULT false;

-- ---- follow-up reminders from promises made in a conversation -------------------------
CREATE TABLE IF NOT EXISTS followup_reminders (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_id       uuid NOT NULL,
  promise          text NOT NULL,
  due_at           timestamptz NOT NULL,
  owner_user_id    uuid REFERENCES users(id) ON DELETE SET NULL,
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dismissed')),
  done_by          text NOT NULL DEFAULT '',
  done_at          timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (message_id, promise)
);
CREATE INDEX IF NOT EXISTS followup_reminders_customer ON followup_reminders (customer_id, status, due_at);

-- ---- governance: evaluation suites, candidates, results, action limits ----------------
-- customer_id NULL is the global suite, run for every change.
CREATE TABLE IF NOT EXISTS ai_eval_cases (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid REFERENCES customers(id) ON DELETE CASCADE,
  question     text NOT NULL,
  expected     text NOT NULL DEFAULT '',
  forbidden    text NOT NULL DEFAULT '',
  enabled      boolean NOT NULL DEFAULT true,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- A change waiting to go live: a model (global) or a business's agent instructions.
CREATE TABLE IF NOT EXISTS ai_candidates (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid REFERENCES customers(id) ON DELETE CASCADE,
  kind         text NOT NULL CHECK (kind IN ('model', 'instructions')),
  change       jsonb NOT NULL,
  previous     jsonb NOT NULL DEFAULT '{}',
  status       text NOT NULL DEFAULT 'testing'
               CHECK (status IN ('testing', 'passed', 'failed', 'promoted', 'withdrawn')),
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  decided_by   text NOT NULL DEFAULT '',
  decided_at   timestamptz
);
CREATE INDEX IF NOT EXISTS ai_candidates_customer ON ai_candidates (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ai_eval_runs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  candidate_id  uuid NOT NULL REFERENCES ai_candidates(id) ON DELETE CASCADE,
  status        text NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'failed')),
  total         int NOT NULL DEFAULT 0,
  passed        int NOT NULL DEFAULT 0,
  failed        int NOT NULL DEFAULT 0,
  results       jsonb NOT NULL DEFAULT '[]',
  error         text NOT NULL DEFAULT '',
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS ai_eval_runs_candidate ON ai_eval_runs (candidate_id, started_at DESC);

-- The model in use. EXA_LLM_MODEL naming another model creates a candidate;
-- this row changes only when an ExaCarib admin promotes one that passed.
CREATE TABLE IF NOT EXISTS ai_live_model (
  id           int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  model        text NOT NULL,
  promoted_by  text NOT NULL,
  promoted_at  timestamptz NOT NULL DEFAULT now()
);

-- How many actions each role may propose per day (UTC). No row: no limit.
CREATE TABLE IF NOT EXISTS ai_action_limits (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  role         text NOT NULL,
  daily_limit  int NOT NULL CHECK (daily_limit >= 0),
  PRIMARY KEY (customer_id, role)
);
