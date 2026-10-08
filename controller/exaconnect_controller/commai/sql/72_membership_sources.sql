-- Who manages a membership (ADR 0023), widened for Jibsy's own account kinds:
-- 'directory' (directory connectors, ADR 0036), 'sandbox' (sandbox users,
-- ADR 0031) and 'partner' (a partner's staff in a linked business, ADR 0031).
-- Like 'scim' and 'sso', the portal does not change or remove these.
ALTER TABLE org_memberships DROP CONSTRAINT IF EXISTS org_memberships_managed_by_check;
ALTER TABLE org_memberships ADD CONSTRAINT org_memberships_managed_by_check
  CHECK (managed_by IN ('scim', 'sso', 'directory', 'sandbox', 'partner'));
