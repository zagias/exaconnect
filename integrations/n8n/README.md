# CommAI for n8n

An n8n community node package (`n8n-nodes-exacarib-commai`) with two nodes and
one credential type. It uses only the public CommAI API.

| Node / operation          | CommAI endpoint                                             |
| ------------------------- | ----------------------------------------------------------- |
| CommAI Trigger            | `POST /webhooks` on activate, `DELETE /webhooks/{id}` on deactivate, `GET /webhooks` to check, `GET /event-types` for the list |
| Create Contact            | `POST /contacts`                                            |
| Find Contacts             | `GET /contacts?q=`                                          |
| Start Conversation        | `POST /conversations`                                       |
| Send Message              | `POST /conversations/{conversation_id}/messages`            |
| Add Internal Note         | `POST /conversations/{conversation_id}/notes`               |
| Propose Action            | `POST /actions` (sensitive actions wait for approval)       |

Paths are relative to `/api/v1/commai/customers/{customer_id}`; the business
comes from `GET /api/v1/auth/me` with the API key. The trigger keeps the
endpoint's signing secret in the workflow's static data and checks every
delivery's `X-ExaCarib-Signature` (HMAC-SHA256 of `<timestamp>.<body>`,
five-minute window) before the workflow runs.

A test in `controller/tests/test_commai_automation_apps.py` checks that every
endpoint used here exists in the CommAI OpenAPI schema.

## Building and publishing (Dudley)

1. `npm install && npm run build` in this folder.
2. To try it on a self-hosted n8n: copy the package into `~/.n8n/custom/` or install it from Settings, Community nodes.
3. To publish: create an npm account, `npm publish`, then submit the package for n8n's verified community nodes.

No key or password is stored here; each business enters its own API key in n8n.
