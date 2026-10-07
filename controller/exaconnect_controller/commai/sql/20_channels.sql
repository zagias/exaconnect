-- Jibsy channels (ADR 0018): website chat, WhatsApp, SMS and email.
-- Applied after 00_core.sql, idempotently, at startup.

-- Website chat: one publishable key per website. The secret signs visitor
-- sessions and verifies the tokens the business's own site signs for its
-- signed-in customers. It is shown once, when the key is made or rotated.
CREATE TABLE IF NOT EXISTS widget_keys (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  public_key       text NOT NULL UNIQUE,
  secret           text NOT NULL,
  name             text NOT NULL DEFAULT 'Website',
  allowed_origins  text[] NOT NULL DEFAULT '{}',
  settings         jsonb NOT NULL DEFAULT '{}',
  active           boolean NOT NULL DEFAULT true,
  created_by       text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now(),
  rotated_at       timestamptz
);
CREATE INDEX IF NOT EXISTS widget_keys_customer ON widget_keys (customer_id);

-- The installation checker: where the widget has reported from.
CREATE TABLE IF NOT EXISTS widget_installs (
  key_id         uuid NOT NULL REFERENCES widget_keys(id) ON DELETE CASCADE,
  origin         text NOT NULL,
  page           text NOT NULL DEFAULT '',
  allowed        boolean NOT NULL DEFAULT false,
  hits           bigint NOT NULL DEFAULT 1,
  first_seen_at  timestamptz NOT NULL DEFAULT now(),
  last_seen_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (key_id, origin)
);

-- Small files attached to messages (website chat uploads).
CREATE TABLE IF NOT EXISTS channel_files (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  conversation_id  uuid REFERENCES conversations(id) ON DELETE CASCADE,
  owner            text NOT NULL,           -- the visitor address that uploaded it
  name             text NOT NULL,
  content_type     text NOT NULL,
  size             int NOT NULL,
  data             bytea NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS channel_files_conversation ON channel_files (conversation_id);

-- WhatsApp, SMS and email accounts. Credentials are never stored here: the
-- provider adapters read them from the environment. hook_token names the
-- inbound webhook URL; hook_secret signs simulated webhooks and is the shared
-- secret for inbound email.
CREATE TABLE IF NOT EXISTS channel_accounts (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  channel          text NOT NULL CHECK (channel IN ('whatsapp', 'sms', 'email')),
  provider         text NOT NULL,
  name             text NOT NULL DEFAULT '',
  address          text NOT NULL,
  status           text NOT NULL DEFAULT 'setup' CHECK (status IN ('setup', 'live', 'paused', 'broken')),
  settings         jsonb NOT NULL DEFAULT '{}',
  hook_token       text NOT NULL UNIQUE,
  hook_secret      text NOT NULL,
  last_inbound_at  timestamptz,
  last_sent_at     timestamptz,
  last_error       text NOT NULL DEFAULT '',
  last_error_at    timestamptz,
  created_by       text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, channel, address)
);

-- Which account a conversation came in on, so replies leave from it.
CREATE TABLE IF NOT EXISTS channel_threads (
  conversation_id  uuid PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
  customer_id      uuid NOT NULL,
  account_id       uuid REFERENCES channel_accounts(id) ON DELETE SET NULL,
  updated_at       timestamptz NOT NULL DEFAULT now()
);

-- WhatsApp message templates. Only an approved template may be sent outside
-- the 24-hour service window.
CREATE TABLE IF NOT EXISTS whatsapp_templates (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id           uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name                  text NOT NULL,
  language              text NOT NULL DEFAULT 'en',
  category              text NOT NULL DEFAULT 'utility' CHECK (category IN ('utility', 'marketing', 'authentication')),
  body                  text NOT NULL,
  status                text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  provider_template_id  text NOT NULL DEFAULT '',
  status_reason         text NOT NULL DEFAULT '',
  status_by             text NOT NULL DEFAULT '',
  created_by            text NOT NULL DEFAULT '',
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, name, language)
);

-- The values filled into a template for one message.
CREATE TABLE IF NOT EXISTS template_sends (
  message_id   uuid PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  template_id  uuid REFERENCES whatsapp_templates(id) ON DELETE SET NULL,
  params       text[] NOT NULL DEFAULT '{}'
);

-- Email threading: every Message-ID we sent or received, and its conversation.
CREATE TABLE IF NOT EXISTS email_message_ids (
  customer_id      uuid NOT NULL,
  message_id       text NOT NULL,
  conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  chat_message_id  uuid,
  PRIMARY KEY (customer_id, message_id)
);

-- What the simulated providers "sent", so tests and the setup screen can show it.
CREATE TABLE IF NOT EXISTS sim_channel_outbox (
  id            bigserial PRIMARY KEY,
  customer_id   uuid NOT NULL,
  account_id    uuid,
  channel       text NOT NULL,
  to_address    text NOT NULL,
  body          text NOT NULL DEFAULT '',
  template      text NOT NULL DEFAULT '',
  headers       jsonb NOT NULL DEFAULT '{}',
  provider_ref  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sim_channel_outbox_customer ON sim_channel_outbox (customer_id, id DESC);

-- Inbound webhook outcomes (accepted, duplicate, rejected signature...), for diagnostics.
CREATE TABLE IF NOT EXISTS channel_webhook_log (
  id           bigserial PRIMARY KEY,
  customer_id  uuid NOT NULL,
  account_id   uuid REFERENCES channel_accounts(id) ON DELETE CASCADE,
  outcome      text NOT NULL,
  detail       text NOT NULL DEFAULT '',
  at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS channel_webhook_log_account ON channel_webhook_log (account_id, id DESC);
