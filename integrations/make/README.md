# CommAI for Make

`app.json` describes a Make custom app for ExaCarib Connect CommAI: one section
per part of Make's app editor (base, connection, webhooks, rpcs, modules).
It uses only the public CommAI API.

| Module              | Kind            | CommAI endpoint                                  |
| ------------------- | --------------- | ------------------------------------------------ |
| Watch events        | instant trigger | `POST /webhooks` (attach), `DELETE /webhooks/{id}` (detach) |
| Watch events (polling) | trigger      | `GET /events?type=&after=`                       |
| Create a contact    | action          | `POST /contacts`                                 |
| Find contacts       | search          | `GET /contacts?q=`                               |
| Start a conversation | action         | `POST /conversations`                            |
| Send a message      | action          | `POST /conversations/{conversation_id}/messages` |
| Add an internal note | action         | `POST /conversations/{conversation_id}/notes`    |
| Propose an action   | action          | `POST /actions` (sensitive actions wait for approval) |

The connection checks the API key with `GET /api/v1/auth/me` and keeps the
business id it returns. Paths are relative to
`/api/v1/commai/customers/{customer_id}`.

Make's webhooks cannot run code, so the `X-ExaCarib-Signature` header is not
checked there; the webhook address Make gives is long and unguessable, and a
business that needs signed deliveries checked should use the polling trigger,
Zapier or n8n instead.

A test in `controller/tests/test_commai_automation_apps.py` checks that every
endpoint used here exists in the CommAI OpenAPI schema.

## Publishing (Dudley)

1. Create a Make account and open Custom apps, Create a new app.
2. Paste each section of `app.json` into the matching tab (Base, Connection, Webhooks, RPCs, Modules).
3. Try it with a test business, then ask Make to review the app for public use.

No key or password is stored here; each business enters its own API key in Make.
