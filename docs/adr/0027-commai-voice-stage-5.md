# ADR 0027: CommAI voice stage 5: real numbers, porting, emergency addresses, carriers and fraud

Date: 2026-10-07. Status: accepted.

## Context

Phase 3 of the CommAI scope adds "voice stage 5 (real numbers, porting,
emergency addresses)" and "more carriers". Each is switched on only against
written operating and security criteria. Stages 2 to 4 (ADR 0021) built the
phone system, orders and billing with one simulated SIP provider. They left
fields for port orders and emergency addresses.

Dudley has no SIP provider or carrier account. He has no public voice host and
no rate sheets. Nothing here may depend on any of them, and nothing may claim
to be live.

## Decision

**One provider interface, simulated in full.** `voice/provider.py` covers
number search and ordering by country, release, port orders, emergency
address checks, test calls, call records per carrier, SIP OPTIONS and placing
a call through a carrier. The port orders cover submit, status, documents,
reschedule, cancel, activate and roll back. `SimulatedProvider` answers like a
real provider: slow answers are rows in `voice_sim_provider_requests` with a
`ready_at`, and durable jobs poll them. These are `voice.port_poll`,
`voice.port_cutover`, `voice.emergency_check` and `voice.trunk_options`. The
same jobs will poll a real API.

The simulated numbers are fictional only. For North America that is
555-0100 to 0199 in each area code. For the UK it is Ofcom's 020 7946 0xxx
drama range. For Guyana and Belize it is numbers starting with 0 after the
country code, which no real number there does.

`HttpSipProvider` is a skeleton. It refuses until EXA_SIP_PROVIDER_URL, _KEY
and _ACCOUNT are set, and its paths are placeholders to match to the chosen
provider.

**Countries through the go-live registry.** The countries module owns kind
"country". Voice declares its own criteria as features
`voice-numbers-XX` and `voice-porting-XX`, for TT, JM, BB, BS, GY, LC, VC, GD,
AG, KN, DM, BZ, US, CA and GB. The criteria cover numbering rules, an
emergency test call and the island's notice, live test calls, licences, the
port process and roll back. Ordering or porting in a country needs its
feature on. If the countries module has declared the country, that must be on
too. The stage 3 sandbox, with the simulated provider and no country named,
still gives fictional numbers so the demo works. With the live provider a
country is always required. An order is active only when every number's test
call works, as before.

**Port orders have their own life.** A port order moves through these states:
draft → submitted → (documents needed | rejected with a code and reason |
scheduled on the firm order commitment date) → cutting over → completed or
rolled back. It can be rescheduled, or cancelled before cut-over.

Documents are checked by their first bytes. Only PDF, PNG and JPEG up to
10 MB are accepted, and the declared type must agree. Files are stored under
the data directory with a random name and mode 0600, outside the database and
any served path. Their SHA-256 is checked on every read, and they are served
as attachments with `nosniff`.

On the agreed date a job cuts the number over and places test calls. If the
calls fail, it hands the number back to the losing carrier and says so on the
timeline. Every step is a timeline line, an event and a notice for the
business's voice admins. Submitting a port needs spend permission and the
price the admin was shown. ExaCarib can still move a port by hand.

**Emergency addresses per island.** Each site, and anyone with their own
address (a home worker), is checked with the provider by a durable job.
Adding or changing a site and moving a person (the stage 2 move flow) start a
check. A person who moves is also told to read the notice for their new
island.

The rules for each country cover:
- the emergency numbers (added to the dial plan and refused as extensions);
- whether the registered address reaches the emergency centre with the call;
- whether outbound calling must wait for a validated address;
- the notice every user must read and acknowledge.

Where the address goes with the call (US, CA, GB), a number cannot call out
until its address is validated. Elsewhere in the Caribbean the notice tells
people to say where they are. These rules are working assumptions. Confirming
them is the "emergency" go-live criterion of each country, and the API and the
portal say so. Emergency calls are never blocked.

**Several carriers.** Carriers are rows in `voice_carriers` with a versioned
rate sheet. Each is a capability of kind "carrier", off until its criteria are
met: contract, interconnect tested, rates checked and emergency delivery
tested. Routing per destination prefix is least cost or quality. Quality is
OPTIONS success and round-trip time. A down trunk, a missing rate or a carrier
that is not switched on is skipped.

