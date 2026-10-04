# ADR 0011: Partner directory and plain-English ordering

Date: 2026-10-04. Status: accepted.

## Context

Step 4 of ExaConnect Fabric. Megaport and Equinix let customers find
partners in a marketplace and order connections in a few clicks. Our
customers are often not network engineers: they want to say "connect
Kingston to our AWS VPC at 50 Mbps" and have it happen, without being
surprised by what it does or costs.

## Decision

**A directory of partners**, kept by ExaCarib admins. Two kinds:

- **Cloud partners** (AWS, Azure, Google Cloud, Oracle): the customer sets
  up the VPN in their own cloud console. An order to one is a cloud circuit
  (ADR 0009) with that provider's preset.
- **Service partners** (payments networks, hosted ERP and similar): the
  partner provides the gateway. The order waits until ExaCarib enters the
  partner's details, then becomes a circuit to the partner's prefixes.

The directory starts with the four clouds and two service entries marked
"Example data"; admins add, edit and unlist partners. A partner that
orders or circuits refer to is unlisted, never deleted.

**Orders in plain English, drafted, then confirmed.** A request becomes a
draft of one to five actions: a cloud circuit, a site circuit, a partner
connection, a bandwidth change or a change of internet breakout. Two
drafting engines:

- **AI**, when a key is configured: the model gets the customer's site,
  circuit, class and partner names (no addresses, keys or traffic) and
  returns JSON actions. Each draft counts against the same hourly limit as
  "Ask your network". If the service fails, the rules engine drafts instead.
- **Rules**: a small parser for the common phrasings (clouds and regions,
  sites by name or town, VLANs, speeds, subnets, "straight out", "through
  the PoP"). It works with no AI service and makes the lab deterministic.

Either way the draft is checked by the controller, not trusted: names are
resolved against the customer's own records, and anything unknown, missing
or out of range is listed as a problem in plain words. The draft shows what
will change, the inputs still needed (a gateway address, an ASN, a key) and
the change to the monthly charge.

**Nothing changes until a person confirms**, and then every action applies
in one transaction or none does. Keys are typed at confirmation and go
straight to the circuit; they are never stored in the order or the audit
log. Every draft, confirmation and cancellation is audited with the actor.

## Consequences

- The AI engine can misread a request. The checks above, the summary in
  plain words and the confirm step are the guard; the portal labels AI
  drafts "check it carefully".
- Service partners need a manual step at ExaCarib until partners have an
  API of their own (MEF LSO Sonata, after the MVP).
- One order is limited to five changes, to keep a draft readable.
- Lab check `m9-order.sh` orders a layer 2 circuit, a bandwidth change and
  an internet change in plain English with the rules engine. The contract
  is `docs/ordering-contract.md`.
