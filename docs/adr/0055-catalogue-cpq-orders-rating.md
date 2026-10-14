# ADR 0055: catalogue, quotes (CPQ), sales orders and the rating engine

Status: accepted, 2026-10-14

## Context

Billing phase 1 (ADR 0022) bills each plan from its price list: plan fees, sites,
link commits, burst and satellite at the 95th percentile, Fabric circuits, Jibsy
usage and voice. There was no way to sell anything else (a box, an installation, a
managed service, seats), to quote it with discounts, to turn an accepted quote into
work, or to rate usage of things sold by contract. Dudley asked for billing, order
management, CPQ and a rating engine. There are no real prices yet.

## Decision

- **Catalogue** (`catalogue_items`, `catalogue_prices`): each item has a product
  (Connect or Jibsy), a charge type (monthly, once, by use, with a meter), how it is
  fulfilled, and the most a person can discount it without approval. Prices are
  versioned per currency with a start date; a new price never changes an old one,
  and quotes keep a copy of the price they used. Plan fees, sites and link commits
  are catalogue items too, but priced from the organisation's plan price list, so a
  quote shows exactly what the invoice will charge and nothing is billed twice.
- **Rating engine** (`commercial/rating.py`, pure functions): per unit, flat,
  tiered (graduated) and volume prices, included quantity, minimum charge, term
  discounts, and a person's discount applied after the term discount (5% then 10%
  is 14.5%). Four decimal places; invoices round each line to the cent. Unit tests
  use hand-worked examples. Previews at `POST /rating/preview`.
- **Quotes**: draft, waiting for approval, approved, sent, accepted or declined;
  withdrawn; expired after "valid until" (copy it for today's prices). A discount
  above an item's limit needs an ExaCarib admin to approve it with a reason.
  Changing a quote takes it back to draft and clears the approval. Quote numbers
  are `Q-YYYY-NNNN`; a printable quote is at `/quotes/{id}/document`.
- **Self-service**: an organisation's owners and admins can build a quote from the
  catalogue at list price (no discounts), finish it and accept it. Members read
  quotes and orders but cannot accept.
- **Sales orders** (`sales_orders`, `sales_order_items`, `SO-YYYY-NNNN`), a new
  table beside Fabric's circuit `orders` (ADR 0011). Accepting a quote (with the
  person's typed name) makes the order and runs its automatic items in order:
  plan, sites (new locations and their Connect sites, ASN and overlay address
  chosen as in ADR 0043), links (next free path), then items that just start.
  Each runs in its own savepoint: a failure keeps its reason and the rest carries
  on; staff fix the details and try again. Hardware and installation wait for
  staff to mark them done.
- **Contract**: a done item starts its charges that day: monthly items and usage
  items become `contract_items` (with the agreed price), one-offs become
  `one_off_charges`. A discount on a plan-priced item becomes a monthly credit,
  tied to its site or link so it stops when that is removed.
- **Usage** (`usage_events`, TMF635-style): meter, quantity, time and the sender's
  own reference; a reference already seen is skipped, so sending again is safe.
  Only meters the organisation has a usage item for are taken.
- **Invoices**: `billing.service.compute_subscription` adds the product's contract
  lines to its plan invoice: `recurring` (pro rata by day), `credit`, `one_off`
  (in the month it was charged) and `rated` (usage rated on the agreed price). An
  item in another currency than the plan's price list stops the invoice with a
  reason; nothing is converted.
- **Example prices**: the seeded catalogue (plan, site, link commit, box,
  installation, managed service, engineer time, Jibsy seats on volume bands, Jibsy
  onboarding) is marked example and shown as "Example data" until ExaCarib sets
  real prices under Operations › Catalogue.
- Payment and accounting stay as in ADR 0022 (simulated providers until Dudley
  chooses real ones).

## Consequences

- Operations has a Sales group: Catalogue, Quotes, Orders. Organisations get
  Quotes and orders beside Billing, and Billing shows what they have bought.
- A live check (`lab/ci/checks/commercial.sh`) runs the whole path in a browser on
  every deploy, with a fresh example organisation that the next run removes.
- Not built: quote e-mails (no outbound e-mail yet), e-signature, contract
  renewal reminders, proration credits when a contract item ends mid-month in an
  issued month, and TMF620/TMF622 views of the catalogue and sales orders.
