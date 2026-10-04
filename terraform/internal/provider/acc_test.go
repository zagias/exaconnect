package provider

import (
	"context"
	"fmt"
	"math/rand"
	"regexp"
	"testing"

	"github.com/hashicorp/terraform-plugin-testing/helper/resource"
	"github.com/hashicorp/terraform-plugin-testing/plancheck"
	"github.com/hashicorp/terraform-plugin-testing/terraform"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

func inPlace(addr string) resource.ConfigPlanChecks {
	return resource.ConfigPlanChecks{PreApply: []plancheck.PlanCheck{
		plancheck.ExpectResourceAction(addr, plancheck.ResourceActionUpdate),
	}}
}

// ---- data sources ---------------------------------------------------------------

func TestAccDataSources(t *testing.T) {
	ids := lab(t)
	me, err := testClient(t).Me(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	// Without customer_id: the key's own organisation, or an error for an admin key.
	mine := resource.TestStep{
		Config: `
data "exaconnect_me" "me" {}
data "exaconnect_sites" "mine" {}
`,
		Check: resource.ComposeAggregateTestCheckFunc(
			resource.TestCheckResourceAttrPair("data.exaconnect_sites.mine", "customer_id", "data.exaconnect_me.me", "customer_id"),
			resource.TestCheckResourceAttr("data.exaconnect_sites.mine", "sites.site-a.id", ids.SiteA),
		),
	}
	if me.CustomerID == nil {
		mine.Check = nil
		mine.ExpectError = regexp.MustCompile(`customer_id is needed`)
	}
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		Steps: []resource.TestStep{mine, {
			Config: fmt.Sprintf(`
data "exaconnect_me" "me" {}
data "exaconnect_sites" "all" { customer_id = %q }
`, ids.Customer),
			Check: resource.ComposeAggregateTestCheckFunc(
				resource.TestMatchResourceAttr("data.exaconnect_me.me", "email", regexp.MustCompile(`@`)),
				resource.TestMatchResourceAttr("data.exaconnect_me.me", "role", regexp.MustCompile(`^(admin|customer)$`)),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "customer_id", ids.Customer),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "sites.site-a.id", ids.SiteA),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "sites.site-a.kind", "site"),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "sites.site-a.location", "Kingston"),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "sites.site-a.lan_prefixes.0", "192.168.10.0/24"),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "sites.site-b.id", ids.SiteB),
				resource.TestCheckResourceAttr("data.exaconnect_sites.all", "sites.pop-miami.kind", "pop"),
			),
		}},
	})
}

// ---- exaconnect_cloud_circuit -----------------------------------------------------

func cloudCircuitConfig(customer, body string) string {
	return fmt.Sprintf(`
data "exaconnect_sites" "all" { customer_id = %q }
resource "exaconnect_cloud_circuit" "aws" {
  customer_id   = %q
  name          = "tf-acc AWS"
  provider_name = "aws"
  region        = "us-east-1"
  peer_asn      = 64512
%s
}
`, customer, customer, body)
}

