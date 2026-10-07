# ADR 0030: Enterprise administration and data governance

Date: 2026-10-07. Status: accepted.

## Context

Phase 3 of the CommAI scope ("Global expansion") includes enterprise admin.
The scope's "Global readiness" section asks for several locations, brands,
time zones and business calendars per customer; roles; multifactor sign-in;
retention, deletion and export per customer; documented processing locations,
subprocessors and backups; and unusual-use detection, spend caps and account
protection. ADR 0017 left three sign-in items open: a business-level "require
two-step for everyone", sign-out reaching the company's provider, and nothing
limiting where a business's accounts sign in from.

## Decision

**Organisations.** A business has locations (country, time zone, primary
flag), brands, opening hours per location (intervals per weekday), public
holidays per country (entered by the business; no outside feed) and one-off
closures. Teams can be tied to a location and a brand. The inbox's service
targets count business time only (`enterprise/calendar.py`, used by
`inbox._due`): a conversation's calendar is its team's location, else the
primary location. Routing uses it too: while a location is closed, new
conversations go to that location's after-hours team if it has one, or wait
in the team's queue unassigned. A business with no locations, or a location
with no hours, holidays or closures, runs around the clock as before.

**Roles.** Eleven fixed permissions (read inbox, reply, notes, manage teams,
channels, integrations, approve spending, voice admin, view reports, export
data, manage security). Four built-in roles describe today's rights (business
admin, agent, internal, viewer); a business admin builds custom roles and gives
them per person, across the business or for one team. Enforcement is in
`access.check`, which every CommAI endpoint already calls: for a person with
roles, the scope and the endpoint's section map to one permission
(`roles.required`), held business-wide or for the team of the conversation in
the path (or the team a list is filtered to). Roles only narrow: they never
widen past the account, seat, key scopes or voice permissions. A person with no
roles keeps exactly the rights they had, so existing seats, scoped keys and
SCIM-provisioned accounts keep working unchanged. A change that would leave no
active person able to manage security is refused.

**Security settings.** Per business: require two-step sign-in, session
lifetime, an IP allow-list (CIDR) and SSO sign-out. They are checked on every
request (`security.guard`, called from `api/deps.current_user`) as well as at
sign-in, so tightening a setting applies to sessions already open.
- Require two-step: a person without it still signs in with their password,
  but the session reaches only `/auth/two-step`, `/auth/passkeys`, `/auth/me`
  and `/auth/logout` until they have set it up; the portal shows the set-up
  page. Enterprise SSO sessions are exempt (the company's provider does the
  second step). The admin turning it on must have two-step first.
- Session lifetime: the shorter of the server default and the business's
  hours, measured from when each session started.
- IP allow-list: applies to the portal and to API keys of the business's
  accounts; ExaCarib admins are exempt. Saving a list that does not contain
  the saving admin's own address is refused (lock-out guard). Blocked sign-ins
  are audited as failed sign-ins (`ip_not_allowed`). The client address is the
  first `X-Forwarded-For` entry, as for the sign-in throttle; this is safe
  only because the controller listens on localhost behind the public proxy,
  which replaces that header.
- SSO sign-out: the gateway's ID token is kept with the session (server side,
  deleted with it). Logout then returns `X-Exa-Logout-Url`, the gateway's
  OpenID Connect RP-initiated logout (`end_session_endpoint`, `id_token_hint`,
  the realm's post-logout address), and the portal goes there. Keycloak passes
  the logout on to a brokered provider that publishes one (OIDC
  `end_session_endpoint`, SAML `SingleLogoutService`). The Security tab says
  per connection whether that is known (SAML metadata uploaded) or depends on
  what the provider publishes.
- An audit view per business: sign-ins, failed sign-ins (including those
  logged against an email before the account was known) and setting changes.

**Data governance.** Retention per category (messages, notes, call
recordings, transcripts, AI logs; days or keep). A durable job runs it once a
day per business (and on request): message and transcript text is blanked
(`messages.redacted_at`; the message row stays so reports still match the
recorded events), notes and AI runs are deleted, call records lose their
recording reference. Nothing linked to a contact under legal hold is touched;
a business-wide hold stops the job. Each run records its cut-offs, what it
changed and what it held back. Subject requests: export everything about one
contact (JSON or ZIP with their files; private notes only when asked for by
someone who may read notes), anonymise (keep conversation shells, remove what
identifies them) or delete (contact and conversations); hold blocks both and
the refusal is recorded. Every query is keyed on the business and the contact.
A full business export is built by a job into a ZIP (one JSON file per table,
secret columns stripped), kept seven days. The processing page is generated
from configuration: the controller and database, the gateway, the AI model
(DeepInfra only when its key is set), call speech, each channel account and
integration, the SIP provider, backups; each marked live, simulated or not
set up, quoting providers' own published locations as unverified.

**Abuse and account protection.** A durable job (every five minutes) looks
for many failed sign-ins, a spike in messages sent and a jump in any usage
meter, each against the business's previous week. API keys record where they
are used from; a new address (or a new country when a GeoIP database is
configured) raises an alert, and five new addresses in ten minutes locks the
key for an hour (an admin can unlock it). Alerts are raised once (dedupe key),
shown in the Security tab, sent as the `security.alert` event (webhooks) and
listed for ExaCarib at `/api/v1/commai/enterprise/alerts`. Thresholds can be
tuned per business. Hard spend caps reuse usage limits: a hard monthly limit on
`message_out:<channel>`, or on `message_out` for all channels together, now
stops sending, checked when the reply is written and again when it is sent.

## Consequences

- Countries are unknown until Dudley supplies a GeoIP database
  (`EXA_GEOIP_DB`, MaxMind format, and the `geoip2` reader); until then only
  new addresses are reported. MaxMind's free GeoLite2 needs an account.
- "Spend" jumps are measured in metered units, not money, until a price list
  exists (README, "What waits on Dudley").
- The call recording itself lives with the SIP provider (simulated today);
  retention removes the reference only. The real adapter must delete the
  provider's copy.
- The SSO sign-out path is written from Keycloak's and the OIDC RP-initiated
  logout documentation and tested with a fake issuer, not against a live
  Keycloak.
- Public holidays are typed in by each business; we ship no holiday data,
  because a wrong holiday silently moves every target.
- Processing locations are only as good as `EXA_HOSTING_REGION` and the
  providers' own statements; the page says so.
