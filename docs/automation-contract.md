# API keys, Python SDK and Terraform provider: contract (ADR 0013)

Step 6 of ExaConnect Fabric. Customers automate ExaConnect from their own
code and infrastructure tooling with the same REST API the portal uses.

## API keys (controller)

A person creates keys for themselves on the Account screen. A key acts as
that person, with their role and organisation, and nothing more. The key
is shown once; only its SHA-256 hash is stored.

- Format: `exa_` followed by 43 URL-safe characters. The first 12 characters
  (`exa_` + 8) are its *prefix*, kept to tell keys apart.
- Sent like a session token: `Authorization: Bearer exa_...`.
- Optional expiry in days (1 to 730); no expiry by default. Revoked or
  expired keys answer 401 "This API key has been revoked or has expired."
- At most 20 active keys a person. Every create and revoke is audited;
  `last_used_at` is updated at most once a minute.

Endpoints, under `/api/v1`, for any signed-in person (keys included):

- `GET /auth/api-keys` → `[{id, name, prefix, created_at, last_used_at, expires_at}]`, own active keys.
- `POST /auth/api-keys` `{name (1 to 60), days? (1 to 730)}` → 201
  `{id, name, prefix, created_at, expires_at, token}`. `token` appears only here.
- `DELETE /auth/api-keys/{id}` → 204 (revokes it).

## Python SDK (`sdk/python`, package `exaconnect`)

```python
from exaconnect import ExaConnect

exa = ExaConnect("https://connect.exacarib.com", api_key=os.environ["EXACONNECT_API_KEY"])
cid = exa.me()["customer_id"]
c = exa.circuits.create(cid, name="AWS prod", kind="cloud", provider="aws", region="us-east-1",
                        peer_address="52.1.2.3", psk=os.environ["AWS_VPN_PSK"],
                        cloud_prefixes=["10.100.0.0/16"], bandwidth_mbps=50)
exa.circuits.update(cid, c["id"], bandwidth_mbps=100)
draft = exa.orders.draft(cid, "Send Port of Spain's internet straight out")
exa.orders.confirm(cid, draft["id"])
```

- `ExaConnect(base_url, api_key=None, *, email=None, password=None, timeout=30, client=None)`;
  `api_key` defaults to `EXACONNECT_API_KEY`, `base_url` may come from `EXACONNECT_URL`.
  `client` is an `httpx.Client` to use (tests pass the controller's TestClient).
- Errors raise `ExaConnectError(status, detail)` with the API's plain-English `detail`.
- Resources mirror the API: `sites`, `circuits`, `internet` (mode, firewall rules, port
  forwards), `traffic` (rules), `orders`, `partners`, `encryption`, `metering`, `decisions`,
  `api_keys`, `storm`. Each method returns the API's JSON (dicts and lists).
- Never logs or prints keys; `repr()` of the client hides the key.
- Depends only on `httpx`. Python 3.10+.

## Terraform provider (`terraform/`, `registry.terraform.io/exacarib/exaconnect`)

Go, terraform-plugin-framework.

```hcl
provider "exaconnect" {
  url     = "https://connect.exacarib.com"   # or EXACONNECT_URL
  api_key = var.exaconnect_api_key           # or EXACONNECT_API_KEY; sensitive
}

data "exaconnect_me" "me" {}
data "exaconnect_sites" "all" { customer_id = data.exaconnect_me.me.customer_id }

resource "exaconnect_cloud_circuit" "aws" {
  customer_id            = data.exaconnect_me.me.customer_id
  name                   = "AWS prod"
  provider_name          = "aws"         # "provider" is reserved in Terraform
  region                 = "us-east-1"
  site_id                = data.exaconnect_sites.all.sites["site-a"].id   # optional
  peer_address           = aws_vpn_connection.x.tunnel1_address
  secondary_peer_address = aws_vpn_connection.x.tunnel2_address          # optional pair
  peer_asn               = 64512
  psk                    = aws_vpn_connection.x.tunnel1_preshared_key    # sensitive, write-only
  cloud_prefixes         = ["10.100.0.0/16"]
  bandwidth_mbps         = 50
}
resource "exaconnect_site_circuit" "erp" { customer_id, name, a_site_id, b_site_id, a_vlan, b_vlan, bandwidth_mbps }
resource "exaconnect_firewall_rule" "x"  { customer_id, action, site_id?, src?, dst?, protocol?, ports?, description?, enabled? }
resource "exaconnect_port_forward" "x"   { customer_id, protocol, port, to_site_id, to_address, to_port?, allow_from?, description? }
resource "exaconnect_site_internet" "x"  { customer_id, site_id, mode }   # destroy sets mode back to "pop"
resource "exaconnect_traffic_rule" "x"   { customer_id, ...as POST /customers/{cid}/rules }
```

- Import by `<customer_id>/<id>` (`<customer_id>/<site_id>` for `exaconnect_site_internet`).
- `psk` is never read back (the API never returns it); a change to it is sent as an update.
- Computed attributes: `id`, `status`, and for circuits `inside_cidr`, `secondary_inside_cidr`,
  `our_inside`, `cloud_inside`.
- 404 on read removes the resource from state.