func TestAccCloudCircuit(t *testing.T) {
	ids := lab(t)
	addr := "exaconnect_cloud_circuit.aws"
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		CheckDestroy: checkGone(t, "exaconnect_cloud_circuit", func(c *client.Client, cust, id string) error {
			_, err := c.GetCircuit(context.Background(), cust, atoi(id))
			return err
		}),
		Steps: []resource.TestStep{
			{ // a resilient pair; 10.100.1.0/16 comes back as 10.100.0.0/16 without a diff
				Config: cloudCircuitConfig(ids.Customer, `
  peer_address           = "52.1.2.3"
  secondary_peer_address = "52.1.2.4"
  psk                    = "tf.acc.test.key.one"
  cloud_prefixes         = ["10.100.1.0/16", "10.101.0.0/16"]
  bandwidth_mbps         = 50
`),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttrSet(addr, "id"),
					resource.TestCheckResourceAttr(addr, "status", "provisioning"),
					resource.TestCheckResourceAttr(addr, "enabled", "true"),
					resource.TestCheckResourceAttr(addr, "site_prefixes.#", "0"),
					resource.TestCheckTypeSetElemAttr(addr, "cloud_prefixes.*", "10.100.1.0/16"),
					resource.TestMatchResourceAttr(addr, "inside_cidr", regexp.MustCompile(`^169\.254\.\d+\.\d+/30$`)),
					resource.TestMatchResourceAttr(addr, "secondary_inside_cidr", regexp.MustCompile(`^169\.254\.\d+\.\d+/30$`)),
					resource.TestMatchResourceAttr(addr, "our_inside", regexp.MustCompile(`^169\.254\.\d+\.\d+/30$`)),
					resource.TestMatchResourceAttr(addr, "cloud_inside", regexp.MustCompile(`^169\.254\.\d+\.\d+$`)),
					resource.TestCheckResourceAttr(addr, "psk", "tf.acc.test.key.one"),
				),
			},
			{ // in place: bandwidth, one tunnel only, new key, a site, a class, other prefixes
				Config: cloudCircuitConfig(ids.Customer, `
  peer_address   = "52.1.2.3"
  psk            = "tf.acc.test.key.two"
  site_id        = data.exaconnect_sites.all.sites["site-a"].id
  site_prefixes  = ["192.168.10.0/25"]
  cloud_prefixes = ["10.100.0.0/16"]
  class_name     = "business"
  bandwidth_mbps = 100
`),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "bandwidth_mbps", "100"),
					resource.TestCheckNoResourceAttr(addr, "secondary_peer_address"),
					resource.TestCheckNoResourceAttr(addr, "secondary_inside_cidr"),
					resource.TestCheckResourceAttr(addr, "site_id", ids.SiteA),
					resource.TestCheckResourceAttr(addr, "class_name", "business"),
					resource.TestCheckResourceAttr(addr, "cloud_prefixes.#", "1"),
				),
			},
			{ // fixed inside addresses, off, no site or class
				Config: cloudCircuitConfig(ids.Customer, `
  peer_address   = "52.1.2.5"
  psk            = "tf.acc.test.key.two"
  inside_cidr    = "169.254.103.252/30"
  bandwidth_mbps = 100
  enabled        = false
`),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "inside_cidr", "169.254.103.252/30"),
					resource.TestCheckResourceAttr(addr, "our_inside", "169.254.103.254/30"),
					resource.TestCheckResourceAttr(addr, "cloud_inside", "169.254.103.253"),
					resource.TestCheckResourceAttr(addr, "status", "off"),
					resource.TestCheckNoResourceAttr(addr, "site_id"),
					resource.TestCheckNoResourceAttr(addr, "class_name"),
					resource.TestCheckResourceAttr(addr, "site_prefixes.#", "0"),
				),
			},
			{
				ResourceName:            addr,
				ImportState:             true,
				ImportStateIdFunc:       importID(addr),
				ImportStateVerify:       true,
				ImportStateVerifyIgnore: []string{"psk"}, // write-only
			},
		},
	})
}

// ---- exaconnect_site_circuit --------------------------------------------------------

func siteCircuitConfig(ids labIDs, body string) string {
	return fmt.Sprintf(`
resource "exaconnect_site_circuit" "erp" {
  customer_id = %q
  a_site_id   = %q
  b_site_id   = %q
%s
}
`, ids.Customer, ids.SiteA, ids.SiteB, body)
}

