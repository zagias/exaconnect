-- Jibsy automation (ADR 0020): integration setup, workflows, onboarding,
-- the platform assistant, support cases and outcome reports. Idempotent.

-- Integration setup as a product. The token itself is never in this table:
-- secret_ref points at commai_secrets, which holds it encrypted.
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS auth_status text NOT NULL DEFAULT 'none';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS auth_method text NOT NULL DEFAULT '';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS token_expires_at timestamptz;
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS granted_scopes text[] NOT NULL DEFAULT '{}';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS mapping_open text[] NOT NULL DEFAULT '{}';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS last_test_at timestamptz;
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS last_test_result jsonb NOT NULL DEFAULT '{}';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS last_cause text NOT NULL DEFAULT '';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS approved_by text NOT NULL DEFAULT '';
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS approved_at timestamptz;
ALTER TABLE integration_connections ADD COLUMN IF NOT EXISTS paused_from text NOT NULL DEFAULT '';

-- Encrypted secrets (Fernet, key from EXA_SECRETS_KEY). Never returned by the API.
CREATE TABLE IF NOT EXISTS commai_secrets (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  purpose      text NOT NULL,
  ciphertext   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- OAuth 2.0 authorisation-code flow: one row per sign-in attempt. Only the
-- hash of the state value is stored.
CREATE TABLE IF NOT EXISTS commai_oauth_states (
  state_hash   text PRIMARY KEY,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app          text NOT NULL,
  actor        text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  used_at      timestamptz
);

-- Objects a connector created, by idempotency key, so a retry never creates twice.
CREATE TABLE IF NOT EXISTS commai_connector_objects (
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app              text NOT NULL,
  idempotency_key  text NOT NULL,
  object_type      text NOT NULL,
  object_id        text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, app, idempotency_key)
);

-- Workflows: a workflow has versions; one version is live at a time.
CREATE TABLE IF NOT EXISTS commai_workflows (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name          text NOT NULL,
  description   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'live', 'paused')),
  live_version  int,
  live_from_seq bigint NOT NULL DEFAULT 0,  -- only events after publishing trigger it
  pack          text NOT NULL DEFAULT '',
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_workflows_customer ON commai_workflows (customer_id, status);

CREATE TABLE IF NOT EXISTS commai_workflow_versions (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id  uuid NOT NULL REFERENCES commai_workflows(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL,
  version      int NOT NULL,
  definition   jsonb NOT NULL,
  source       text NOT NULL DEFAULT 'person',
  note         text NOT NULL DEFAULT '',
  created_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workflow_id, version)
);

CREATE TABLE IF NOT EXISTS commai_workflow_runs (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  workflow_id      uuid NOT NULL REFERENCES commai_workflows(id) ON DELETE CASCADE,
  version_id       uuid NOT NULL REFERENCES commai_workflow_versions(id) ON DELETE CASCADE,
  version          int NOT NULL,
  event_id         uuid,
  conversation_id  uuid,
  test             boolean NOT NULL DEFAULT false,
  status           text NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'waiting', 'awaiting_approval', 'held', 'done', 'skipped',
                                     'failed', 'cancelled')),
  step_index       int NOT NULL DEFAULT 0,
  context          jsonb NOT NULL DEFAULT '{}',
  wait             jsonb NOT NULL DEFAULT '{}',
  error            text NOT NULL DEFAULT '',
  started_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  finished_at      timestamptz
);
-- A live run happens once per (workflow version, event).
CREATE UNIQUE INDEX IF NOT EXISTS commai_workflow_runs_once
  ON commai_workflow_runs (version_id, event_id) WHERE event_id IS NOT NULL AND NOT test;
CREATE INDEX IF NOT EXISTS commai_workflow_runs_wf ON commai_workflow_runs (workflow_id, started_at DESC);
CREATE INDEX IF NOT EXISTS commai_workflow_runs_waiting ON commai_workflow_runs (customer_id) WHERE status = 'waiting';

CREATE TABLE IF NOT EXISTS commai_workflow_run_steps (
  id           bigserial PRIMARY KEY,
  run_id       uuid NOT NULL REFERENCES commai_workflow_runs(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL,
  step_index   int NOT NULL,
  step_id      text NOT NULL,
  type         text NOT NULL,
  status       text NOT NULL,
  summary      text NOT NULL DEFAULT '',
  detail       jsonb NOT NULL DEFAULT '{}',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_workflow_run_steps_run ON commai_workflow_run_steps (run_id, id);

-- Where the trigger dispatcher has read commai_events up to.
CREATE TABLE IF NOT EXISTS commai_workflow_cursor (
  id        int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  last_seq   bigint NOT NULL DEFAULT 0,
  tick       bigint NOT NULL DEFAULT 0,
  gap_seq    bigint,
  gap_since  timestamptz
);
INSERT INTO commai_workflow_cursor (id) VALUES (1) ON CONFLICT DO NOTHING;

-- AI onboarding drafts: each is reviewed and approved before it goes live.
CREATE TABLE IF NOT EXISTS commai_onboarding_drafts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  batch        uuid NOT NULL,
  kind         text NOT NULL CHECK (kind IN ('profile', 'team', 'knowledge', 'routing', 'workflow')),
  title        text NOT NULL,
  content      jsonb NOT NULL DEFAULT '{}',
  source       text NOT NULL DEFAULT 'rules',
  status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved', 'rejected')),
  applied_ref  text NOT NULL DEFAULT '',
  created_by   text NOT NULL,
  reviewed_by  text NOT NULL DEFAULT '',
  reviewed_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_onboarding_drafts_customer ON commai_onboarding_drafts (customer_id, created_at DESC);

-- Platform assistant: questions, proposed fixes (applied only when an admin
-- approves) and support cases.
CREATE TABLE IF NOT EXISTS commai_assistant_answers (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  question     text NOT NULL,
  answer       text NOT NULL,
  confidence   text NOT NULL,
  findings     jsonb NOT NULL DEFAULT '[]',
  fixes        jsonb NOT NULL DEFAULT '[]',
  source       text NOT NULL DEFAULT 'rules',
  asked_by     text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS commai_assistant_fixes (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  answer_id    uuid REFERENCES commai_assistant_answers(id) ON DELETE SET NULL,
  fix_id       text NOT NULL,
  label        text NOT NULL,
  params       jsonb NOT NULL DEFAULT '{}',
  status       text NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'applied', 'failed', 'rejected')),
  result       jsonb NOT NULL DEFAULT '{}',
  proposed_at  timestamptz NOT NULL DEFAULT now(),
  decided_by   text NOT NULL DEFAULT '',
  decided_at   timestamptz
);
CREATE TABLE IF NOT EXISTS commai_support_cases (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  reference        text NOT NULL UNIQUE,
  subject          text NOT NULL,
  question         text NOT NULL DEFAULT '',
  status           text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
  configuration    jsonb NOT NULL DEFAULT '{}',
  diagnostics      jsonb NOT NULL DEFAULT '[]',
  errors           jsonb NOT NULL DEFAULT '[]',
  correlation_ids  text[] NOT NULL DEFAULT '{}',
  created_by       text NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
