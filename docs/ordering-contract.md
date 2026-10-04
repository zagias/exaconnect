# Partner directory and plain-English ordering: contract (ADR 0011)

Step 4 of ExaConnect Fabric. Customers find partners in a directory and
order connections, circuits, bandwidth changes and internet breakout by
describing what they want in plain English. Nothing changes until a person
confirms the draft. Partner connections reuse the virtual circuits of ADR
0009 (route-based IPsec and BGP at the PoP).

## Partners

```json
{"id": 3, "slug": "aws", "name": "Amazon Web Services", "category": "cloud",
 "kind": "cloud",            // cloud: the customer sets up the VPN in their own cloud console
                             // service: the partner provides the gateway; ExaCarib completes the order
 "provider": "aws",          // cloud only: the circuit provider preset (fabric.PROVIDERS)
 "description": "...", "website": "https://...", "regions": ["us-east-1", ...],
 "prefixes": ["203.0.113.0/24"],   // service only: what the partner advertises (shown to customers)
 "price_per_mbps_month": 2.0, "listed": true, "example": false}
```

Categories: `cloud`, `saas`, `payments`, `internet`, `security`, `content`, `other`.
`example: true` marks seeded sample entries; the portal labels them "Example data".

## Orders

An order holds one to five actions:

```json
{"action": "cloud_circuit", "name": "AWS us-east-1", "provider": "aws", "region": "us-east-1",
 "site": "site-a" | null,     // null: every site may reach it
 "bandwidth_mbps": 50, "cloud_prefixes": ["10.100.0.0/16"], "class_name": "business" | null}
{"action": "site_circuit", "name": "...", "a_site": "site-a", "b_site": "site-b",
 "a_vlan": 100, "b_vlan": 200, "bandwidth_mbps": 20}
{"action": "partner_connection", "partner": "example-pay", "site": null, "bandwidth_mbps": 10}
{"action": "bandwidth", "circuit": "AWS us-east-1", "bandwidth_mbps": 100}
{"action": "internet_mode", "site": "site-b", "mode": "pop" | "local" | "off"}
```

Sites are named by `name`. A `partner_connection` to a `cloud` partner is a
`cloud_circuit` with the partner's provider; to a `service` partner it waits
for ExaCarib to enter the partner's gateway details.

### Order view

```json
{"id": 12, "status": "draft",        // draft | done | pending_partner | cancelled | failed
 "engine": "ai" | "rules" | "form",  // who drafted it
 "text": "Connect Kingston to our AWS VPC in us-east-1 at 50 Mbps",
 "actions": [ ... ],                 // as above, normalised
 "summary": ["New circuit from site-a to Amazon Web Services (us-east-1), 50 Mbps, for 10.100.0.0/16."],
 "needs": [                          // inputs the person must give before confirming
   {"action": 0, "field": "peer_address", "label": "The AWS VPN gateway's public address", "secret": false},
   {"action": 0, "field": "peer_asn", "label": "The gateway's ASN", "secret": false, "default": 64512},
   {"action": 0, "field": "psk", "label": "Pre-shared key from the AWS console", "secret": true}
 ],
 "problems": ["There is no site called Montego Bay."],   // the draft can't be confirmed while any remain
 "monthly_estimate": 100.0,          // change in the monthly charge, US$
 "results": [{"action": 0, "ok": true, "circuit_id": 7, "message": "..."}],
 "created_by", "created_at", "confirmed_by", "confirmed_at"}
```

`needs` come from the circuit validators: a cloud circuit needs
`peer_address`, `peer_asn` (default from the provider) and `psk`, plus
`inside_cidr` optionally. Nothing secret is stored in the order: the key
goes straight to the circuit when the order is confirmed.

## API (portal to controller), under /api/v1

Customers their own; admins any; carrier users none. Every write is audited.

- `GET /partners?category=&q=`: listed partners. `GET /partners/{slug}`.
- `POST /customers/{cid}/orders/draft` `{text, engine?: "auto"|"rules"}` → 201 with a draft.
  The rules parser understands, for example: "Connect Kingston to AWS us-east-1 at 50 Mbps for
  10.100.0.0/16", "Join Kingston and Port of Spain on VLAN 100 and 200 called \"erp l2\"",
  "Increase AWS prod to 100 Mbps", "Send Port of Spain's internet straight out".
  `auto` uses the AI service when a key is configured (counted against the
  hourly question limit), else the rules parser. If nothing is understood,
  the draft has no actions and one problem saying so, with an example.
- `POST /customers/{cid}/orders` `{actions: [...]}` → 201 with a draft (engine "form"), for
  the directory's Connect button and the portal's forms.
- `GET /customers/{cid}/orders` newest first (last 50); `GET /customers/{cid}/orders/{id}`.
- `POST /customers/{cid}/orders/{id}/confirm` `{inputs: [{"peer_address": "...", "peer_asn": 64512,
  "psk": "...", "inside_cidr": null}, {...}]}` (one object per action, `{}` where none needed) →
  applies every action in one transaction: all or nothing. 200 with the order (`done`, or
  `pending_partner` when a service partner must act); 400 with `detail` if anything fails.
- `POST /customers/{cid}/orders/{id}/cancel` → the order, `cancelled`.
- Admin: `POST /admin/partners`, `PATCH /admin/partners/{id}`, `DELETE /admin/partners/{id}`
  (unlists it if orders refer to it); `GET /admin/partners` (all, listed or not);
  `GET /admin/orders?status=pending_partner`;
  `POST /admin/orders/{id}/complete` `{peer_address, peer_asn, psk, inside_cidr?, prefixes}` →
  creates the circuit for a service partner connection; the order becomes `done`.
