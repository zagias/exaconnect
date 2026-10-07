-- One portal, two apps (ADR 0041). An organisation's plans (customers.products)
-- say which apps it holds; each member may then be given some or all of them.
-- NULL means every app on the organisation's plan, so a plan added later
-- reaches everyone until an owner or admin narrows it.
ALTER TABLE org_memberships ADD COLUMN IF NOT EXISTS apps text[];

-- An owner or admin asking ExaCarib to add an app to the organisation's plan.
-- Cleared when the plan is changed.
CREATE TABLE IF NOT EXISTS customer_app_requests (
  customer_id   uuid NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  product       text NOT NULL CHECK (product IN ('connect', 'commai')),
  requested_by  text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (customer_id, product)
);