func TestAccSiteCircuit(t *testing.T) {
	ids := lab(t)
	addr := "exaconnect_site_circuit.erp"
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		CheckDestroy: checkGone(t, "exaconnect_site_circuit", func(c *client.Client, cust, id string) error {
			_, err := c.GetCircuit(context.Background(), cust, atoi(id))
			return err
		}),
		Steps: []resource.TestStep{
			{
				Config: siteCircuitConfig(ids, `
  name           = "tf-acc ERP"
  a_vlan         = 100
  bandwidth_mbps = 20
`),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttrSet(addr, "id"),
					resource.TestCheckResourceAttr(addr, "b_vlan", "100"),
					resource.TestCheckResourceAttr(addr, "status", "provisioning"),
				),
			},
			{
				Config: siteCircuitConfig(ids, `
  name           = "tf-acc ERP 2"
  a_vlan         = 101
  b_vlan         = 200
  bandwidth_mbps = 40
  enabled        = false
`),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "a_vlan", "101"),
					resource.TestCheckResourceAttr(addr, "b_vlan", "200"),
					resource.TestCheckResourceAttr(addr, "bandwidth_mbps", "40"),
					resource.TestCheckResourceAttr(addr, "status", "off"),
				),
			},
			{ // b_vlan unset again: back to a_vlan
				Config: siteCircuitConfig(ids, `
  name           = "tf-acc ERP 2"
  a_vlan         = 101
  bandwidth_mbps = 40
`),
				ConfigPlanChecks: inPlace(addr),
				Check:            resource.TestCheckResourceAttr(addr, "b_vlan", "101"),
			},
			{ResourceName: addr, ImportState: true, ImportStateIdFunc: importID(addr), ImportStateVerify: true},
			{ // a circuit of the other kind cannot be imported here
				Config: siteCircuitConfig(ids, "  name = \"tf-acc ERP 2\"\n  a_vlan = 101\n  bandwidth_mbps = 40\n") + fmt.Sprintf(`
resource "exaconnect_cloud_circuit" "wrong" {
  customer_id    = %q
  name           = "wrong"
  provider_name  = "aws"
  peer_address   = "52.1.2.9"
  peer_asn       = 64512
  psk            = "tf.acc.test.key.one"
  bandwidth_mbps = 1
}
`, ids.Customer),
				ResourceName:      "exaconnect_cloud_circuit.wrong",
				ImportState:       true,
				ImportStateIdFunc: importID(addr),
				ExpectError:       regexp.MustCompile(`is a site circuit; manage it with\s+exaconnect_site_circuit`),
			},
		},
	})
}

// ---- exaconnect_firewall_rule -------------------------------------------------------

func firewallConfig(customer, body string) string {
	return fmt.Sprintf(`
resource "exaconnect_firewall_rule" "x" {
  customer_id = %q
%s
}
`, customer, body)
}

func TestAccFirewallRule(t *testing.T) {
	ids := lab(t)
	addr := "exaconnect_firewall_rule.x"
	get := func(c *client.Client, cust, id string) error {
		_, err := c.GetFirewallRule(context.Background(), cust, atoi(id))
		return err
	}
	var ruleID string
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		CheckDestroy:             checkGone(t, "exaconnect_firewall_rule", get),
		Steps: []resource.TestStep{
			{ // a bare address comes back as /32 without a diff
				Config: firewallConfig(ids.Customer, `
  action   = "deny"
  dst      = ["198.51.100.7", "203.0.113.0/24"]
  protocol = "icmp"
`),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttrSet(addr, "id"),
					resource.TestCheckResourceAttrSet(addr, "position"),
					resource.TestCheckNoResourceAttr(addr, "site_id"),
					resource.TestCheckResourceAttr(addr, "ports", ""),
					resource.TestCheckResourceAttr(addr, "enabled", "true"),
					resource.TestCheckTypeSetElemAttr(addr, "dst.*", "198.51.100.7"),
					func(s *terraform.State) error {
						ruleID = s.RootModule().Resources[addr].Primary.ID
						return nil
					},
				),
			},
			{
				Config: firewallConfig(ids.Customer, fmt.Sprintf(`
  action      = "allow"
  site_id     = %q
  src         = ["192.168.10.0/24"]
  dst         = ["198.51.100.0/24"]
  protocol    = "tcp"
  ports       = "443,8000-8100"
  description = "tf-acc web"
  enabled     = false
`, ids.SiteA)),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "action", "allow"),
					resource.TestCheckResourceAttr(addr, "site_id", ids.SiteA),
					resource.TestCheckResourceAttr(addr, "ports", "443,8000-8100"),
					resource.TestCheckResourceAttr(addr, "enabled", "false"),
				),
			},
			{ResourceName: addr, ImportState: true, ImportStateIdFunc: importID(addr), ImportStateVerify: true},
			{ // removed outside Terraform: read drops it and the plan creates it again
				PreConfig: func() {
					if err := testClient(t).DeleteFirewallRule(context.Background(), ids.Customer, atoi(ruleID)); err != nil {
						t.Fatal(err)
					}
				},
				RefreshState:       true,
				ExpectNonEmptyPlan: true,
			},
		},
	})
}

