-- More ready-made connectors (ADR 0035): helpdesks, team chat, commerce,
-- payments and knowledge sources. Idempotent.

-- A helpdesk ticket linked to a Jibsy conversation; the status is synced
-- back from the helpdesk's webhook.
CREATE TABLE IF NOT EXISTS commai_ticket_links (
  customer_id      uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app              text NOT NULL,
  ticket_id        text NOT NULL,
  conversation_id  uuid REFERENCES conversations(id) ON DELETE SET NULL,
  number           text NOT NULL DEFAULT '',
  status           text NOT NULL DEFAULT '',
  url              text NOT NULL DEFAULT '',
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, app, ticket_id)
);
CREATE INDEX IF NOT EXISTS commai_ticket_links_conv ON commai_ticket_links (conversation_id);

-- A staff member's Slack or Teams user, linked by a business admin to their
-- Jibsy sign-in, so a button press there acts as that person.
CREATE TABLE IF NOT EXISTS commai_chat_identities (
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app            text NOT NULL,
  external_user  text NOT NULL,
  user_email     text NOT NULL,
  created_by     text NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, app, external_user)
);

-- Files from Google Drive or OneDrive/SharePoint and the knowledge source
-- each became. A changed file needs approving again; a file the business
-- can no longer see is removed.
CREATE TABLE IF NOT EXISTS commai_knowledge_files (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app          text NOT NULL,
  file_id      text NOT NULL,
  source_id    uuid REFERENCES knowledge_sources(id) ON DELETE SET NULL,
  name         text NOT NULL DEFAULT '',
  version      text NOT NULL DEFAULT '',
  url          text NOT NULL DEFAULT '',
  status       text NOT NULL DEFAULT 'synced' CHECK (status IN ('synced', 'skipped', 'removed')),
  reason       text NOT NULL DEFAULT '',
  synced_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, app, file_id)
);

-- Small per-connection state: identities learnt from the provider (Slack
-- team, Stripe account), link codes, push channel ids.
CREATE TABLE IF NOT EXISTS commai_connector_state (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  app          text NOT NULL,
  key          text NOT NULL,
  value        jsonb NOT NULL DEFAULT '{}',
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, app, key)
);
CREATE INDEX IF NOT EXISTS commai_connector_state_lookup ON commai_connector_state (app, key, (value->>'id'));
