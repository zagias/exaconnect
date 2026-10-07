# Jibsy for Zapier

A Zapier Platform (CLI) app that lets a business connect Jibsy by ExaCarib
to the apps it already uses in Zapier. It talks only to the public Jibsy API
(`/api/v1/commai/customers/{customer_id}/...`), so it can do nothing a Jibsy
API key cannot already do.

## What it offers

| Kind    | Name                 | Jibsy endpoint                                             |
| ------- | -------------------- | ----------------------------------------------------------- |
| Trigger | New Event (REST hook) | `POST /webhooks` to subscribe, `DELETE /webhooks/{id}` to unsubscribe, `GET /events` for samples |
| Create  | Create Contact       | `POST /contacts`                                            |
| Create  | Start Conversation   | `POST /conversations`                                       |
| Create  | Send Message         | `POST /conversations/{conversation_id}/messages`            |
| Create  | Add Internal Note    | `POST /conversations/{conversation_id}/notes`               |
| Create  | Propose Action       | `POST /actions` (sensitive actions wait for approval in Jibsy) |
| Search  | Find Contact         | `GET /contacts?q=`                                          |

Sign-in is a Jibsy API key (`exa_...`) and the portal address; the app reads
the business from `GET /api/v1/auth/me`. Every delivery from Jibsy is checked
against the `X-ExaCarib-Signature` header (HMAC-SHA256 of
`<timestamp>.<body>` with the endpoint's secret, five-minute window) before a
Zap runs.

A test in `controller/tests/test_commai_automation_apps.py` checks that every
endpoint used here exists in the Jibsy OpenAPI schema.

## Publishing (Dudley)

1. Create a Zapier developer account and install the CLI: `npm install -g zapier-platform-cli`.
2. In this folder: `npm install`, `zapier login`, `zapier register "Jibsy by ExaCarib"`, then `zapier push`.
3. Invite a test business with `zapier users:add`, try each step, then apply for public listing in the Zapier developer platform.

Nothing here holds a key or a password; each business types its own API key in Zapier.
