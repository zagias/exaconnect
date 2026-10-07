-- Jibsy core (ADR 0016). Applied after schema.sql, idempotently, at startup.
-- Every record carries customer_id: the business that owns it.

-- API keys may be limited to scopes. NULL keeps the old meaning: the key can do
-- whatever its owner can. See commai/access.py for the scope names.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS scopes text[];

-- Durable work queue (the Postgres outbox). A job with a dedupe key is
-- enqueued at most once, so a retried request never creates a second job.
CREATE TABLE IF NOT EXISTS jobs (
  id            bigserial PRIMARY KEY,
  kind          text NOT NULL,
  customer_id   uuid,
  payload       jsonb NOT NULL DEFAULT '{}',
  dedupe_key    text UNIQUE,
  status        text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'dead')),
  attempts      int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 6,
  run_after     timestamptz NOT NULL DEFAULT now(),
  locked_until  timestamptz,
  last_error    text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_ready ON jobs (run_after) WHERE status IN ('queued', 'running');

-- Idempotency keys for API writes: the first response is stored and replayed.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  principal     text NOT NULL,
  key           text NOT NULL,
  method        text NOT NULL,
  path          text NOT NULL,
  body_hash     text NOT NULL,
  status_code   int,
  response      bytea,
  content_type  text NOT NULL DEFAULT 'application/json',
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (principal, key)
);

-- Recorded events: the source for webhooks, live updates and reports.
CREATE TABLE IF NOT EXISTS commai_events (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  seq          bigserial UNIQUE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  type         text NOT NULL,
  subject      text NOT NULL DEFAULT '',
  data         jsonb NOT NULL DEFAULT '{}',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_events_customer ON commai_events (customer_id, seq);
CREATE INDEX IF NOT EXISTS commai_events_type ON commai_events (customer_id, type, at);

-- Signed webhooks.
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  url          text NOT NULL,
  description  text NOT NULL DEFAULT '',
  events       text[] NOT NULL DEFAULT '{*}',
  secret       text NOT NULL,
  active       boolean NOT NULL DEFAULT true,
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id             bigserial PRIMARY KEY,
  endpoint_id    uuid NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
  customer_id    uuid NOT NULL,
  event_id       uuid NOT NULL,
  event_type     text NOT NULL,
  status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'delivered', 'failed')),
  attempts       int NOT NULL DEFAULT 0,
  response_code  int,
  last_error     text NOT NULL DEFAULT '',
  created_at     timestamptz NOT NULL DEFAULT now(),
  delivered_at   timestamptz,
  UNIQUE (endpoint_id, event_id)
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_endpoint ON webhook_deliveries (endpoint_id, id DESC);

-- Per-business Jibsy settings. `config` holds module settings (hours, widget,
-- AI profile...) that each module documents where it reads them.
CREATE TABLE IF NOT EXISTS commai_settings (
  customer_id            uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  mode                   text NOT NULL DEFAULT 'human_first' CHECK (mode IN ('ai_first', 'human_first', 'human_only')),
  timezone               text NOT NULL DEFAULT 'America/Port_of_Spain',
  first_reply_minutes    jsonb NOT NULL DEFAULT '{"urgent": 5, "high": 15, "normal": 60, "low": 240}',
  resolve_hours          jsonb NOT NULL DEFAULT '{"urgent": 4, "high": 8, "normal": 24, "low": 72}',
  config                 jsonb NOT NULL DEFAULT '{}',
  updated_at             timestamptz NOT NULL DEFAULT now()
);

-- Teams and seats. A user with seat 'internal' reads conversations and writes
-- private notes but never replies to customers.
CREATE TABLE IF NOT EXISTS commai_teams (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name         text NOT NULL,
  skills       text[] NOT NULL DEFAULT '{}',
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name)
);
CREATE TABLE IF NOT EXISTS commai_members (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  seat         text NOT NULL DEFAULT 'agent' CHECK (seat IN ('agent', 'internal')),
  skills       text[] NOT NULL DEFAULT '{}',
  languages    text[] NOT NULL DEFAULT '{en}',
  available    boolean NOT NULL DEFAULT true,
  PRIMARY KEY (customer_id, user_id)
);
CREATE TABLE IF NOT EXISTS commai_team_members (
  team_id  uuid NOT NULL REFERENCES commai_teams(id) ON DELETE CASCADE,
  user_id  uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (team_id, user_id)
);

