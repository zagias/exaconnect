# ADR 0025: Partners, white-label, regional hosting and the developer platform

Status: accepted, 7 October 2026

## Context

Phase 3 of CommAI adds partners (resellers and managed-service providers) who
look after several businesses, white-label branding for them, regional hosting,
and the developer platform pieces the scope lists: OAuth for partner apps,
sandbox keys, an API version and deprecation policy, and a website chat SDK.
Every existing tenant check must keep holding.

## Decisions

### Partners act through delegate accounts

A partner asks to manage a business, naming the scopes it wants. Someone at the
business with `commai:admin` accepts, choosing which of those scopes to grant,
or declines; they can narrow or revoke at any time. One partner manages a
business at a time.

When a partner person switches into a business, they get a session for a
**delegate account** of that business: role `customer`, `customer_id` of that
business, `access_scopes` set to the granted scopes, and an unusable password.

Options considered:

1. A new `partner` role and a "which customer am I acting for" field on every
   request, checked in `deps.py` and every router.
2. Delegate accounts (chosen).

Option 2 reuses every existing check unchanged (customer isolation, scopes,
notes, seats, the Connect API gate for limited accounts), so a partner can
never reach a business it is not linked to, or do more than it was granted,
without any router knowing partners exist. Revoking disables the delegates,
ends their sessions and revokes their keys and OAuth grants. Delegates can
never accept, change or revoke links, approve OAuth apps, or switch back with
an API key. Their audit actor is `user:<email>#partner:<link id>`, and every
switch is audited under the person's own account.

The consolidated view shows, per linked business, a short health summary (only
when `commai:read` or `commai:write` was granted) and this month's usage at
example prices with the partner's markup. Statements record usage and markup
per month for billing; no payment is taken.

### White-label is data, applied at run time

A brand (product name, colour, text colour, support email, logo) belongs to a
partner or, once a partner manages it, one business. The portal fetches
`/commai/branding/current` and sets the colour tokens and title (light mode
only, so dark mode keeps its tuned contrast); the widget gets the brand in its
config. ExaCarib's own look is the default and its brand rules are unchanged.

Checks: colours need 4.5:1 contrast with white (WCAG AA), logos are PNG, JPEG
or WebP only (never SVG), at most 256 KB, bytes matching the type, PNGs at most
2000 px. A partner brand can't use the ExaCarib name.

Custom domains are proved by a DNS TXT record at
`_exacarib-challenge.<domain>` through a `Resolver` interface (simulated by
default; DNS over HTTPS with `EXA_DNS_RESOLVER=doh`). Caddy's on-demand TLS
asks `/api/v1/commai/whitelabel/tls-ask` before issuing a certificate, so only
verified domains get one (`deploy/public/Caddyfile.custom-domains`).

### Regions are go-live capabilities with dependency criteria

Each region is declared in the go-live registry (ADR 0022) with one criterion
per dependency (database, backups, AI model, channels, speech). A database
trigger keeps a dependency criterion unmet unless a provider **in that region**
is recorded through the regions API, whichever API marks it, so the registry's
own "every criterion met" rule blocks switching on. Removing a provider unmeets
the criterion and switches the region off. Only `primary` (the current server)
is real; the others are declared and off. Businesses have a home region
(default `primary`) that data-location reports read.

### OAuth tokens are expiring API keys

Authorisation code with PKCE (S256 only), clients registered by partners,
consent in the portal by a person of the business (not a key, not a delegate,
not an ExaCarib admin). The access token is an `api_keys` row with the granted
scopes and a one-hour expiry, so the existing key path enforces scopes and
tenancy. Refresh tokens rotate; replaying an old refresh token or a used code
revokes the grant. Revocation per RFC 7009, and by the business.

### Sandboxes are separate customers

A sandbox is its own `customers` row (`sandbox_of`), so sandbox keys (owned by
the sandbox's own account) are kept out of the real business by the usual
checks. Database triggers force every sandbox channel account onto the
simulated provider and refuse number and porting orders, whatever the API is
asked. No integration credentials are copied.

### Deprecation

`commai/apipolicy.py` lists deprecated endpoints; their responses carry
`Deprecation` (RFC 9745), `Sunset` (RFC 8594) and a successor `Link`, with at
least six months' notice. The first: `GET .../event-types`, replaced by
`.../event-catalogue`. Policy in `docs/commai/api-policy.md`; changelog at
`/api/v1/commai/changelog`.

## Consequences

- No change to `deps.py` or any existing router's checks; partners inherit them.
- Delegate and sandbox accounts appear as users of the business, with clear
  names, so the business can see who acts in it.
- The region trigger means a mis-recorded criterion fails safe (stays unmet)
  rather than raising an error.
- A future `golive` pre-switch hook could replace the trigger.
