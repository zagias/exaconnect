# ADR 0037: CommAI self-service: My settings for staff, a help centre for customers

Date: 2026-10-07. Status: accepted.

## Context

Dudley asked for "the self service portal for end users". CommAI has two kinds
of end user: a business's staff (people with a CommAI seat), and the
business's own customers. Staff already had pieces of self-service scattered
around (voice self-service, ADR 0021; two-step and API keys on the Account
screen, ADR 0017). Customers could reach a business only through its channels
and website chat (ADR 0018). Phase 3 also has other workers on white-label
branding, languages and enterprise roles, so this part must read what exists
and leave clean seams.

## Decision

**My settings** (`/commai/me`, API `/customers/{id}/me/...`) is one page for a
person's own profile and portal language, the languages they answer in,
notifications (new assignment, mention, service-target warning; in the portal
and by email; quiet hours), availability, calls, devices and sessions, and a
link to API keys.

- *Own by construction.* No endpoint takes a user id: the person is the
  session's. A user id smuggled into a request body is ignored. Names from a
  company directory (SCIM) can't be changed here.
- *Availability feeds routing.* Online, away or offline is stored with the
  preferences and written to `commai_members.available`, which routing already
  reads; only online people get new conversations.
- *Notifications* are preferences plus an in-portal list built from recorded
  events (assignment events, notes that mention the person, their own
  conversations near a service target). `staff.should_notify()` applies the
  switches and quiet hours (quiet hours hold back email only). Sending staff
  emails is not wired: no staff mail sender exists yet.
- *Calls* reuse voice self-service unchanged, plus one new endpoint that issues
  a fresh sign-in link for the person's own softphone (the voice admin's
  device link endpoint stays admin-only). The link is the QR code's text; the
  portal does not draw QR codes (no library in the portal; adding one is a
  small follow-up).
- *Sessions* lists the person's own portal sessions, ends one, or ends all but
  the current one. API keys can't do this.

**The help centre** (`/help/<slug>` in the portal app, outside the staff
sign-in and shell; API `/api/v1/commai/help/<slug>/...`), one per business.

- *Off until switched on* by a business admin on CommAI Settings → Help centre,
  who chooses what shows (articles, search, Ask, contact form, hours, channel
  links, sign-in, bookings, data requests), the address, title, colour, hours
  and channel numbers, and previews it. Not a country-, channel-, language- or
  carrier-specific capability, so it is not in the go-live registry.
- *Branding through one function*, `selfservice.branding.branding()`: the
  business name and a colour (the help centre's, else its website chat's,
  else ExaCarib blue; a colour white text can't sit on at 4.5:1 falls back).
  White-label replaces that one function's source.
- *Articles are published, not just approved.* `help_articles` records a
  person publishing an approved knowledge source. A source edited (or
  re-approved) after publishing is hidden until published again, because the
  public query requires `published_at >= source.updated_at`. Unapproved
  sources, notes and AI data are never read by the public functions.
- *Ask* is the customer AI agent itself (`agent.respond`), with all its rules:
  approved knowledge only, escalation when unsure, no confirmation of actions
  in its own words. The question becomes a website-chat conversation, so staff
  see it. An anonymous question the AI couldn't answer is closed with a
  pointer to the contact form (nobody can reply to an anonymous visitor); the
  knowledge gap is still recorded. Ask is hidden when the business runs
  "people only" or has the AI agent switched off. It may answer from any
  approved source, as the website chat does, not only published articles.
- *Contact form* opens a website-chat conversation. Without sign-in the typed
  email is recorded unchecked and a note tells staff to reply by email.
- *Rate limits* per client address on Ask, the contact form and sign-in
  requests (`ss_public_hits`).

**End-user sign-in.**

- *Emailed one-time link*: asked for by email address; the answer is always
  the same (no enumeration). A link goes only to an address the business
  already has as an email contact, through the business's email account
  (simulated when it has none or SMTP isn't set). Only the hash is stored;
  it lasts 15 minutes, is used once (checked and marked in one UPDATE), and
  works only on its own business's help centre. At most 3 links per address
  per 15 minutes (further requests get the same answer and no email) and 20
  requests per client (429 for everyone alike). Using the link marks that
  email address verified.
- *The business's own login*: its website signs the same HS256 token as for
  website chat; the customer arrives with `#user_token=...` (a fragment, so it
  isn't sent in server logs) and gets the same verified identity
  (`web:user:<sub>`) as in the chat, so chat history shows too.
- *Session*: a random token, hashed in `ss_enduser_sessions`, 8 hours, in an
  HttpOnly, SameSite=Strict cookie scoped to `/api/v1/commai/help/<slug>`
  (or the `X-Help-Session` header for the business's own apps). Help centre
  writes need `X-Requested-With: exa-help`.

**What a signed-in end user reaches.** A session proves addresses
(identities): the one they signed in with, plus their help-centre address.
Conversations on those addresses only; customer-facing messages only, through
`inbox.messages` and `widget.public_message` (no staff emails, notes, handover
packets or AI runs). Reply to a conversation that isn't closed. Bookings made
on their conversations: rescheduling books the new time through
`actions.propose` first, and only after the calendar confirms it does a job
ask to cancel the old one; "moved" comes only from the action service on
success, and a refusal tells them the old time stands. Cancelling is a
sensitive action, so the business approves it. Contact preferences per
address: opting out uses the channels' own opt-out, so every send (person, AI,
workflow) is refused at the gateway (email still allows replies within a day
of the person's own email, as in ADR 0018); opting back in needs an address the
session proved, and unproved addresses are shown masked. Data requests
(download, delete) are recorded for the business admin, who can download the
person's data (record, addresses and customer-facing messages, no notes) and
mark the request done or refused. No subject-request flow existed to feed.

## Consequences

- Help centre end users are not portal users: they never get a staff session,
  and a session for one business is nothing on another's.
- Rescheduling briefly holds both times until the business approves releasing
  the old one, because cancelling is sensitive in every calendar connector.
- Deleting a person's data is a manual step for the business admin for now;
  an erase that keeps the audit trail belongs with the enterprise data work.
- The pages were type-checked and built, and the API is tested; they were not
  opened in a real browser in this build.
