-- Organisations already using CommAI before plans existed keep it (ADR 0022):
-- without a CommAI subscription, the next plan write would take CommAI out of
-- customers.products. One subscription each, only where none was ever made.
INSERT INTO subscriptions (customer_id, plan_id, product, starts_on, created_by)
SELECT c.id, p.id, 'commai', c.created_at::date, 'system:schema'
FROM customers c JOIN plans p ON p.product = 'commai' AND p.name = 'CommAI Standard'
WHERE (EXISTS (SELECT 1 FROM commai_settings x WHERE x.customer_id = c.id)
       OR EXISTS (SELECT 1 FROM contacts x WHERE x.customer_id = c.id)
       OR EXISTS (SELECT 1 FROM conversations x WHERE x.customer_id = c.id))
  AND NOT EXISTS (SELECT 1 FROM subscriptions x WHERE x.customer_id = c.id AND x.product = 'commai');