A call tries the carriers in order until one takes it, and every attempt is
kept. The carried call records its carrier and cost. Stage 4's supplier
reconciliation uses that cost. Each carrier's records can be imported and
checked against its own rate sheet. Three failed OPTIONS mark a trunk down,
and one answer brings it back. With no carrier switched on, calls use the
single provider, as before.

**Edge and media.** Kamailio stands in front of FreeSWITCH, with:
- an allow-list rendered from the carriers;
- pike and a per-source cap on new calls;
- TLS;
- one dispatcher set per carrier, with OPTIONS probing;
- failover in `failure_route`, in the order the controller gives
  (`X-Exa-Route`).

Self-hosted LiveKit (server, SIP bridge and an agent worker) carries AI on
phone calls. The worker talks to the controller through
`/voice/livekit/calls...`, with its own shared secret. The call is the same
inbox conversation as stage 1 browser calls. All of it is in compose profile
`voice` and is not started by default.

**Revenue share fraud.** This extends stage 4's limits. "International" means
another country than the business's own, so Trinidad to Jamaica counts. The
checks are:
- a high-risk destination list per calling country, kept by ExaCarib, which a
  business can override per prefix with spend permission;
- after-hours rules (allow, alert or block);
- daily caps on international calls and spend;
- spike detection that suspends international calling.

Spike detection compares this hour's attempts, blocked ones included, with
the usual hourly rate over the week. After a suspension a voice admin with
spend permission restores calling, with a note, and it is audited.

## What is simulated, and what waits on Dudley

- **Provider and carriers**: all simulated (`sim-carrier-1`, `sim-carrier-2`,
  with example rates and documentation-range IPs). A real carrier needs an
  account, its signalling addresses, TLS details, a rate sheet and a signed
  contract. Then its go-live record is completed.
- **Voice host**: a public IP and ports, listed in `deploy/kamailio/README.md`,
  and a TLS certificate. Kamailio and LiveKit have not been run here.
- **Speech for AI phone calls**: no provider is chosen. This needs Dudley's
  yes to the spend.
- **Emergency rules**: to confirm with each regulator or carrier before go-live.
- **High-risk list**: a starting list. Review it with each carrier's fraud team.
- **FreeSWITCH to Kamailio**: wired (addendum below). It has been run against
  the real FreeSWITCH and Kamailio images with a stand-in controller and dead
  carriers, not with a real carrier.

## Addendum: the dial plan asks before every outside call

Real calls skipped the controller: the dial plan bridged outside calls straight
to the gateway. Now each business's rendered dial plan asks the controller
first, with mod_curl, at `GET /api/v1/commai/internal/voice/authorise`
(`voice/pbx.py`). It reuses `billing.authorise` and `carriers.plan`, the same
code as simulated calls. The answer is one line the dial plan matches with a
regular expression: `OK <set ids> <caller id>` or `NO <code>`. `OK` sets
`X-Exa-Route` and the caller id, then bridges. A refusal is kept as a blocked
call and in `voice_pbx_authorisations`.

- **Fail closed.** No answer, or anything that is not `OK`, refuses the call.
  Emergency calls never ask. They bridge at once with every carrier switched
  on for the business in `X-Exa-Route`.
- **Signed, not a bare secret.** mod_curl logs request headers at debug level.
  So the request carries a digest of its fields made with `EXA_PBX_SECRET`,
  not the secret. FreeSWITCH's dial plan offers only md5, so the digest is
  md5(secret:fields:secret). A leaked digest is good for that one call only.
  It runs on the private compose network, and the public proxy refuses the
  path.
- **The business is the tenant the dial plan was rendered for**, never a value
  the caller sends.
- **Forwarding to an outside number** goes back through the same check
  (loopback).
- **`EXA_VOICE_EDGE`** chooses the gateway: the single provider, as in stage 4,
  or Kamailio. Kamailio's allow-list now has FreeSWITCH in group 2
  (`EXA_KAMAILIO_PBX_NETS`).

Known limits:
- Calls that were allowed count towards spike detection only when their call
  record arrives. FreeSWITCH's call records (`mod_json_cdr`) are not wired yet.
- When Kamailio cannot open a connection to a carrier at once (TCP refused), it
  answers an error instead of trying the next set. Timeouts and SIP errors do
  fail over.

## Consequences

Numbers, ports, emergency addresses, carriers and fraud protection can be
shown end to end with no account. Switching to real ones means an adapter's
request paths, environment settings, and completed go-live records. The flows
and screens stay the same. Every country, carrier and emergency rule stays off
until ExaCarib records its criteria as met.
