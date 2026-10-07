# CommAI integration catalogue

Operator note for ADR 0034. Code: `controller/exaconnect_controller/commai/connectors/`
(one file per app; the shared kit is `connectors/kit.py`), `commai/standards/`,
`commai/channels/mailbox.py` and `commai/api/integrations_catalogue.py`. Portal:
CommAI → App catalogue (`portal/src/pages/commai/Catalogue.tsx`).

## How every app works

- **Off until switched on.** Each app declares a go-live capability
  (`kind = feature`, `key = integration-<app>`) with written criteria. Every one
  has `app-registered`, `live-test` and `permissions`, plus its own criteria and the
  three base criteria from ADR 0028. Until an ExaCarib admin records them as met and
  switches the capability to pilot or on, a business's connection runs on the app's
  **stand-in**. The stand-in is an in-process copy of the provider's API that answers
  in the provider's own shapes, keeps records per business, and is labelled
  "Example data" in the portal.
- **Real API shapes.** Each connector is written from the provider's public
  documentation: its endpoints, OAuth flow, pagination, error bodies, rate-limit
  headers and webhook signatures. Tests drive the real request path against the
  stand-in, or a fake HTTP layer, with no calls to real providers.
- **Actions.** Reads are kept apart from writes (create, update, cancel, delete).
  Deletes and cancels need a person's approval. Every write is idempotent: a retry
  with the same key never creates twice. Each app records how in its own file.
- **Rate limits.** A `Retry-After` of 5 s or less is waited out inline, at most twice.
  Anything longer becomes a `provider` error, and the action service retries later.
- **Health and repair.** Failures are named `expired_signin`, `permission`, `mapping`,
  `provider` or `input`, and the repair steps from ADR 0020 apply. The setting
  `simulate_failure` rehearses each one on the stand-in.
- **Secrets.** OAuth tokens, API keys and app passwords live in the vault, encrypted
  with `EXA_SECRETS_KEY`. They are never returned by the API, written to logs or
  shown again.

## Ready-made apps

The client ids and secrets below are ExaCarib's OAuth apps, set on the server. The
OAuth redirect is `{EXA_PUBLIC_URL}/api/v1/commai/oauth/{app}/callback`.

| App | Category | Sign-in | Reads | Changes | Webhooks in | Server settings |
| --- | --- | --- | --- | --- | --- | --- |
| Salesforce (`salesforce`) | CRM | OAuth web server flow (`api refresh_token`); per business `login_host` | find_contact, search_contacts | create_contact, update_contact, create_lead, create_deal (Opportunity), create_ticket (Case) | through a generic inbound webhook (Flow or Apex) | `EXA_SALESFORCE_CLIENT_ID`, `EXA_SALESFORCE_CLIENT_SECRET` |
| HubSpot (`hubspot`, extended) | CRM | OAuth, or a private-app token by secure entry | find_contact | create_contact, create_lead, create_deal, create_ticket, update_contact | X-HubSpot-Signature-v3 (client secret, 5 minutes) | `EXA_HUBSPOT_CLIENT_ID`, `EXA_HUBSPOT_CLIENT_SECRET` |
| Microsoft Dynamics 365 (`dynamics365`) | CRM | Entra ID OAuth, scope `https://{org_host}/user_impersonation` | find_contact | create_contact, update_contact, create_lead, create_deal, create_ticket (incident) | through a generic inbound webhook | `EXA_DYNAMICS_CLIENT_ID`, `EXA_DYNAMICS_CLIENT_SECRET` |
| Zoho CRM (`zoho_crm`) | CRM | Zoho OAuth per data centre (`dc`), keeps `api_domain` | find_contact | create_contact, update_contact, create_lead, create_deal | notification token in the body | `EXA_ZOHO_CLIENT_ID`, `EXA_ZOHO_CLIENT_SECRET` |
| Pipedrive (`pipedrive`) | CRM | Pipedrive OAuth (Basic client auth), keeps `api_domain` | find_contact | create_contact, update_contact, create_lead, create_deal | HTTP Basic credentials on the webhook | `EXA_PIPEDRIVE_CLIENT_ID`, `EXA_PIPEDRIVE_CLIENT_SECRET` |
| Gmail (`gmail`) | Email | Google OAuth (`gmail.send`, `gmail.readonly`) | find_messages | send_email | Pub/Sub push with a token in the address | `EXA_GOOGLE_CLIENT_ID`, `EXA_GOOGLE_CLIENT_SECRET` |
| Microsoft 365 / Outlook (`microsoft365`) | Calendar and email | Entra ID OAuth (Graph, delegated) | find_slots, find_messages | book, send_email, cancel | Graph change notifications (validationToken, clientState) | `EXA_MS365_CLIENT_ID`, `EXA_MS365_CLIENT_SECRET` |
| Google Calendar (`google_calendar`, phase 2) | Calendar | Google OAuth | find_slots | book, cancel | none | `EXA_GOOGLE_CLIENT_ID`, `EXA_GOOGLE_CLIENT_SECRET` |
| Calendly (`calendly`) | Calendar | Calendly OAuth, keeps owner and organisation | list_event_types, find_events | create_booking_link (single-use link), cancel_event | Calendly-Webhook-Signature (`t=`, `v1=` HMAC) | `EXA_CALENDLY_CLIENT_ID`, `EXA_CALENDLY_CLIENT_SECRET` |
| CalDAV calendar (`caldav`) | Calendar | user name and app password (secure entry) | find_slots | book (with a signed .ics invite link), cancel | none (CalDAV has no push) | none |
| CardDAV contacts (`carddav`) | Contacts | user name and app password | find_contact, sync_contacts | create_contact, update_contact, delete_contact | none (sync-collection) | none |

