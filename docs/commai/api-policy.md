# Jibsy API: versions, deprecation and changes

This is the published policy for the Jibsy and Connect API (ADR 0031). The same
text is served as JSON at `GET /api/v1/commai/api-policy`.

## Versions

- The version is in the path: `/api/v1`.
- Within a version, changes only add: new endpoints, new optional request
  fields, new response fields, new event types. Clients should ignore fields
  they don't know.
- A breaking change (removing or renaming a field or endpoint, changing a
  meaning) needs a new endpoint or a new version. The old one is deprecated first.

## Deprecation

- A deprecated endpoint keeps working for **at least six months**.
- Every response from it carries:
  - `Deprecation: @<unix time>`: when it was deprecated (RFC 9745).
  - `Sunset: <HTTP date>`: when it stops working (RFC 8594).
  - `Link: <successor>; rel="successor-version", </api/v1/commai/api-policy>; rel="deprecation"`.
- It is marked `deprecated` in the OpenAPI schema (`/api/v1/openapi.json`).
- It is listed in the changelog and in `GET /api/v1/commai/api-policy`.

| Endpoint | Deprecated | Sunset | Use instead |
| --- | --- | --- | --- |
| `GET /api/v1/commai/customers/{customer_id}/event-types` | 7 October 2026 | 7 April 2027 | `GET .../event-catalogue` |

## Changelog

`GET /api/v1/commai/changelog` (no sign-in) lists every change with its date
and kind (`added`, `deprecated`, `removed`). It is kept in
`controller/exaconnect_controller/commai/apipolicy.py`.

## Keys, apps and sandboxes

- **API keys** act as the person who made them, limited to their scopes.
- **OAuth apps** (made by partners) get a one-hour access token and a rotating
  refresh token after a person at the business approves them. See
  `docs/commai/partners.md`.
- **Sandbox keys** (`exa_sbx_...`) act only in the business's sandbox, where
  every provider is simulated and nothing reaches real people.
