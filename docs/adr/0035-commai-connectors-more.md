# ADR 0035: More CommAI connectors

Date: 7 October 2026
Status: accepted

## Context

Businesses asked for the tools they already use: helpdesks (Zendesk,
Freshdesk, ServiceNow), team chat (Slack, Teams), commerce and payments
(Shopify, Stripe), shared files as knowledge (Google Drive,
OneDrive/SharePoint), and the automation platforms (Zapier, Make, n8n).
The connector kit (ADR 0034) and the action service (ADR 0020) already exist.

## Decision

- Each app is one file under `commai/connectors/`, built on the kit, and
  written against the provider's real API shape. Code they share
  (`more_common.py`, `helpdesk.py`, `knowledge_files.py`) holds the parts
  that are the same across apps.
- Each app runs on a simulated stand-in until ExaCarib's app credentials are
  set and `integration-<app>` is switched on in the go-live registry. Tests
  make no network calls.
- Writes go through the action service. Refunds, order cancellations and
  payment links are sensitive and always need a second person. Writes carry
  the run's key to the provider (an idempotency header, external id, tag,
  note or metadata), or look it up there first, so a retry never repeats a
  write.
- Inbound webhooks use the kit's generic receiver and each provider's own
  signature scheme. Where a provider has none (Freshdesk, ServiceNow), the
  business adds a CommAI-issued secret header or HMAC to its rule.
- Slack and Teams carry approval buttons, not conversations. They post only
  what the business allows (`none`, `names`, `summary`), and never message
  text, emails, phones or card numbers. A button press counts only from a
  verified request, from the business's own workspace or tenant, by a linked
  member who did not propose the action.
- Files from Drive and OneDrive become knowledge sources a person approves.
  Changes need approving again, and removed or no-longer-shared files are
  taken out. Only text formats are read for now.
- Zapier, Make and n8n definitions live in `integrations/` and use only the
  public API. A test checks that their endpoints exist in the OpenAPI schema.

## Consequences

- Freshdesk, ServiceNow and Teams incoming webhooks can go live with no
  ExaCarib app. The others wait on the apps listed in
  `docs/commai/integrations-more.md`.
- Google's drive.readonly is a restricted scope, so a security assessment is
  needed before the Drive connector is open to everyone.
- Word and PDF files are skipped until a text extractor is chosen. That will
  be a separate decision.
- The automation file client (`automation/http.py`) no longer cuts text,
  Markdown and CSV bodies short, so file contents reach the knowledge base
  whole. The 2 MB limit still applies.
