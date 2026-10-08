# ExaConnect with Terraform: a resilient circuit to AWS, a layer 2 circuit
# between two sites, local internet breakout, a firewall rule, a port forward
# and a traffic rule.
#
#   export EXACONNECT_URL=https://connect.exacarib.com
#   export EXACONNECT_API_KEY=...            # from the portal's Account screen
#   export TF_VAR_aws_vpn_psk=...            # or wire it to aws_vpn_connection (below)
#   terraform plan
#
# Site names (site-a, site-b) are the lab's; use your own.

terraform {
  required_providers {
    exaconnect = {
      source = "exacarib/exaconnect"
    }
  }
}

provider "exaconnect" {
  # url     = "https://connect.exacarib.com"   # or EXACONNECT_URL
  # api_key = var.exaconnect_api_key           # or EXACONNECT_API_KEY
}

variable "aws_tunnel1_address" {
  type    = string
  default = "52.1.2.3"
}

variable "aws_tunnel2_address" {
  type    = string
  default = "52.1.2.4"
}

variable "aws_vpn_psk" {
  type      = string
  sensitive = true
}

data "exaconnect_me" "me" {}

data "exaconnect_sites" "all" {
  customer_id = data.exaconnect_me.me.customer_id
}

locals {
  cid    = data.exaconnect_me.me.customer_id
  site_a = data.exaconnect_sites.all.sites["site-a"]
  site_b = data.exaconnect_sites.all.sites["site-b"]
}

# With the AWS provider, take the addresses and key from the VPN connection:
#   peer_address           = aws_vpn_connection.x.tunnel1_address
#   secondary_peer_address = aws_vpn_connection.x.tunnel2_address
#   psk                    = aws_vpn_connection.x.tunnel1_preshared_key
#   inside_cidr            = aws_vpn_connection.x.tunnel1_inside_cidr
#   secondary_inside_cidr  = aws_vpn_connection.x.tunnel2_inside_cidr
resource "exaconnect_cloud_circuit" "aws" {
  customer_id            = local.cid
  name                   = "AWS prod"
  provider_name          = "aws"
  region                 = "us-east-1"
  site_id                = local.site_a.id
  peer_address           = var.aws_tunnel1_address
  secondary_peer_address = var.aws_tunnel2_address
  peer_asn               = 64512
  psk                    = var.aws_vpn_psk
  cloud_prefixes         = ["10.100.0.0/16"]
  bandwidth_mbps         = 50
}

resource "exaconnect_site_circuit" "erp" {
  customer_id    = local.cid
  name           = "ERP VLAN"
  a_site_id      = local.site_a.id
  b_site_id      = local.site_b.id
  a_vlan         = 100
  bandwidth_mbps = 20
}

resource "exaconnect_site_internet" "site_b" {
  customer_id = local.cid
  site_id     = local.site_b.id
  mode        = "local" # pop | local | off; destroy sets it back to pop
}

resource "exaconnect_firewall_rule" "no_ping_out" {
  customer_id = local.cid
  action      = "deny"
  protocol    = "icmp"
  dst         = ["198.51.100.0/24"]
  description = "No ping to the test range"
}

resource "exaconnect_port_forward" "web" {
  customer_id = local.cid
  protocol    = "tcp"
  port        = 8443
  to_site_id  = local.site_a.id
  to_address  = cidrhost(local.site_a.lan_prefixes[0], 10)
  to_port     = 443
  allow_from  = ["203.0.113.0/24"]
  description = "Intranet"
}

resource "exaconnect_traffic_rule" "teams" {
  customer_id = local.cid
  name        = "Teams media"
  class_name  = "voice"
  ports       = "udp:3478-3481"
  domains     = ["teams.microsoft.com"]
}

output "aws_inside" {
  value = {
    tunnel1 = { ours = exaconnect_cloud_circuit.aws.our_inside, cloud = exaconnect_cloud_circuit.aws.cloud_inside }
    status  = exaconnect_cloud_circuit.aws.status
  }
}
