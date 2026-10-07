-- Shared accounts (ADR 0023): one person, several organisations, a role in each.
-- Idempotent; applied after 10_identity.sql (it reads provisioned_by and access_scopes).
--
-- users.customer_id stays: it is the person's primary (default) organisation,
-- and every path that sets it (ExaCarib admin, SCIM, SSO, seed, tests) gets a
-- membership row from the trigger below, so nothing that predates memberships
-- loses access. Carrier and ExaCarib admin accounts have no memberships.

-- The plans an organisation holds: Connect, CommAI or both. Existing (and, until
-- the billing work's subscriptions land, new) organisations hold both; NULL is
-- read as both too. The billing layer reconciles this column when it merges.
ALTER TABLE customers ADD COLUMN IF NOT EXISTS products text[] DEFAULT ARRAY['connect', 'commai']::text[];
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'customers_products_known') THEN
    ALTER TABLE customers ADD CONSTRAINT customers_products_known
      CHECK (products IS NULL OR products <@ ARRAY['connect', 'commai']::text[]);
  END IF;
END
$$;

CREATE TABLE IF NOT EXISTS org_memberships (
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role         text NOT NULL CHECK (role IN ('owner', 'admin', 'member', 'viewer')),
  -- NULL: managed in the portal. 'scim' or 'sso': the business's directory owns
  -- this person's rights here, so the portal does not change or remove them.
  managed_by   text CHECK (managed_by IN ('scim', 'sso')),
  added_by     text NOT NULL DEFAULT '',
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, user_id)
);
CREATE INDEX IF NOT EXISTS org_memberships_user ON org_memberships (user_id);

-- Invitations. Only the SHA-256 of the one-time token is kept; the link is shown
-- once to the person who invited (no email is sent yet).
CREATE TABLE IF NOT EXISTS org_invites (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  customer_id  uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  email        text NOT NULL,
  role         text NOT NULL CHECK (role IN ('admin', 'member', 'viewer')),
  token_hash   text NOT NULL UNIQUE,
  invited_by   text NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  expires_at   timestamptz NOT NULL,
  accepted_at  timestamptz,
  accepted_by  uuid REFERENCES users(id) ON DELETE SET NULL,
  revoked_at   timestamptz
);
CREATE INDEX IF NOT EXISTS org_invites_customer ON org_invites (customer_id, created_at DESC);

-- The organisation a portal session is acting for (NULL: the primary one).
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS customer_id uuid REFERENCES customers(id) ON DELETE SET NULL;
-- The organisation an API key acts in. Set when the key is made; a key never
-- follows its owner into another organisation.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS customer_id uuid REFERENCES customers(id) ON DELETE CASCADE;

-- Every customer account with a primary organisation is a member of it.
CREATE OR REPLACE FUNCTION exa_primary_membership() RETURNS trigger AS $$
BEGIN
  IF NEW.role = 'customer' AND NEW.customer_id IS NOT NULL THEN
    INSERT INTO org_memberships (customer_id, user_id, role, managed_by, added_by)
    VALUES (
      NEW.customer_id,
      NEW.id,
      CASE
        WHEN NEW.provisioned_by IS NOT NULL THEN
          CASE WHEN NEW.access_scopes IS NULL THEN 'admin' ELSE 'member' END
        WHEN NOT EXISTS (SELECT 1 FROM org_memberships o WHERE o.customer_id = NEW.customer_id AND o.role = 'owner')
          THEN 'owner'
        ELSE 'admin'
      END,
      NEW.provisioned_by,
      'system'
    )
    ON CONFLICT (customer_id, user_id) DO NOTHING;
  END IF;
  RETURN NEW;
END
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS users_primary_membership ON users;
CREATE TRIGGER users_primary_membership AFTER INSERT OR UPDATE OF customer_id, role ON users
  FOR EACH ROW EXECUTE FUNCTION exa_primary_membership();

-- Back-fill for accounts made before memberships: hand-made people become
-- admins (the rights they had), directory people follow their scopes.
INSERT INTO org_memberships (customer_id, user_id, role, managed_by, added_by)
SELECT u.customer_id, u.id,
       CASE WHEN u.provisioned_by IS NOT NULL AND u.access_scopes IS NOT NULL THEN 'member' ELSE 'admin' END,
       u.provisioned_by, 'migration'
FROM users u
WHERE u.role = 'customer' AND u.customer_id IS NOT NULL
ON CONFLICT (customer_id, user_id) DO NOTHING;

-- Every organisation with hand-managed people has an owner: the earliest of them.
UPDATE org_memberships m SET role = 'owner', updated_at = now()
FROM (
  SELECT DISTINCT ON (o.customer_id) o.customer_id, o.user_id
  FROM org_memberships o JOIN users u ON u.id = o.user_id
  WHERE o.managed_by IS NULL AND o.role IN ('admin', 'owner')
  ORDER BY o.customer_id, (o.role = 'owner') DESC, u.created_at, u.id
) first
WHERE m.customer_id = first.customer_id AND m.user_id = first.user_id AND m.role <> 'owner'
  AND NOT EXISTS (SELECT 1 FROM org_memberships x WHERE x.customer_id = m.customer_id AND x.role = 'owner');

-- Keys made before memberships act in the organisation they were made in.
UPDATE api_keys k SET customer_id = u.customer_id
FROM users u
WHERE k.user_id = u.id AND k.customer_id IS NULL AND u.role = 'customer' AND u.customer_id IS NOT NULL;