-- The business's own customers ("contacts") and their channel identities.
CREATE TABLE IF NOT EXISTS contacts (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name          text NOT NULL DEFAULT '',
  email         text NOT NULL DEFAULT '',
  phone         text NOT NULL DEFAULT '',
  language      text NOT NULL DEFAULT '',
  external_ref  text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS contacts_customer ON contacts (customer_id, created_at DESC);
CREATE TABLE IF NOT EXISTS contact_identities (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id   uuid NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
  channel      text NOT NULL,
  address      text NOT NULL,
  verified     boolean NOT NULL DEFAULT false,
  opted_out    boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, channel, address)
);

CREATE TABLE IF NOT EXISTS conversations (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id        uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  contact_id         uuid REFERENCES contacts(id) ON DELETE SET NULL,
  identity_id        uuid REFERENCES contact_identities(id) ON DELETE SET NULL,
  channel            text NOT NULL,
  subject            text NOT NULL DEFAULT '',
  state              text NOT NULL DEFAULT 'open'
                     CHECK (state IN ('open', 'awaiting_customer', 'awaiting_internal', 'snoozed', 'resolved', 'reopened')),
  priority           text NOT NULL DEFAULT 'normal' CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  team_id            uuid REFERENCES commai_teams(id) ON DELETE SET NULL,
  queue              text NOT NULL DEFAULT '',
  assignee_id        uuid REFERENCES users(id) ON DELETE SET NULL,
  handler            text NOT NULL DEFAULT 'none' CHECK (handler IN ('none', 'ai', 'human')),
  handler_user_id    uuid REFERENCES users(id) ON DELETE SET NULL,
  language           text NOT NULL DEFAULT '',
  tags               text[] NOT NULL DEFAULT '{}',
  snoozed_until      timestamptz,
  first_reply_due    timestamptz,
  resolve_due        timestamptz,
  first_reply_at     timestamptz,
  resolved_at        timestamptz,
  resolved_by        text NOT NULL DEFAULT '',
  last_inbound_at    timestamptz,
  last_message_at    timestamptz,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS conversations_inbox ON conversations (customer_id, state, last_message_at DESC);
CREATE INDEX IF NOT EXISTS conversations_identity ON conversations (identity_id, created_at DESC);

-- Messages the customer sees. Private notes live in commai_notes, never here.
CREATE TABLE IF NOT EXISTS messages (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id        uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id    uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  direction          text NOT NULL CHECK (direction IN ('in', 'out')),
  author_kind        text NOT NULL CHECK (author_kind IN ('contact', 'user', 'ai', 'system', 'workflow')),
  author             text NOT NULL DEFAULT '',
  body               text NOT NULL DEFAULT '',
  original_body      text NOT NULL DEFAULT '',
  original_language  text NOT NULL DEFAULT '',
  template           text NOT NULL DEFAULT '',
  attachments        jsonb NOT NULL DEFAULT '[]',
  status             text NOT NULL DEFAULT 'received'
                     CHECK (status IN ('received', 'queued', 'sent', 'delivered', 'read', 'failed', 'blocked')),
  external_id        text,
  provider_ref       text NOT NULL DEFAULT '',
  error              text NOT NULL DEFAULT '',
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_conversation ON messages (conversation_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS messages_external ON messages (customer_id, external_id) WHERE external_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS messages_provider_ref ON messages (provider_ref) WHERE provider_ref <> '';

CREATE TABLE IF NOT EXISTS commai_notes (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  author           text NOT NULL,
  body             text NOT NULL,
  mentions         text[] NOT NULL DEFAULT '{}',
  attachments      jsonb NOT NULL DEFAULT '[]',
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_notes_conversation ON commai_notes (conversation_id, created_at);

-- Every change of state, owner, handler, team, priority or tags.
CREATE TABLE IF NOT EXISTS conversation_log (
  id               bigserial PRIMARY KEY,
  customer_id      uuid NOT NULL,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  at               timestamptz NOT NULL DEFAULT now(),
  actor            text NOT NULL,
  kind             text NOT NULL,
  from_value       text NOT NULL DEFAULT '',
  to_value         text NOT NULL DEFAULT '',
  reason           text NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS conversation_log_conv ON conversation_log (conversation_id, id);

-- What the AI hands a person when it escalates.
CREATE TABLE IF NOT EXISTS handovers (
  id               bigserial PRIMARY KEY,
  customer_id      uuid NOT NULL,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  reason           text NOT NULL,
  packet           jsonb NOT NULL DEFAULT '{}',
  at               timestamptz NOT NULL DEFAULT now()
);

-- Routing rules: first match wins; anything unmatched goes to the default team.
CREATE TABLE IF NOT EXISTS commai_routing_rules (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  position     int NOT NULL DEFAULT 100,
  name         text NOT NULL,
  match        jsonb NOT NULL DEFAULT '{}',   -- {"channel": "...", "keywords": [...], "language": "...", "intent": "..."}
  team_id      uuid REFERENCES commai_teams(id) ON DELETE CASCADE,
  queue        text NOT NULL DEFAULT '',
  priority     text CHECK (priority IN ('low', 'normal', 'high', 'urgent')),
  enabled      boolean NOT NULL DEFAULT true,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Integrations: one connection per business per app (connectors/).
CREATE TABLE IF NOT EXISTS integration_connections (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app              text NOT NULL,
  status           text NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'authorised', 'testing', 'live', 'paused', 'broken')),
  settings         jsonb NOT NULL DEFAULT '{}',
  secret_ref       text NOT NULL DEFAULT '',
  allowed_actions  text[] NOT NULL DEFAULT '{}',
  mapping          jsonb NOT NULL DEFAULT '{}',
  test_mode        boolean NOT NULL DEFAULT true,
  last_success_at  timestamptz,
  last_failure_at  timestamptz,
  last_error       text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, app)
);

-- Which tools each AI role may use, per business. Not listed means refused.
CREATE TABLE IF NOT EXISTS commai_role_tools (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  role         text NOT NULL CHECK (role IN ('customer_agent', 'copilot', 'platform_assistant', 'workflow')),
  tool         text NOT NULL,
  PRIMARY KEY (customer_id, role, tool)
);

-- Actions against business systems: propose, check, approve, execute once, confirm.
CREATE TABLE IF NOT EXISTS action_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid REFERENCES conversations(id) ON DELETE SET NULL,
  role             text NOT NULL,
  app              text NOT NULL,
  action           text NOT NULL,
  inputs           jsonb NOT NULL DEFAULT '{}',
  idempotency_key  text NOT NULL,
  sensitive        boolean NOT NULL DEFAULT false,
  status           text NOT NULL DEFAULT 'proposed'
                   CHECK (status IN ('proposed', 'awaiting_approval', 'approved', 'executing', 'succeeded', 'failed', 'rejected')),
  check_errors     jsonb NOT NULL DEFAULT '[]',
  result           jsonb NOT NULL DEFAULT '{}',
  error            text NOT NULL DEFAULT '',
  on_success       jsonb NOT NULL DEFAULT '{}',
  proposed_by      text NOT NULL,
  approved_by      text NOT NULL DEFAULT '',
  test             boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz,
  UNIQUE (customer_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS action_runs_customer ON action_runs (customer_id, created_at DESC);

-- The simulated calendar and CRM used in test mode and the lab (connectors/simulated.py).
CREATE TABLE IF NOT EXISTS sim_records (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL,
  app              text NOT NULL,
  kind             text NOT NULL,
  idempotency_key  text NOT NULL,
  data             jsonb NOT NULL DEFAULT '{}',
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, app, idempotency_key)
);

-- Metered usage per business (messages, AI replies, calls...), with budgets
-- and hard limits. Prices come later from Dudley's price list.
CREATE TABLE IF NOT EXISTS usage_records (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  meter        text NOT NULL,
  quantity     numeric NOT NULL,
  ref          text NOT NULL DEFAULT '',
  detail       jsonb NOT NULL DEFAULT '{}',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS usage_records_customer ON usage_records (customer_id, meter, at);
CREATE UNIQUE INDEX IF NOT EXISTS usage_records_ref ON usage_records (customer_id, meter, ref) WHERE ref <> '';
CREATE TABLE IF NOT EXISTS usage_limits (
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  meter           text NOT NULL,
  monthly_alert   numeric,
  monthly_hard    numeric,
  PRIMARY KEY (customer_id, meter)
);