// ---- exaconnect_port_forward ---------------------------------------------------------

func forwardConfig(ids labIDs, body string) string {
	return fmt.Sprintf(`
resource "exaconnect_port_forward" "web" {
  customer_id = %q
  to_site_id  = %q
%s
}
`, ids.Customer, ids.SiteA, body)
}

func TestAccPortForward(t *testing.T) {
	ids := lab(t)
	addr := "exaconnect_port_forward.web"
	// The public port is shared by every organisation on the PoP.
	port := 40000 + rand.Intn(20000)
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		CheckDestroy: checkGone(t, "exaconnect_port_forward", func(c *client.Client, cust, id string) error {
			_, err := c.GetPortForward(context.Background(), cust, atoi(id))
			return err
		}),
		Steps: []resource.TestStep{
			{
				Config: forwardConfig(ids, fmt.Sprintf(`
  protocol   = "tcp"
  port       = %d
  to_address = "192.168.10.10"
`, port)),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttrSet(addr, "id"),
					resource.TestCheckResourceAttr(addr, "to_port", fmt.Sprint(port)),
					resource.TestCheckResourceAttr(addr, "allow_from.#", "0"),
					resource.TestCheckResourceAttr(addr, "description", ""),
				),
			},
			{
				Config: forwardConfig(ids, fmt.Sprintf(`
  protocol    = "tcp"
  port        = %d
  to_address  = "192.168.10.11"
  to_port     = 8443
  allow_from  = ["203.0.113.9", "198.51.100.0/24"]
  description = "tf-acc web"
`, port)),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "to_port", "8443"),
					resource.TestCheckResourceAttr(addr, "to_address", "192.168.10.11"),
					resource.TestCheckResourceAttr(addr, "allow_from.#", "2"),
				),
			},
			{ // to_port unset again: back to the public port
				Config: forwardConfig(ids, fmt.Sprintf(`
  protocol    = "udp"
  port        = %d
  to_address  = "192.168.10.11"
  description = "tf-acc web"
`, port+1)),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "to_port", fmt.Sprint(port+1)),
					resource.TestCheckResourceAttr(addr, "protocol", "udp"),
					resource.TestCheckResourceAttr(addr, "allow_from.#", "0"),
				),
			},
			{ResourceName: addr, ImportState: true, ImportStateIdFunc: importID(addr), ImportStateVerify: true},
		},
	})
}

// ---- exaconnect_site_internet --------------------------------------------------------

