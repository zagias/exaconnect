-- CommAI integration catalogue and standard interfaces (ADR 0028). Idempotent.

-- Outbound webhooks: the delivery format is a choice per endpoint. 'exacarib'
-- is the original v1 body; 'cloudevents' is a CloudEvents 1.0 structured JSON
-- body. Every delivery carries the v1 signature; standard_headers adds the
-- Standard Webhooks headers (webhook-id, webhook-timestamp, webhook-signature).
ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS format text NOT NULL DEFAULT 'exacarib';
ALTER TABLE webhook_endpoints ADD COLUMN IF NOT EXISTS standard_headers boolean NOT NULL DEFAULT false;
DO $$ BEGIN
  ALTER TABLE webhook_endpoints ADD CONSTRAINT webhook_endpoints_format_check
    CHECK (format IN ('exacarib', 'cloudevents'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Inbound webhooks: a generic hook (Zapier, Make, n8n, any system) starts
-- workflows; an app hook carries an app's own change notifications. Each has
-- its own address token and signing secret.
CREATE TABLE IF NOT EXISTS commai_inbound_hooks (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id       uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind              text NOT NULL DEFAULT 'generic' CHECK (kind IN ('generic', 'app')),
  app               text NOT NULL DEFAULT '',
  name              text NOT NULL,
  event_type        text NOT NULL DEFAULT '',
  token             text NOT NULL UNIQUE,
  secret            text NOT NULL,
  active            boolean NOT NULL DEFAULT true,
  received_count    bigint NOT NULL DEFAULT 0,
  rejected_count    bigint NOT NULL DEFAULT 0,
  last_received_at  timestamptz,
  last_rejected_at  timestamptz,
  last_error        text NOT NULL DEFAULT '',
  created_by        text NOT NULL DEFAULT '',
  created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS commai_inbound_hooks_customer ON commai_inbound_hooks (customer_id, kind);
CREATE UNIQUE INDEX IF NOT EXISTS commai_inbound_hooks_app ON commai_inbound_hooks (customer_id, app) WHERE kind = 'app';

-- A delivery is accepted once per delivery id (webhook-id, ce-id or the provider's id).
CREATE TABLE IF NOT EXISTS commai_inbound_receipts (
  hook_id      uuid NOT NULL REFERENCES commai_inbound_hooks(id) ON DELETE CASCADE,
  delivery_id  text NOT NULL,
  event_id     uuid,
  received_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (hook_id, delivery_id)
);

-- A business's own REST app, described by an OpenAPI 3 document. Only the
-- operations a person chose become actions; a person approves it before use.
CREATE TABLE IF NOT EXISTS commai_rest_apps (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  slug         text NOT NULL,
  name         text NOT NULL,
  base_url     text NOT NULL DEFAULT '',
  document     jsonb NOT NULL DEFAULT '{}',
  operations   jsonb NOT NULL DEFAULT '[]',
  actions      jsonb NOT NULL DEFAULT '[]',
  auth         jsonb NOT NULL DEFAULT '{}',
  status       text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
  drafted_by   text NOT NULL DEFAULT 'person',
  version      int NOT NULL DEFAULT 1,
  approved_by  text NOT NULL DEFAULT '',
  approved_at  timestamptz,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, slug)
);

-- IMAP/SMTP mailboxes for email channel accounts (provider 'mailbox'). The
-- password is in commai_secrets (secret_ref), never here.
CREATE TABLE IF NOT EXISTS commai_mailboxes (
  account_id      uuid PRIMARY KEY REFERENCES channel_accounts(id) ON DELETE CASCADE,
  customer_id     uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  imap_host       text NOT NULL,
  imap_port       int NOT NULL DEFAULT 993,
  imap_security   text NOT NULL DEFAULT 'ssl' CHECK (imap_security IN ('ssl', 'starttls')),
  smtp_host       text NOT NULL,
  smtp_port       int NOT NULL DEFAULT 587,
  smtp_security   text NOT NULL DEFAULT 'starttls' CHECK (smtp_security IN ('ssl', 'starttls')),
  username        text NOT NULL,
  secret_ref      text NOT NULL DEFAULT '',
  folder          text NOT NULL DEFAULT 'INBOX',
  mode            text NOT NULL DEFAULT 'poll' CHECK (mode IN ('poll', 'idle')),
  poll_seconds    int NOT NULL DEFAULT 60,
  uidvalidity     bigint,
  last_uid        bigint NOT NULL DEFAULT 0,
  last_poll_at    timestamptz,
  last_ok_at      timestamptz,
  last_error      text NOT NULL DEFAULT '',
  created_by      text NOT NULL DEFAULT '',
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

-- The iCalendar feed of a business's bookings: only the token's hash is kept;
-- invite links are signed with the feed's own key.
CREATE TABLE IF NOT EXISTS commai_ical_feeds (
  customer_id  uuid PRIMARY KEY REFERENCES customers(id) ON DELETE CASCADE,
  token_hash   text NOT NULL UNIQUE,
  sign_key     text NOT NULL,
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Every CSV and vCard import, with what it did.
CREATE TABLE IF NOT EXISTS commai_data_imports (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  kind         text NOT NULL,
  rows         int NOT NULL DEFAULT 0,
  created      int NOT NULL DEFAULT 0,
  updated      int NOT NULL DEFAULT 0,
  skipped      int NOT NULL DEFAULT 0,
  problems     jsonb NOT NULL DEFAULT '[]',
  created_by   text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now()
);
