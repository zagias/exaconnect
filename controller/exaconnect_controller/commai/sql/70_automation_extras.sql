-- Jibsy gap fixes for automation, the assistant and billing (ADR 0039). Idempotent.

-- ---- module entitlements --------------------------------------------------------------
-- Messaging, voice, AI agents and automation are sold separately. No row means
-- the module is on, so every business that existed before this keeps everything.
CREATE TABLE IF NOT EXISTS commai_entitlements (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  module       text NOT NULL CHECK (module IN ('messaging', 'voice', 'ai_agents', 'automation')),
  enabled      boolean NOT NULL,
  note         text NOT NULL DEFAULT '',
  updated_by   text NOT NULL,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, module)
);

-- ---- approvals: what an action will do, worked out when it is proposed --------------------
ALTER TABLE action_runs ADD COLUMN IF NOT EXISTS preview jsonb NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS action_runs_awaiting ON action_runs (customer_id, created_at)
  WHERE status = 'awaiting_approval';

-- ---- workflow schedules (time triggers) ---------------------------------------------------
-- One row per live scheduled workflow. The token changes on every publish, pause
-- or resume, so a timer queued for an older schedule does nothing.
CREATE TABLE IF NOT EXISTS commai_workflow_schedules (
  workflow_id  uuid PRIMARY KEY REFERENCES commai_workflows(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  token        text NOT NULL,
  next_at      timestamptz NOT NULL,
  last_at      timestamptz,
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Which workflow sent a message, so its cost counts against that workflow's budget.
CREATE TABLE IF NOT EXISTS commai_message_sources (
  message_id   uuid PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  workflow_id  uuid NOT NULL,
  run_id       uuid
);

-- ---- support cases reach ExaCarib ----------------------------------------------------------
ALTER TABLE commai_support_cases DROP CONSTRAINT IF EXISTS commai_support_cases_status_check;
ALTER TABLE commai_support_cases ADD CONSTRAINT commai_support_cases_status_check
  CHECK (status IN ('open', 'in_progress', 'waiting_on_customer', 'closed'));
ALTER TABLE commai_support_cases ADD COLUMN IF NOT EXISTS priority text NOT NULL DEFAULT 'normal';
ALTER TABLE commai_support_cases ADD COLUMN IF NOT EXISTS assignee text NOT NULL DEFAULT '';
ALTER TABLE commai_support_cases ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS commai_support_cases_queue ON commai_support_cases (status, created_at);
CREATE TABLE IF NOT EXISTS commai_support_replies (
  id           bigserial PRIMARY KEY,
  case_id      uuid NOT NULL REFERENCES commai_support_cases(id) ON DELETE CASCADE,
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  author       text NOT NULL,
  from_exacarib boolean NOT NULL,
  internal     boolean NOT NULL DEFAULT false,  -- ExaCarib's own notes; the business never sees them
  body         text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_support_replies_case ON commai_support_replies (case_id, id);

-- ---- the single bill: rate cards for messaging, AI and workflows ---------------------------
CREATE TABLE IF NOT EXISTS commai_rate_cards (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  version         int NOT NULL,
  label           text NOT NULL DEFAULT '',
  example         boolean NOT NULL DEFAULT false,
  currency        text NOT NULL DEFAULT 'USD',
  effective_from  timestamptz NOT NULL DEFAULT now(),
  prices          jsonb NOT NULL,   -- {"meter": "unit price as a string"}
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, version)
);

-- One rated charge per usage record, priced on the card in force when it was used.
CREATE TABLE IF NOT EXISTS commai_charges (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id        uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  usage_id           bigint NOT NULL UNIQUE REFERENCES usage_records(id) ON DELETE CASCADE,
  meter              text NOT NULL,
  family             text NOT NULL,   -- messaging | ai | automation
  channel            text NOT NULL DEFAULT '',
  workflow_id        uuid,
  quantity           numeric(18, 6) NOT NULL,
  unit_price         numeric(14, 8) NOT NULL,
  amount             numeric(16, 6) NOT NULL,
  rate_card_id       uuid REFERENCES commai_rate_cards(id),
  rate_card_version  int NOT NULL,
  at                 timestamptz NOT NULL,
  bill_id            uuid
);
CREATE INDEX IF NOT EXISTS commai_charges_period ON commai_charges (customer_id, at);
CREATE INDEX IF NOT EXISTS commai_charges_workflow ON commai_charges (customer_id, workflow_id, at)
  WHERE workflow_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS commai_bills (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind          text NOT NULL DEFAULT 'invoice' CHECK (kind IN ('invoice', 'credit_note')),
  number        text,
  status        text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'issued')),
  period_start  date NOT NULL,
  period_end    date NOT NULL,
  currency      text NOT NULL DEFAULT 'USD',
  total         numeric(14, 2) NOT NULL DEFAULT 0,
  credits_id    uuid REFERENCES commai_bills(id),
  reason        text NOT NULL DEFAULT '',
  created_by    text NOT NULL,
  issued_by     text NOT NULL DEFAULT '',
  issued_at     timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, number)
);
CREATE UNIQUE INDEX IF NOT EXISTS commai_bills_one_draft
  ON commai_bills (customer_id, period_start) WHERE status = 'draft' AND kind = 'invoice';

