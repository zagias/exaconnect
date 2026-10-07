# ADR 0040: One portal, two apps, and who may open each

Date: 7 October 2026. Status: accepted.

## Context

ExaCarib sells two apps: Connect (the network) and the customer conversations
app, until now called CommAI. They are separate plans (ADR 0022, 0023): an
organisation holds Connect, the conversations app or both. Dudley asked for
both to sit in one portal but clearly apart, because people are given one
app, the other or both, and for an easy-to-follow menu. He named the
conversations app **Jibsy**, shown as **Jibsy by ExaCarib**.

## Decision

- **Plans stay where ADR 0023 put them**: `customers.products` (NULL: both).
- **Each member's apps**: `org_memberships.apps` (NULL: everything on the
  plan, so a plan added later reaches everyone who was never narrowed). The
  apps a person may open are the plan narrowed by this list. `check_product`
  refuses a request with "isn't on your organisation's plan" or "You don't
  have access to …", so every API and the inbox socket follow it.
- **Who sets it**: owners and admins on Account > People (a box per app), never
  for themselves; only an owner changes another owner's apps; an app off the
  plan can't be given. Audited as `org.member.apps`.
- **Asking for an app**: owners and admins ask ExaCarib to add an app
  (`POST /orgs/{id}/apps/{app}/request`, table `customer_app_requests`).
  ExaCarib admins see the queue on Apps and plans; a plan change answers it.
- **Portal**: an app switcher heads the sidebar, and the sidebar shows one
  app's menu at a time (Connect blue, Jibsy violet). People with one app see
  a plain label, not a switcher. Account, People, Apps and plans, Billing and
  My settings form a shared Account area. A screen of an app someone can't
  open explains why and links to the apps they have. The Storm Mode switch
  shows on Connect screens only. Jump-to searches every app the person has.
- **Names**: people see "Jibsy" and "Jibsy by ExaCarib". Code, routes
  (`/commai`), API paths, scopes and database names keep `commai`, so
  integrations, keys and stored data are untouched.

## Consequences

- Seats and prices stay with billing; this ADR is about access only.
- ADRs written before the rename still say CommAI; they record history.
