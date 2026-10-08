-- Identity (ADR 0017): two-step sign-in, single sign-on through the gateway,
-- SCIM provisioning. Idempotent; applied after schema.sql and 00_core.sql.

-- Accounts. disabled_at: switched off (SCIM active=false); no sign-in, no keys.
-- access_scopes: NULL is the full account; a list limits it like a scoped API
-- key (people provisioned from a directory until an approved admin mapping).
ALTER TABLE users ADD COLUMN IF NOT EXISTS disabled_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS access_scopes text[];
ALTER TABLE users ADD COLUMN IF NOT EXISTS provisioned_by text;  -- NULL (by hand), 'scim' or 'sso'
ALTER TABLE users ADD COLUMN IF NOT EXISTS display_name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS given_name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS family_name text NOT NULL DEFAULT '';
ALTER TABLE users ADD COLUMN IF NOT EXISTS scim_external_id text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS scim_deleted_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS scim_linked boolean NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();
-- Two-step sign-in (TOTP, RFC 6238). The secret has to be readable to check codes.
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_pending text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_enabled_at timestamptz;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_last_step bigint;

ALTER TABLE sessions ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS via text NOT NULL DEFAULT 'password';

CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
  id         bigserial PRIMARY KEY,
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  code_hash  text NOT NULL,
  used_at    timestamptz
);
CREATE INDEX IF NOT EXISTS mfa_recovery_codes_user ON mfa_recovery_codes (user_id) WHERE used_at IS NULL;

-- A password that was right, waiting for the second step. Single use, short-lived.
CREATE TABLE IF NOT EXISTS mfa_challenges (
  token_hash  text PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at  timestamptz NOT NULL,
  attempts    int NOT NULL DEFAULT 0,
  used_at     timestamptz
);

-- One row per OIDC redirect to the gateway (state, nonce, PKCE verifier). Single use.
CREATE TABLE IF NOT EXISTS oidc_states (
  state_hash     text PRIMARY KEY,
  nonce          text NOT NULL,
  verifier       text NOT NULL,
  idp            text NOT NULL,
  purpose        text NOT NULL CHECK (purpose IN ('signin', 'test')),
  connection_id  uuid,
  next_path      text NOT NULL DEFAULT '/',
  requested_by   text NOT NULL DEFAULT '',
  expires_at     timestamptz NOT NULL,
  used_at        timestamptz
);

-- Enterprise single sign-on: a business's own identity provider, brokered by Keycloak.
CREATE TABLE IF NOT EXISTS sso_connections (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  alias          text NOT NULL UNIQUE,               -- the Keycloak identity provider alias
  protocol       text NOT NULL CHECK (protocol IN ('saml', 'oidc')),
  display_name   text NOT NULL,
  metadata_xml   text NOT NULL DEFAULT '',           -- SAML
  metadata_url   text NOT NULL DEFAULT '',           -- SAML metadata URL or OIDC discovery URL
  client_id      text NOT NULL DEFAULT '',           -- OIDC; the client secret is kept by Keycloak only
  status         text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'tested', 'enabled', 'disabled')),
  require_sso    boolean NOT NULL DEFAULT false,
  last_test      jsonb,
  tested_at      timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sso_connections_customer ON sso_connections (customer_id);

-- Email-domain claims. They route nobody until an ExaCarib admin approves them.
CREATE TABLE IF NOT EXISTS sso_domains (
  id             bigserial PRIMARY KEY,
  connection_id  uuid NOT NULL REFERENCES sso_connections(id) ON DELETE CASCADE,
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  domain         text NOT NULL,
  status         text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
  requested_by   text NOT NULL DEFAULT '',
  decided_by     text,
  decided_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, domain)
);
CREATE UNIQUE INDEX IF NOT EXISTS sso_domains_one_owner ON sso_domains (domain) WHERE status = 'approved';

-- SCIM 2.0 bearer tokens, one business each. Only the hash is kept.
CREATE TABLE IF NOT EXISTS scim_tokens (
  id            bigserial PRIMARY KEY,
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  name          text NOT NULL,
  prefix        text NOT NULL,
  token_hash    text NOT NULL UNIQUE,
  created_by    text NOT NULL DEFAULT '',
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz
);

-- Directory groups and how they map to Jibsy teams, seats and rights.
CREATE TABLE IF NOT EXISTS scim_groups (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id    uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  display_name   text NOT NULL,
  external_id    text,
  team_id        uuid REFERENCES commai_teams(id) ON DELETE SET NULL,
  seat           text NOT NULL DEFAULT 'agent' CHECK (seat IN ('agent', 'internal')),
  -- Business-admin rights: asked for by a business admin, granted only once approved.
  admin_requested       boolean NOT NULL DEFAULT false,
  admin_requested_by    text,
  admin_approved_by     text,
  admin_approved_at     timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (customer_id, display_name)
);
CREATE TABLE IF NOT EXISTS scim_group_members (
  group_id  uuid NOT NULL REFERENCES scim_groups(id) ON DELETE CASCADE,
  user_id   uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  PRIMARY KEY (group_id, user_id)
);

-- Passkeys (WebAuthn) as the second step for local accounts.
CREATE TABLE IF NOT EXISTS passkeys (
  id             bigserial PRIMARY KEY,
  user_id        uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  credential_id  text NOT NULL UNIQUE,            -- base64url
  public_key     bytea NOT NULL,                  -- COSE key
  sign_count     bigint NOT NULL DEFAULT 0,
  name           text NOT NULL DEFAULT 'Passkey',
  created_at     timestamptz NOT NULL DEFAULT now(),
  last_used_at   timestamptz
);
CREATE INDEX IF NOT EXISTS passkeys_user ON passkeys (user_id);
-- A WebAuthn challenge we issued, single use, for registration or sign-in.
CREATE TABLE IF NOT EXISTS passkey_challenges (
  token_hash  text PRIMARY KEY,
  user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  purpose     text NOT NULL CHECK (purpose IN ('register', 'signin')),
  challenge   bytea NOT NULL,
  expires_at  timestamptz NOT NULL,
  used_at     timestamptz
);