How each app avoids creating twice on a retry:
- **Salesforce:** a SOSL search for the reference.
- **Dynamics:** a PATCH to our own uuid5 with `If-None-Match: *`.
- **Zoho:** upsert with duplicate check fields, then COQL by reference.
- **Pipedrive:** a reference in the title, and a person search.
- **Gmail:** a fixed Message-ID, and an `rfc822msgid:` search.
- **Graph:** `transactionId`, and an extended property in Sent Items.
- **Calendly:** a recorded link.
- **CalDAV:** `PUT` with `If-None-Match: *` (412 means it already exists).
- **CardDAV:** ETags.

Helpdesk, chat, commerce, payments and file apps (Zendesk, Freshdesk, ServiceNow,
Slack, Teams, Shopify, Stripe, Google Drive, OneDrive/SharePoint, and the Zapier,
Make and n8n apps) are built by a separate piece of work. They use the same kit and
appear in the same catalogue.

## Standard interfaces

| Standard | What it gives | Where |
| --- | --- | --- |
| CalDAV and iCalendar (RFC 4791, 5545, 5546) | Any CalDAV calendar; a bookings feed at a secret address; signed `.ics` invites (REQUEST, CANCEL) | `connectors/caldav.py`, `standards/ical.py`, `standards/ical_feed.py` |
| CardDAV and vCard (RFC 6352, 6350) | Any address book, with sync-collection; vCard import and export | `connectors/carddav.py`, `standards/vcard.py` |
| IMAP and SMTP (RFC 9051, 2177, 6409) | Any mailbox as an email channel account (provider `mailbox`): new mail by polling or IDLE, replies by SMTP | `channels/mailbox.py` |
| CloudEvents 1.0 and Standard Webhooks | Event webhooks in either format, signed with ExaCarib v1 and optionally the Standard Webhooks headers; signed inbound webhooks that start workflows | `standards/webhooks_std.py`, `standards/inbound.py` |
| OpenAPI 3.0 and 3.1 | A business's own REST API as an app (below) | `standards/openapi_import.py`, `connectors/rest_generic.py` |
| OpenAPI 3.1 and AsyncAPI 3.0 documents | CommAI's own endpoints and event stream, at `/api/v1/commai/openapi.json` and `/api/v1/commai/asyncapi.json`, checked by tests | `standards/api_docs.py` |
| CSV (RFC 4180) | Contacts in and out; conversations out (one row per message) | `standards/exchange.py` |

### Inbound webhooks (Zapier, Make, n8n, anything)

`POST /customers/{id}/inbound-hooks` returns an address and a secret, shown once. A
delivery must carry either the Standard Webhooks headers (`webhook-id`,
`webhook-timestamp`, `webhook-signature`) or `X-ExaCarib-Timestamp` and
`X-ExaCarib-Signature`, within five minutes. The body may be JSON or a CloudEvent
(structured or binary). Each delivery is accepted once, by its id, and records
`inbound_webhook.received`, which workflows can start from. Apps with their own
signatures use `POST /integrations/{app}/hook`, and each delivery records
`integration.event`.

### A business's own REST API

