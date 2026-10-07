# Partners, white-label, regions and the developer platform

Decision record: ADR 0025. Code: `commai/partners.py`, `branding.py`,
`regions.py`, `oauth.py`, `sandbox.py`, `apipolicy.py`, routers in
`commai/api/partners.py`, `regions.py`, `developer.py`, SQL in
`commai/sql/62_partners.sql`. Portal: CommAI > Partners (or Partners and apps),
Manage > Go-live, and the consent page `/commai/oauth/authorize`.

## Partners

1. An ExaCarib admin creates the partner (`POST /commai/partners`: name, kind
   `reseller` or `msp`, markup %) and adds its people by email
   (`POST /commai/partners/{id}/members`, role `admin` or `member`). They need
   ordinary customer accounts first (normally of the partner's own business).
2. A partner admin asks to manage a business with its id and the permissions
   wanted (`POST /commai/partners/{id}/links`).
3. Someone at the business accepts in CommAI > Partners and apps, choosing which
   permissions to grant; they can narrow or revoke later. Only the business's
   own people can do this, never the partner.
4. Partner people press "Switch to" (`POST /commai/partners/switch`) and work in
   the business with only what it granted; "Back to <partner>" returns.
5. The partner's screen shows each business's health and usage, and a partner
   admin records monthly statements (usage at example prices plus markup). No
   payments are taken.

## White-label

Partner admins set the product name, brand and text colours (4.5:1 contrast
with white), support email and logo (PNG, JPEG or WebP, 256 KB). Their people
and the businesses they manage see it in the portal; each business's chat
widget uses it unless the business has its own (a managed business may).

### Custom domains: what Dudley does in DNS

For a partner domain such as `portal.partner.com`:

1. The partner adds it under Branding > Custom domains. The screen shows a TXT
   record: name `_exacarib-challenge.portal.partner.com`, value
   `exacarib-verify=<token>`, and a CNAME to `connect.exacarib.com`.
2. The partner (or Dudley, if ExaCarib runs their DNS) publishes both records.
3. Before real lookups: set `EXA_DNS_RESOLVER=doh` in the server `.env`
   (optionally `EXA_DOH_URL`). Until then the simulated resolver is used and an
   ExaCarib admin can publish test records with
   `PUT /commai/whitelabel/simulated-dns`.
4. Press Verify. Once verified, Caddy can issue a certificate.
5. Once, on the server: add the `on_demand_tls { ask ... }` global block shown
   at the foot of `deploy/public/Caddyfile.custom-domains` to the top of
   `deploy/public/Caddyfile`, add `import Caddyfile.custom-domains` at its end,
   and mount the snippet beside it. Caddy then issues
   Let's Encrypt certificates on demand, asking the controller first; it gets a
   yes only for verified domains.

## Regions

Regions are in Manage > Go-live > Regions. Each needs a provider in that region
recorded for the database, backups, AI model, channels and speech, plus the
three base criteria, before it can be piloted or switched on. Today only
`primary` (the current server) is real; `caribbean`, `us-east` and `eu-west`
are declared and off, and nothing is created for them. A business's data
location is at `GET /commai/customers/{id}/data-location`; ExaCarib admins set
a home region (only one switched on for that business).

Optional `.env` facts for the report: `EXA_REGION_LOCATION`,
`EXA_BACKUP_TARGET`, `EXA_BACKUP_REGION`, `EXA_LLM_REGION`.

## OAuth for partner apps

- Register: partner admin, `POST /commai/partners/{id}/oauth-clients`
  (name, redirect URIs over https, scopes; a confidential app gets a secret once).
- Authorise: send the person to
  `https://<portal>/commai/oauth/authorize?response_type=code&client_id=...&redirect_uri=...&scope=commai:read&state=...&code_challenge=<S256>&code_challenge_method=S256`.
- Token: `POST /api/v1/commai/oauth/token` (form) with `grant_type=authorization_code`,
  `code`, `redirect_uri`, `client_id`, `code_verifier` (and the secret for a
  confidential app, in the body or HTTP Basic). Access tokens last an hour;
  `grant_type=refresh_token` rotates both tokens.
- Revoke: `POST /api/v1/commai/oauth/revoke` with `token` and `client_id`. The
  business can also revoke under Connected apps.

## Sandboxes

CommAI > Partners and apps > Sandbox makes a copy of the business. Sandbox keys
(`exa_sbx_...`) act only on the sandbox id. Every channel there is simulated
(enforced in the database), and numbers and porting can't be ordered.

## Website chat SDK

```html
<script>
  window.ExaCaribChat = window.ExaCaribChat || { q: [] };
  ["on", "open", "close", "identify"].forEach(function (m) {
    window.ExaCaribChat[m] = window.ExaCaribChat[m] || function () { window.ExaCaribChat.q.push([m].concat([].slice.call(arguments))); };
  });
  ExaCaribChat.on("message", function (m) { console.log("reply", m.body); });
</script>
<script src="https://HOST/api/v1/commai/widget/v1.js" data-key="wk_..." async></script>
```

| Call | What it does |
| --- | --- |
| `open()`, `close()`, `isOpen()` | Show or hide the chat; whether it is shown |
| `identify(token)` | A signed-in customer: an HS256 token your server signs with the widget secret (`sub`, `email`, `name`, `exp`) |
| `on(event, fn)`, `off(event, fn)` | `ready`, `open`, `close`, `message` (from the team or assistant), `sent`, `identified`, `error`. Also dispatched on `window` as `exacarib-chat:<event>` |

API policy and deprecations: `docs/commai/api-policy.md`.
