# ADR 0022: Billing phase 1, priced per product plan

Date: 2026-10-07. Status: accepted.

## Context

Connect and CommAI are sold as separate plans. An organisation may hold
either or both. ExaCarib needs to bill each plan monthly from data it
already keeps (inventory, metering, probe windows, CommAI usage and voice
rating), show every line's reason, give carriers and Fabric partners their
due, and see margin. No price list has been agreed yet, no payment provider
account is open, and nothing may depend on either.

## Decision

**Products, plans, subscriptions.** Two products, `connect` and `commai`.
A plan (`plans`) belongs to one product and is billed monthly. An
organisation's subscriptions (`subscriptions`: plan, `starts_on`, exclusive
`ends_on`, status) are the source of truth for which products it holds;
`billing/plans.products()` answers that for a day. At most one open-ended
subscription per product. A plan change ends one subscription on a day and
starts the next on the same day, so the month splits cleanly. The
organisations work adds a nullable `customers.products text[]` for access
gating; every subscription write copies the held products into it, guarded
by a check that the column exists, and an organisation that never had a
subscription is left as it was. Existing deployments get one Connect
Standard subscription per organisation with sites, from the day it was
added; the lab seed does the same.

**Price lists per plan, versioned.** `price_lists` rows belong to a plan,
optionally to one organisation (a negotiated contract, which wins), with
`effective_from`, currency (default USD) and tax rate (default 0). Every
save is a new version; old versions never change. Connect lists carry the
site fee, commit and burst price per Mbps, satellite price per GB, an
optional circuit price per Mbps a month, and the SLA credit table and cap.
CommAI lists carry a monthly plan fee and meter prices (exact meter, then
the family, `message_out:*`). Both plans start with clearly marked example
lists, and every screen and export says "Example data" while they are used.

**Charges per subscription per calendar month (UTC).** The billed window is
the part of the month the subscription covers. Connect: plan fee, site fee
(pro rata by day from when the site was added), commit per link (pro rata
likewise), burst at the 95th percentile from `metering.core.settle` on the
same 5-minute samples as the metering screen and carrier CSV, satellite data
per GB from those samples, and Fabric virtual circuits and elastic bandwidth
metered hourly from `fabric.charges`. CommAI: plan fee, usage from
`usage_records` at the plan's meter prices, and CommAI Voice charges taken
as voice billing rated them (`voice_charges`, its own monthly user and
number fees made by its own `monthly_charges`); voice minutes in
`usage_records` are never priced again. Voice charges already on an issued
voice invoice are left off. Each line names its plan and keeps its inputs.

**SLA credits.** `sla.windows()` is the query behind the Overview's "SLA met
in the last 24 hours" and now takes a window, so the monthly figure is the
same count. Per site and class (best effort excluded), the share of 10 s
windows that met the class SLA falls into the credit table; the credit is a
share of that site's fee, capped per site, and appears as a negative line
with a plain-English reason.

**Invoices.** One invoice per subscription and month. Drafts can be
regenerated at will: the same invoice gets fresh lines. Issuing (admin only,
after the window has ended) gives the next number for the year across all
organisations and plans, `EXA-2026-0001`, under an advisory lock. A database
trigger freezes issued and void invoices and their lines; an issued invoice
may only become void (with a reason), after which the month can be drafted
again under a new number. A paid invoice can't be voided. CSV and a
printable page are served for each.

**Supplier side, kept separate.** Carrier payables come from each link's
carrier prices (`links.cost_per_mbps`, `burst_price`) through the same
settlement code; partner payables from `partners.cost_per_mbps_month`, hour
by hour, shown as unknown until set. Customer rate cards never feed these.
Margin per organisation and per service (plan fees, sites, connectivity,
Fabric, credits, CommAI usage, voice) uses the live invoice where there is
one and the month worked out now where there is not, in USD only.

**Payments and accounting, off by default.** `EXA_PAYMENTS=off|simulated|live`.
Two adapters behind one interface: Stripe Checkout (session created by form
post, `checkout.session.completed` webhook checked against `Stripe-Signature`
with five minutes' tolerance) and a hosted payment page shaped like First
Atlantic Commerce / PowerTranz (JSON request with merchant id and password
headers, ISO 4217 numeric currency, callback signed with HMAC-SHA256 in
`X-Signature`). Simulated mode makes no network call; a simulated completion
is a signed event played through the same webhook, which is the only path
that marks an invoice paid. Each event is acted on once; a wrong amount or
currency is recorded as a mismatch for a person to look at. Live needs the
provider's settings in the environment (placeholder names in
`.env.example`); a provider missing them stays simulated. Accounting export
gives Xero and QuickBooks Online invoice payloads and Xero's import CSV for
issued invoices; nothing is sent. Lines whose quantity times unit price
would not give the line amount are exported as one unit at the amount.

## Consequences

- Billing is explainable line by line and reproducible from stored data.
- Taking real payments needs ExaCarib's provider accounts and a decision to
  switch `EXA_PAYMENTS` to `live`; pushing to Xero or QuickBooks needs an
  OAuth connection, which is phase 2.
- CommAI Voice keeps its own invoices for now. When a CommAI plan invoice is
  issued, ExaCarib should stop issuing separate voice invoices for that
  organisation; phase 2 marks voice charges as billed on the plan invoice.
- Credit notes for Connect and CommAI plan invoices are phase 2; until then a
  correction is a void and a new invoice.
- Margin has no CommAI supply cost yet (voice supplier reconciliation lives
  in voice billing).