CREATE TABLE IF NOT EXISTS commai_bill_lines (
  id               bigserial PRIMARY KEY,
  bill_id          uuid NOT NULL REFERENCES commai_bills(id) ON DELETE CASCADE,
  customer_id      uuid NOT NULL,
  family           text NOT NULL,   -- messaging | ai | automation | voice
  meter            text NOT NULL DEFAULT '',
  voice_charge_id  uuid,            -- a voice line traces to its call or fee
  rate_card_version int,
  credits_line     bigint REFERENCES commai_bill_lines(id),
  description      text NOT NULL,
  quantity         numeric(18, 6) NOT NULL DEFAULT 0,
  unit_price       numeric(14, 8),
  amount           numeric(16, 6) NOT NULL
);
CREATE INDEX IF NOT EXISTS commai_bill_lines_bill ON commai_bill_lines (bill_id, id);

-- Voice charges land on exactly one document: the single bill or (older) a voice invoice.
ALTER TABLE voice_charges ADD COLUMN IF NOT EXISTS bill_id uuid;

-- Issued bills and their lines never change.
CREATE OR REPLACE FUNCTION commai_bill_frozen() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM customers WHERE id = OLD.customer_id) THEN
    RETURN OLD;
  END IF;
  IF TG_TABLE_NAME = 'commai_bills' THEN
    IF OLD.status = 'issued' THEN
      RAISE EXCEPTION 'bill % is issued and frozen', OLD.number USING ERRCODE = 'check_violation';
    END IF;
  ELSE
    IF EXISTS (SELECT 1 FROM commai_bills WHERE id = OLD.bill_id AND status = 'issued') THEN
      RAISE EXCEPTION 'lines of an issued bill are frozen' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS commai_bills_frozen ON commai_bills;
CREATE TRIGGER commai_bills_frozen BEFORE UPDATE OR DELETE ON commai_bills
  FOR EACH ROW EXECUTE FUNCTION commai_bill_frozen();
DROP TRIGGER IF EXISTS commai_bill_lines_frozen ON commai_bill_lines;
CREATE TRIGGER commai_bill_lines_frozen BEFORE UPDATE OR DELETE ON commai_bill_lines
  FOR EACH ROW EXECUTE FUNCTION commai_bill_frozen();

-- ---- money budgets ---------------------------------------------------------------------
-- scope: total | channel (key = channel name) | workflow (key = workflow id) | ai
CREATE TABLE IF NOT EXISTS commai_budgets (
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  scope          text NOT NULL CHECK (scope IN ('total', 'channel', 'workflow', 'ai')),
  key            text NOT NULL DEFAULT '',
  monthly_alert  numeric(14, 4),
  monthly_hard   numeric(14, 4),
  updated_by     text NOT NULL,
  updated_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, scope, key)
);

-- ---- AI supplier costs (model and speech), imported from the supplier's usage ------------
CREATE TABLE IF NOT EXISTS ai_supplier_imports (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  supplier    text NOT NULL,
  period      date NOT NULL,
  simulated   boolean NOT NULL DEFAULT false,
  rows        int NOT NULL DEFAULT 0,
  created_by  text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);
-- One row per supplier, day, model and kind (tokens or speech seconds).
CREATE TABLE IF NOT EXISTS ai_supplier_costs (
  id          bigserial PRIMARY KEY,
  import_id   uuid NOT NULL REFERENCES ai_supplier_imports(id) ON DELETE CASCADE,
  supplier    text NOT NULL,
  day         date NOT NULL,
  model       text NOT NULL,
  kind        text NOT NULL CHECK (kind IN ('tokens', 'speech_seconds')),
  quantity    numeric(18, 4) NOT NULL,
  cost        numeric(14, 6) NOT NULL,
  UNIQUE (supplier, day, model, kind)
);

-- ---- onboarding: channel set-up drafts ------------------------------------------------------
ALTER TABLE commai_onboarding_drafts DROP CONSTRAINT IF EXISTS commai_onboarding_drafts_kind_check;
ALTER TABLE commai_onboarding_drafts ADD CONSTRAINT commai_onboarding_drafts_kind_check
  CHECK (kind IN ('profile', 'team', 'knowledge', 'routing', 'workflow', 'channel'));
