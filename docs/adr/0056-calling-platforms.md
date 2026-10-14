# ADR 0056: calling platforms (Microsoft Teams, Zoom Phone and others)

Status: accepted, 2026-10-14

## Context

Organisations already make calls in Microsoft Teams, Zoom or Webex, and run contact
centres on Genesys Cloud, NICE CXone or Talkdesk. Dudley asked for connectors so
calling in these applications joins Jibsy: people keep their app, and Jibsy supplies
the numbers, the extensions, the carriers and the bill. All seven platforms let an
outside SIP border controller carry their calls ("bring your own carrier"):

| Platform | Name for it | How it connects |
| --- | --- | --- |
| Microsoft Teams | Direct Routing | mutual TLS + SRTP to `sip*.pstnhub.microsoft.com`; IPs fixed by Microsoft |
| Zoom Phone | BYOC (Premises Peering) | TLS or UDP to the addresses Zoom gives the account |
| Webex Calling | Local Gateway, certificate based | TLS + SRTP to the addresses Webex gives |
| Google Voice | SIP Link | TLS + SRTP to Google's addresses |
| Genesys Cloud | BYOC Premises | SIP trunk to the org's Genesys edge addresses |
| NICE CXone | BYOC | SIP trunk to the addresses NICE gives |
| Talkdesk | BYOC | SIP trunk to the addresses Talkdesk gives |

## Decision

- **One link per platform per organisation** (`voice_platform_links`), on Phone ›
  Teams and Zoom. A link is also a row in `voice_carriers` with a `platform` column,
  so it reuses the carrier machinery (Kamailio dispatcher sets, address allow-list,
  simulated mode, failure modes, max channels) but never shows as a trunk and is
  never used for trunk routing.
- **Its own SIP address**: `<organisation>-<platform>.sip.exacarib.com`
  (`EXA_PLATFORM_SIP_DOMAIN`). For Teams this is Microsoft's carrier hosting model:
  ExaCarib's tenant owns the base domain, each customer tenant adds its subdomain
  (the customer pastes Microsoft's TXT value; ExaCarib staff publish it under
  Operations, `/voice-admin/platform-dns`). Kamailio treats the whole domain as its
  own (corex `alias_subdomains`) and presents the organisation's address in
  Record-Route and Contact, as Microsoft requires.
- **People, not trunks**: for Teams, Zoom, Webex and Google, a person (their sign-in)
  gets a Jibsy number and optionally an extension. Calls to that number or
  extension ring them in their app; from their app they call extensions, other
  organisations' numbers and the outside world, showing their own number. For
  contact-centre platforms the "people" are destinations (queues) instead.
- **Trust**: a call to an organisation's platform address is accepted only from that
  platform's signalling addresses (address list tagged with the platform name), the
  link key travels to FreeSWITCH in `X-Exa-Link` (any copy from outside is removed),
  and FreeSWITCH asks the controller (`/internal/voice/authorise-platform`, signed)
  that the caller is that link's person before an outside call, which is then billed
  per minute like any other.
- **Emergency** numbers dialled in a platform go out on ExaCarib's carriers with the
  location's callback number, as for trunks (ADR 0044).
- **Guide**: each link shows its set-up steps; Teams gets ready-to-paste PowerShell
  (gateway, PSTN usage, voice route, routing policy, dial plan, and a line per person).
- **Simulated by default**: until the voice admin switches a link to live, Jibsy
  answers calls to platform people with an echo and test calls run against the
  simulated platform (also while it refuses calls or is down), so an organisation
  can set everything up before its platform account is ready.

## What is not done, and needs Dudley

- DNS: `*.sip.exacarib.com` pointing at the voice host.
- A publicly trusted certificate covering `*.sip.exacarib.com` (Microsoft and the
  others reject self-signed ones). A wildcard certificate may cost money.
- Teams: ExaCarib's own Microsoft 365 tenant with `sip.exacarib.com` verified and a
  Teams Phone licence, then each customer's tenant adds its subdomain.
- Each platform account with BYOC enabled (Zoom, Webex, Google, Genesys, NICE,
  Talkdesk). Cisco and Google certify only listed border controllers; Kamailio with
  FreeSWITCH works but is not on their lists.

## Tests

`controller/tests/test_commai_voice_platforms.py`; Kamailio flows checked with a
SIP script; `lab/ci/checks/voice-platforms.sh` runs `deploy/voice/uat/platforms.mjs`
on every deploy: join Teams, add a person, read the guide, test calls both ways and
while refused, send the TXT value, join Zoom with its addresses, and a real browser
phone call to the Teams person that the simulated link echoes back.