func TestAccSiteInternet(t *testing.T) {
	ids := lab(t)
	addr := "exaconnect_site_internet.b"
	config := func(mode string) string {
		return fmt.Sprintf(`
resource "exaconnect_site_internet" "b" {
  customer_id = %q
  site_id     = %q
  mode        = %q
}
`, ids.Customer, ids.SiteB, mode)
	}
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		CheckDestroy: func(*terraform.State) error {
			s, err := testClient(t).GetSiteInternet(context.Background(), ids.Customer, ids.SiteB)
			if err != nil {
				return err
			}
			if s.Mode != "pop" {
				return fmt.Errorf("destroy should set the mode back to pop, it is %s", s.Mode)
			}
			return nil
		},
		Steps: []resource.TestStep{
			{
				Config:      config("sideways"),
				ExpectError: regexp.MustCompile(`value must be one of`),
			},
			{
				Config: config("local"),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "id", ids.SiteB),
					resource.TestCheckResourceAttr(addr, "mode", "local"),
				),
			},
			{
				Config:           config("off"),
				ConfigPlanChecks: inPlace(addr),
				Check:            resource.TestCheckResourceAttr(addr, "mode", "off"),
			},
			{
				ResourceName:      addr,
				ImportState:       true,
				ImportStateId:     ids.Customer + "/" + ids.SiteB,
				ImportStateVerify: true,
			},
		},
	})
}

// ---- exaconnect_traffic_rule ---------------------------------------------------------

func trafficConfig(customer, body string) string {
	return fmt.Sprintf(`
resource "exaconnect_traffic_rule" "teams" {
  customer_id = %q
%s
}
`, customer, body)
}

func TestAccTrafficRule(t *testing.T) {
	ids := lab(t)
	addr := "exaconnect_traffic_rule.teams"
	resource.Test(t, resource.TestCase{
		ProtoV6ProviderFactories: testAccProviders,
		CheckDestroy: checkGone(t, "exaconnect_traffic_rule", func(c *client.Client, cust, id string) error {
			_, err := c.GetTrafficRule(context.Background(), cust, atoi(id))
			return err
		}),
		Steps: []resource.TestStep{
			{ // the API lower-cases names and turns 10.9.9.9 into 10.9.9.9/32: no diff
				Config: trafficConfig(ids.Customer, `
  name        = "tf-acc Teams"
  class_name  = "voice"
  ports       = "udp:3478-3481"
  domains     = ["Teams.Microsoft.com"]
  dst_subnets = ["10.9.9.9"]
  dscp        = [46, 34]
`),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttrSet(addr, "id"),
					resource.TestCheckResourceAttr(addr, "ordinal", "100"),
					resource.TestCheckResourceAttr(addr, "enabled", "true"),
					resource.TestCheckResourceAttr(addr, "dscp.#", "2"),
					resource.TestCheckTypeSetElemAttr(addr, "domains.*", "Teams.Microsoft.com"),
				),
			},
			{
				Config: trafficConfig(ids.Customer, fmt.Sprintf(`
  name        = "tf-acc Teams media"
  class_name  = "business"
  site_ids    = [%q]
  ports       = "udp:3478-3481 tcp:443"
  domains     = ["teams.microsoft.com", "zoom.us"]
  src_subnets = ["192.168.10.0/24"]
  vlans       = [100]
  ordinal     = 20
  enabled     = false
`, ids.SiteA)),
				ConfigPlanChecks: inPlace(addr),
				Check: resource.ComposeAggregateTestCheckFunc(
					resource.TestCheckResourceAttr(addr, "class_name", "business"),
					resource.TestCheckResourceAttr(addr, "ordinal", "20"),
					resource.TestCheckResourceAttr(addr, "dscp.#", "0"),
					resource.TestCheckResourceAttr(addr, "dst_subnets.#", "0"),
					resource.TestCheckResourceAttr(addr, "domains.#", "2"),
					resource.TestCheckResourceAttr(addr, "site_ids.#", "1"),
				),
			},
			{ResourceName: addr, ImportState: true, ImportStateIdFunc: importID(addr), ImportStateVerify: true},
			{ // the API's own error comes through
				Config: trafficConfig(ids.Customer, `
  name       = "tf-acc Teams media"
  class_name = "no-such-class"
  ports      = "udp:3478"
`),
				ExpectError: regexp.MustCompile(`There is no class called 'no-such-class'`),
			},
		},
	})
}