1. `POST /rest-apps` with the OpenAPI document (JSON, or YAML when PyYAML is
   installed). CommAI only reads the document. Remote `$ref`s are refused.
2. `POST /rest-apps/{id}/draft` suggests actions and field mapping. A suggestion can
   only name operations that are in the document.
3. `PUT /rest-apps/{id}` saves the chosen operations, the field mapping and the
   sign-in method. The sign-in must be a scheme the document declares: API key
   (header or query), Basic, bearer, OAuth 2.0 client credentials, or OAuth 2.0
   authorisation code with the business's own client. Any change sends the app back
   to draft.
4. `POST /rest-apps/{id}/approve`, by a person (an API key cannot approve). The app
   then appears as `rest_<id>` in that business's catalogue only.

Calls go only to the public HTTPS base address, checked like webhook addresses. Path
values are escaped. Writes carry `Idempotency-Key`. Go-live is a single capability,
`integration-rest`. Until it is on, or until the business signs in, the app runs on
a stand-in built from the document's own examples. An API that needs no sign-in has
no stand-in: its actions are refused until `integration-rest` is on.

### Mailbox (IMAP and SMTP)

Create an email channel account with provider `mailbox`, then
`PUT /channel-accounts/{id}/mailbox` with the IMAP and SMTP servers, user name and
password (kept in the vault). Go-live: `integration-mailbox`. Until it is on, nothing
is read and replies go to the simulated outbox. New mail is found by UID and
UIDVALIDITY. The first read starts from the newest message, not from history.
Out-of-office replies are skipped. For IDLE mode, run
`python -m exaconnect_controller.commai.channels.mailbox <account_id>` as a service.

### Apps reached through a standard

These have no ready-made app here:
- **Through CalDAV or CardDAV:** iCloud, Fastmail and Nextcloud.
- **Through the mailbox:** Zoho Mail, Yahoo Mail and any mailbox.
- **Through your own API (OpenAPI):** Acuity Scheduling, Square Appointments,
  SugarCRM, Odoo, Copper, Insightly, Keap, ActiveCampaign, Mailchimp, Airtable,
  Google Sheets and Notion.
- **Through webhooks:** Zapier, Make and n8n. CommAI's app definitions for them are
  in `integrations/zapier`, `integrations/make` and `integrations/n8n`. They use the
  event webhooks (CloudEvents and Standard Webhooks) out, and inbound webhooks in.

A ready-made app that receives webhooks can act on each verified, first-time event
by implementing `on_webhook_event(conn, connection, event)`. It is a no-op on the kit,
and a failure inside it never loses the recorded event.

## What Dudley sets up (placeholders in `.env.example`)

| Setting | For |
| --- | --- |
| `EXA_SECRETS_KEY` | the vault (all credentials) |
| `EXA_PUBLIC_URL` | OAuth redirects, webhook addresses, the calendar feed and invite links |
| `EXA_SALESFORCE_CLIENT_ID`, `EXA_SALESFORCE_CLIENT_SECRET` | Salesforce connected app |
| `EXA_HUBSPOT_CLIENT_ID`, `EXA_HUBSPOT_CLIENT_SECRET` | HubSpot public app; the secret also checks webhook signatures |
| `EXA_DYNAMICS_CLIENT_ID`, `EXA_DYNAMICS_CLIENT_SECRET` | Entra ID app for Dynamics 365 |
| `EXA_ZOHO_CLIENT_ID`, `EXA_ZOHO_CLIENT_SECRET` | Zoho API console client (server-based) |
| `EXA_PIPEDRIVE_CLIENT_ID`, `EXA_PIPEDRIVE_CLIENT_SECRET` | Pipedrive Marketplace app |
| `EXA_GOOGLE_CLIENT_ID`, `EXA_GOOGLE_CLIENT_SECRET` | Google OAuth app for Gmail and Calendar (Gmail needs Google's restricted-scope verification) |
| `EXA_MS365_CLIENT_ID`, `EXA_MS365_CLIENT_SECRET` | Entra ID multi-tenant app for Outlook mail and calendar (shared with OneDrive/SharePoint) |
| `EXA_CALENDLY_CLIENT_ID`, `EXA_CALENDLY_CLIENT_SECRET` | Calendly developer app |

Then, for each app, an ExaCarib admin records its go-live criteria at
`/api/v1/commai/golive` and switches it to pilot (named businesses) or on. Each app
is tested on a provider sandbox or test account first. All of these are free
developer accounts. Nothing here costs money until a business uses a paid plan of
its own.
