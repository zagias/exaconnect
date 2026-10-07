-- Directory templates and connectors (ADR 0030). Idempotent.

-- One guided set-up per business and provider. Off until the business connects it.
CREATE TABLE IF NOT EXISTS directory_setups (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id       uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  provider          text NOT NULL,
  status            text NOT NULL DEFAULT 'off' CHECK (status IN ('off', 'connected')),
  alias             text NOT NULL UNIQUE,              -- the gateway identity provider alias it uses
  protocol          text NOT NULL DEFAULT '' CHECK (protocol IN ('', 'saml', 'oidc')),
  mode              text NOT NULL DEFAULT 'none' CHECK (mode IN ('none', 'scim', 'pull', 'ldap')),
  inputs            jsonb NOT NULL DEFAULT '{}',       -- tenant ID, Okta domain...: never secrets
  settings          jsonb NOT NULL DEFAULT '{}',       -- pull and LDAP settings: never secrets
  secret_ref        text NOT NULL DEFAULT '',          -- the LDAP bind password, in the vault
  connection_id     uuid REFERENCES sso_connections(id) ON DELETE SET NULL,
  scim_token_id     bigint REFERENCES scim_tokens(id) ON DELETE SET NULL,
  accepted_presets  text[] NOT NULL DEFAULT '{}',
  presets_by        text,
  last_test         jsonb,
  tested_at         timestamptz,
  last_sync         jsonb,
  synced_at         timestamptz,
  sync_interval_s   int NOT NULL DEFAULT 3600 CHECK (sync_interval_s >= 900),
  connected_by      text,
  connected_at      timestamptz,
  created_by        text NOT NULL DEFAULT '',
  created_at        timestamptz NOT NULL DEFAULT now(),
  updated_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, provider)
);

-- What a pull sync made, so a person or group gone from the directory is found.
CREATE TABLE IF NOT EXISTS directory_synced_users (
  setup_id     uuid NOT NULL REFERENCES directory_setups(id) ON DELETE CASCADE,
  external_id  text NOT NULL,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (setup_id, external_id)
);
CREATE TABLE IF NOT EXISTS directory_synced_groups (
  setup_id     uuid NOT NULL REFERENCES directory_setups(id) ON DELETE CASCADE,
  external_id  text NOT NULL,
  group_id     uuid NOT NULL REFERENCES scim_groups(id) ON DELETE CASCADE,
  PRIMARY KEY (setup_id, external_id)
);
