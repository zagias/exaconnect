# ExaConnect Terraform provider

Manage ExaConnect from Terraform with the same REST API the portal uses
(contract: `docs/automation-contract.md`, ADR 0013). Provider address
`registry.terraform.io/exacarib/exaconnect`; built on
terraform-plugin-framework.

```hcl
terraform {
  required_providers {
    exaconnect = { source = "exacarib/exaconnect" }
  }
}

provider "exaconnect" {
  url     = "https://connect.exacarib.com"   # or EXACONNECT_URL
  api_key = var.exaconnect_api_key           # or EXACONNECT_API_KEY; sensitive
}

data "exaconnect_me" "me" {}
data "exaconnect_sites" "all" { customer_id = data.exaconnect_me.me.customer_id }

resource "exaconnect_cloud_circuit" "aws" {
  customer_id            = data.exaconnect_me.me.customer_id
  name                   = "AWS prod"
  provider_name          = "aws"
  region                 = "us-east-1"
  site_id                = data.exaconnect_sites.all.sites["site-a"].id
  peer_address           = aws_vpn_connection.x.tunnel1_address
  secondary_peer_address = aws_vpn_connection.x.tunnel2_address
  peer_asn               = 64512
  psk                    = aws_vpn_connection.x.tunnel1_preshared_key
  cloud_prefixes         = ["10.100.0.0/16"]
  bandwidth_mbps         = 50
}
```

A fuller example is in [`examples/main.tf`](examples/main.tf).

Create the API key on the portal's Account screen. It acts as you, with
your role and organisation. Keep it in an environment variable or a
secret store, never in a `.tf` file.

## What it manages

| Type | API |
| --- | --- |
| `data.exaconnect_me` | `GET /auth/me`: `email`, `role`, `customer_id` (null for admins) |
| `data.exaconnect_sites` | `GET /sites`, as `sites["<name>"] = {id, name, kind, location, lan_prefixes}`. `customer_id` defaults to the key's organisation; an admin key must set it |
| `exaconnect_cloud_circuit` | `/customers/{cid}/circuits`, kind `cloud` (PATCH on change) |
| `exaconnect_site_circuit` | `/customers/{cid}/circuits`, kind `site` (PATCH on change) |
| `exaconnect_firewall_rule` | `/customers/{cid}/firewall/rules` (PATCH on change) |
| `exaconnect_port_forward` | `/customers/{cid}/port-forwards` (PATCH on change) |
| `exaconnect_site_internet` | `PATCH /customers/{cid}/internet/sites/{site_id}`; destroy sets the mode back to `pop` |
| `exaconnect_traffic_rule` | `/customers/{cid}/rules` (PUT on change) |

Notes:

- **Import** with `<customer_id>/<id>`, or `<customer_id>/<site_id>` for
  `exaconnect_site_internet`:
  `terraform import exaconnect_cloud_circuit.aws 12a21e21-.../7`.
- **`psk` is write-only.** The API never returns it. It is sent on create
  and whenever it changes in your configuration. After an import Terraform
  does not know it, so the next apply sends it once (an in-place update).
- **Computed values.** `id`, `inside_cidr`, `secondary_inside_cidr`,
  `our_inside` and `cloud_inside` stay as they are between applies.
  `inside_cidr` and `secondary_inside_cidr` can also be set to what the
  cloud console shows. `status` (`provisioning`, `up`, `down`, `off`) comes
  from the agents' telemetry: it is refreshed on every plan and shows as
  "known after apply" when the circuit itself changes.
- **Resilient pairs.** Set `secondary_peer_address` for a second tunnel;
  remove it to go back to one.
- **Defaults follow the API.** `b_vlan` defaults to `a_vlan`, `to_port`
  to `port`, `enabled` to true, a firewall rule's `protocol` to `any`, a
  traffic rule's `ordinal` to 100, lists to empty. Unsetting one of them
  puts the default back.
- **Same value, different spelling.** The API normalises addresses
  (`203.0.113.9` becomes `203.0.113.9/32`) and website names (lower case,
  no `*.`). The provider treats those as no change.
- **Firewall order.** A new rule goes to the end of the organisation's
  list; `position` shows where it is. Use `depends_on` between rules if
  their order matters.
- **Deleted elsewhere.** A resource that the API no longer has is dropped
  from state on refresh, and the next apply creates it again.
- Errors show the API's own message, for example
  `400 Bad Request: The second gateway address must differ from the first.`

## Build and use it locally

Needs Go 1.25.8 or later (terraform-plugin-framework 1.19 and
terraform-plugin-testing 1.16 require it). With an older Go, the default
`GOTOOLCHAIN=auto` fetches the right toolchain by itself.

```sh
cd terraform
go build -o ~/.terraform.d/exaconnect-dev/terraform-provider-exaconnect .
```

Tell Terraform to use that build instead of the registry, in
`~/.terraformrc` (or a file named by `TF_CLI_CONFIG_FILE`):

```hcl
provider_installation {
  dev_overrides {
    "exacarib/exaconnect" = "/Users/you/.terraform.d/exaconnect-dev"
  }
  direct {}
}
```

Then, with no `terraform init` needed for this provider:

```sh
cd examples
export EXACONNECT_URL=http://127.0.0.1:8000     # your controller
export EXACONNECT_API_KEY=...                   # never commit it
export TF_VAR_aws_vpn_psk=...
terraform plan
```

Terraform prints a warning that dev overrides are in use; that is expected.

Cross-builds: `CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build ./...` (and
`GOARCH=amd64`, or `GOOS=darwin`).

## Tests

```sh
go test ./...                       # unit tests: the API client against httptest, normalisers
```

Acceptance tests create, update in place, import and destroy every
resource and read both data sources against a real controller seeded with
the lab inventory (`python -m exaconnect_controller.seed --lab`: customer
"Demo Organisation", sites `site-a` and `site-b`):

```sh
export TF_ACC=1
export EXACONNECT_URL=http://127.0.0.1:8010
export EXACONNECT_API_KEY=...       # an API key or session token, admin or customer
# optional: EXACONNECT_TEST_CUSTOMER_ID (default: the key's organisation,
#           or "Demo Organisation" for an admin key)
# optional: TF_ACC_TERRAFORM_PATH=/path/to/terraform. Without it the tests
#           download the latest Terraform with hc-install; set
#           TF_ACC_TERRAFORM_VERSION=1.16.5 if checkpoint-api.hashicorp.com is blocked
go test ./internal/provider -run TestAcc -count=1 -v
```

They leave the organisation as they found it (port forwards use a random
public port from 40000 to 59999).
