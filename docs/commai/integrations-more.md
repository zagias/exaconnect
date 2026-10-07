# CommAI connectors: helpdesks, team chat, commerce, files and automation platforms

ADR 0029. Built on the connector kit (ADR 0028), the same way as HubSpot and
Google Calendar. Each connector:

- is listed in the integrations catalogue (`commai/connectors/installed.py`);
- stays on its **simulated stand-in** (example data, no network) until ExaCarib
  sets the provider app's environment settings **and** the go-live registry
  switches on `feature` / `integration-<app>` for that business;
- runs every action through the action service. Reads are separate from writes,
  writes are idempotent (the run's key is sent to the provider or looked up there
  first), and the kinds refund, delete and anything marked sensitive need a
  second person to approve;
- has test mode (a dry run with the inputs that would be sent), a health check,
  field-mapping suggestions, and repair causes (`expired_signin`, `permission`,
  `mapping`, `input`, `provider`) so the Integrations screen can say what to fix;
- backs off on 429 and 503 (Retry-After up to five seconds inline, longer waits
  retried by the job queue);
- takes inbound webhooks through the generic receiver,
  `/api/v1/commai/integration-hooks/{token}`, which checks the provider's
  signature before anything is stored.

## The connectors

| App | Sign-in | Reads | Writes (approval) | Webhooks checked with |
| --- | --- | --- | --- | --- |
| Zendesk | OAuth (per subdomain) | find_ticket, list_tickets | create_ticket, update_ticket, add_comment | `X-Zendesk-Webhook-Signature` (HMAC of timestamp + body, 5 min) |
| Freshdesk | API key entered by the business | find_ticket, list_tickets | create_ticket, update_ticket, add_comment | `X-CommAI-Token` header the business adds to its automation rule |
| ServiceNow | API key (`x-sn-apikey`) entered by the business | find_ticket, list_tickets | create_ticket, update_ticket, add_comment | `X-CommAI-Signature` (HMAC of body) from the business rule script |
| Slack | OAuth (bot) | list_channels | notify_staff, handover_alert, request_approval | Slack request signing (`X-Slack-Signature`, v0, 5 min) |
| Microsoft Teams | Incoming webhook address, or ExaCarib's bot | none | notify_staff, handover_alert, request_approval | Bot Framework JWT (RS256, issuer, audience, service URL) |
| Shopify | OAuth (per shop), GraphQL Admin API | find_order (order number and email must match) | refund_order (sensitive), cancel_order (sensitive) | `X-Shopify-Hmac-Sha256` with the app secret, plus shop domain |
| Stripe | Stripe Connect OAuth | payment_status | create_payment_link (sensitive), deactivate_payment_link | `Stripe-Signature` (t, v1, 5 min) |
| Google Drive | OAuth, drive.readonly | list_folders, list_files | none (sync only) | Push channel token (`X-Goog-Channel-Token`) |
| OneDrive and SharePoint | Microsoft OAuth, Files.Read.All and Sites.Read.All | list_folders, list_files, find_libraries | none (sync only) | Graph `clientState`, with the validationToken handshake |

Notes:

- **Helpdesks** link each ticket to its conversation (`commai_ticket_links`).
  When the provider says a ticket changed, CommAI adds a note to the
  conversation and emits `ticket.updated`.
- **Slack and Teams** never post message text, email addresses, phone numbers
  or card numbers. A business chooses how much to share (`none`, `names`,
  `summary`). Approve and Reject buttons call the action service only after
  the request is verified, the workspace or tenant is the business's own, and
  the person pressing is a linked CommAI member (not the one who proposed it).
  Endpoints: `POST /api/v1/commai/slack/interactions` and
  `POST /api/v1/commai/teams/messages`.
- **Shopify** shows an order only when the order number and the customer's
  email match, and returns only status, totals and tracking. `shop/redact`
  signs the shop out.
- **Stripe** never handles card data: inputs that look like card numbers are
  refused. Payment events add a note to the linked conversation and emit
  `payment.succeeded` or `payment.failed`.
- **Knowledge files** become knowledge sources that a person must approve
  before the AI uses them. A changed file needs approving again. A file that
  leaves the folders, or that the sharing rule now excludes, is removed. Files
  whose owner turned off downloading are always skipped. Syncs run on request,
  when the provider reports a change, and every six hours. Only Google Docs and
  Sheets, text, Markdown and CSV files are read; Word and PDF are listed as
  skipped.

## Automation platforms

`integrations/zapier/` (Zapier Platform app), `integrations/make/` (Make
custom app) and `integrations/n8n/` (n8n community node) use the public CommAI
API with a business's API key. They offer a trigger on CommAI events
(subscribed through `/webhooks`), plus actions: create contact, find contact,
start conversation, send message, add note and propose action. A test checks
that every endpoint they call is in the OpenAPI schema. Zapier and n8n check
the `X-ExaCarib-Signature` on each delivery; Make cannot run code, so its
README points businesses that need that to the polling trigger.

## What Dudley must create

None of this costs money to set up. Put each value in the server's `.env`,
never in the repository (`.env.example` lists the names). The OAuth redirect for
every app is `{EXA_PUBLIC_URL}/api/v1/commai/oauth/{app}/callback`.

| App | Create | Settings |
| --- | --- | --- |
| Zendesk | A global OAuth client (Zendesk Marketplace partner) | `EXA_ZENDESK_CLIENT_ID`, `EXA_ZENDESK_CLIENT_SECRET` |
| Freshdesk, ServiceNow | Nothing; each business enters its own key | none |
| Slack | A Slack app with bot scopes chat:write, channels:read, groups:read, channels:history, groups:history; Interactivity URL `{EXA_PUBLIC_URL}/api/v1/commai/slack/interactions` | `EXA_SLACK_CLIENT_ID`, `EXA_SLACK_CLIENT_SECRET`, `EXA_SLACK_SIGNING_SECRET` |
| Teams | An Azure Bot (multi-tenant) with messaging endpoint `{EXA_PUBLIC_URL}/api/v1/commai/teams/messages`; incoming webhooks need nothing | `EXA_TEAMS_BOT_ID`, `EXA_TEAMS_BOT_PASSWORD` |
| Shopify | A public app in the Shopify Partner dashboard with read_orders and write_orders, and the mandatory privacy webhooks | `EXA_SHOPIFY_CLIENT_ID`, `EXA_SHOPIFY_CLIENT_SECRET` |
| Stripe | A Stripe Connect platform (Standard accounts, OAuth on) | `EXA_STRIPE_CLIENT_ID`, `EXA_STRIPE_CLIENT_SECRET` |
| Google Drive | The Google app used for Calendar, with the Drive API on and drive.readonly added; Google's restricted-scope verification | `EXA_GOOGLE_CLIENT_ID`, `EXA_GOOGLE_CLIENT_SECRET` |
| OneDrive and SharePoint | A multi-tenant Entra ID app with Files.Read.All and Sites.Read.All (delegated) | `EXA_MS365_CLIENT_ID`, `EXA_MS365_CLIENT_SECRET` |
| Zapier, Make, n8n | Developer accounts, then publish as each README says | none |

Then switch on `integration-<app>` for a business in the go-live registry.
