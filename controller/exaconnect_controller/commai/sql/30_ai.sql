-- Jibsy AI agents (ADR 0019): knowledge, knowledge gaps, AI runs with their
-- sources, customer memory and browser calls. Idempotent; applied at startup.

-- Approved business content. Only approved sources are ever given to the AI.
CREATE TABLE IF NOT EXISTS knowledge_sources (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  title        text NOT NULL,
  body         text NOT NULL,
  source_url   text NOT NULL DEFAULT '',
  approved     boolean NOT NULL DEFAULT false,
  approved_by  text NOT NULL DEFAULT '',
  approved_at  timestamptz,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS knowledge_sources_customer ON knowledge_sources (customer_id, created_at DESC);

-- Sources are split into chunks for retrieval. Postgres full-text search for
-- now (a GIN index on a generated tsvector); the retrieval interface in
-- commai/ai/knowledge.py lets pgvector replace it later.
CREATE TABLE IF NOT EXISTS knowledge_chunks (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  source_id    uuid NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
  ordinal      int NOT NULL,
  heading      text NOT NULL DEFAULT '',
  text         text NOT NULL,
  tsv          tsvector GENERATED ALWAYS AS (
                 setweight(to_tsvector('english', heading), 'A') || to_tsvector('english', text)
               ) STORED,
  UNIQUE (source_id, ordinal)
);
CREATE INDEX IF NOT EXISTS knowledge_chunks_tsv ON knowledge_chunks USING gin (tsv);
CREATE INDEX IF NOT EXISTS knowledge_chunks_customer ON knowledge_chunks (customer_id);

-- Questions the AI could not answer from approved knowledge (missing or
-- contradictory), grouped by question so the business sees what to write.
CREATE TABLE IF NOT EXISTS knowledge_gaps (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  question_key     text NOT NULL,
  question         text NOT NULL,
  reason           text NOT NULL CHECK (reason IN ('missing', 'contradictory')),
  detail           text NOT NULL DEFAULT '',
  conversation_id  uuid REFERENCES conversations(id) ON DELETE SET NULL,
  times            int NOT NULL DEFAULT 1,
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
  resolved_by      text NOT NULL DEFAULT '',
  first_seen       timestamptz NOT NULL DEFAULT now(),
  last_seen        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, question_key)
);

-- Every time an AI role ran: what it answered, from which sources, what it
-- proposed and how it ended. Staff see the sources of each AI answer here.
-- One customer-agent run per inbound message, so a retried job never replies twice.
CREATE TABLE IF NOT EXISTS ai_runs (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id       uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id   uuid REFERENCES conversations(id) ON DELETE CASCADE,
  message_id        uuid,
  reply_message_id  uuid,
  role              text NOT NULL,
  task              text NOT NULL DEFAULT 'reply',
  model             text NOT NULL DEFAULT '',
  outcome           text NOT NULL CHECK (outcome IN ('replied', 'escalated', 'failed', 'proposed', 'done')),
  intent            text NOT NULL DEFAULT '',
  reason            text NOT NULL DEFAULT '',
  language          text NOT NULL DEFAULT '',
  sources           jsonb NOT NULL DEFAULT '[]',
  tool_calls        jsonb NOT NULL DEFAULT '[]',
  tokens_in         int NOT NULL DEFAULT 0,
  tokens_out        int NOT NULL DEFAULT 0,
  actor             text NOT NULL DEFAULT '',
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ai_runs_message ON ai_runs (message_id)
  WHERE role = 'customer_agent' AND message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ai_runs_conversation ON ai_runs (conversation_id, created_at);
CREATE INDEX IF NOT EXISTS ai_runs_customer ON ai_runs (customer_id, created_at DESC);

-- What the AI may remember about a contact. Only for verified identities;
-- staff can see and delete every fact.
CREATE TABLE IF NOT EXISTS contact_memory (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id       uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  fact             text NOT NULL,
  source           text NOT NULL DEFAULT 'ai' CHECK (source IN ('ai', 'staff')),
  conversation_id  uuid REFERENCES conversations(id) ON DELETE SET NULL,
  created_by       text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS contact_memory_fact ON contact_memory (contact_id, lower(fact));

-- Browser calls with the AI agent (voice stage 1). The transcript is the
-- conversation's messages on channel 'voice'.
CREATE TABLE IF NOT EXISTS ai_calls (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
  caller           text NOT NULL DEFAULT '',
  started_by       text NOT NULL DEFAULT '',
  speech           text NOT NULL DEFAULT 'browser',
  turns            int NOT NULL DEFAULT 0,
  started_at       timestamptz NOT NULL DEFAULT now(),
  ended_at         timestamptz
);
