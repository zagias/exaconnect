# ADR 0034: CommAI integration catalogue

Status: accepted, 7 October 2026

## Context

Dudley asked for "all integration interfaces which the platform would allow". He
asked for them as standard interfaces first, plus ready-made connectors for the
popular platforms. The constraints:
- Nothing may cost money.
- No real accounts exist yet.
- No test may call a real provider.
- Anything specific starts off, and ExaCarib switches it on only when its written
  criteria are met (ADR 0028).

ADR 0020 gave us one connector interface, the action service, the setup flow
(draft → authorised → testing → live) and repair causes, with two connectors.

## Decision

1. **A connector kit** (`connectors/kit.py`).
   - Each app is one file: its endpoints, OAuth provider, error mapping, pagination,
     idempotency technique, webhook verification and **stand-in**.
   - The kit handles the go-live gate, simulated routing, the rate-limit policy
     (wait inline at most twice for a `Retry-After` of 5 s or less, otherwise a
     provider error), health and descriptions.
   - Registering an app declares `feature / integration-<app>` with its criteria.
2. **Stand-ins, not mocks.**
   - Until an app's OAuth client is on the server and its capability is on, a
     connection runs with `auth_method = 'simulated'`. Its calls go to an in-process
     copy of the provider's API, which answers in the provider's shapes and keeps
     records per business.
   - Tests point the fake HTTP layer at the same stand-ins, so the real request
     path (URLs, headers, auth) is what gets exercised.
   - Moving to the real app resets approval and test results.
3. **Standards first.** CommAI speaks:
   - CalDAV and CardDAV (with iCalendar, vCard and sync-collection);
   - IMAP and SMTP, as a mailbox provider on the existing email channel rather than
     a second email system;
   - CloudEvents 1.0 and Standard Webhooks, both outbound per endpoint and inbound;
   - OpenAPI 3 import for a business's own API;
   - CSV.
   CommAI also publishes OpenAPI 3.1 and AsyncAPI 3.0 descriptions of itself.
   Popular apps without a ready-made app are listed with the standard that
   reaches them.
4. **A business's own API is data, never code.**
   - CommAI reads the document and lists its operations. A business chooses some
     as actions and maps fields.
   - A person (not an API key) approves. Any change returns the app to draft.
   - Actions can only name operations that are in the document.
   - Remote `$ref`s are refused. Base addresses must be public HTTPS, and path
     values are escaped.
   - There is one capability, `integration-rest`, for the generic connector rather
     than one per business API.
5. **The catalogue is data-driven.** `GET /integration-catalogue` groups the
   registry by category, and the portal page draws from it. A new connector file
   plus one line in `connectors/installed.py` puts it in the catalogue.

## Consequences

- Every app can be tried end to end, on example data, before any account exists.
  Going live is a server setting plus an admin's go-live record, with no deploy.
- Webhook signature schemes differ per provider and are verified in each app's
  file:
  - HubSpot v3 HMAC;
  - Calendly `t=`/`v1=`;
  - Graph `clientState` and its validation handshake;
  - Pipedrive Basic credentials;
  - the Zoho token;
  - the Gmail push token.
  Salesforce and Dynamics reach CommAI through generic inbound webhooks.
- The stand-ins follow the documentation, not observed behaviour. Each app's
  `live-test` criterion requires a real sandbox run before go-live.
- Not built here:
  - importing conversation history;
  - an LLM-drafted REST mapping (the draft is rules-based and limited to the
    document);
  - pushing `.ics` invite links from Google Calendar and Microsoft 365 bookings.
    Graph sends its own invitations, and Google Calendar belongs to phase 2 code.
    Confirmation text can still use `{invite_url}` with CalDAV.

## Alternatives considered

- **A per-provider SDK.** Rejected: it adds dependencies, and the SDKs hide the
  HTTP we need to control for idempotency and rate limits.
- **iPaaS only (Zapier and similar).** Rejected as the only route: it is paid, and
  it puts customer data in a third party. It is still supported through webhooks.
- **Running generated client code from an OpenAPI document.** Rejected for
  security reasons.
