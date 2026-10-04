package provider

import (
	"context"
	"fmt"
	"os"
	"testing"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"
	"github.com/hashicorp/terraform-plugin-go/tfprotov6"
	"github.com/hashicorp/terraform-plugin-testing/terraform"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

// Acceptance tests (TF_ACC=1) run against a real controller:
//
//	EXACONNECT_URL      the controller, like http://127.0.0.1:8010
//	EXACONNECT_API_KEY  an API key or session token (admin or customer)
//	EXACONNECT_TEST_CUSTOMER_ID  optional; defaults to the key's organisation,
//	                    or for an admin to "Demo Organisation" (seed --lab)
//
// They expect the lab inventory: sites site-a (192.168.10.0/24) and site-b
// (192.168.20.0/24) and the classes voice, business and bulk.

var testAccProviders = map[string]func() (tfprotov6.ProviderServer, error){
	"exaconnect": providerserver.NewProtocol6WithError(New("test")()),
}

func testAccPreCheck(t *testing.T) {
	t.Helper()
	for _, k := range []string{"EXACONNECT_URL", "EXACONNECT_API_KEY"} {
		if os.Getenv(k) == "" {
			t.Fatalf("%s must be set for acceptance tests", k)
		}
	}
}

func testClient(t *testing.T) *client.Client {
	t.Helper()
	c, err := client.New(os.Getenv("EXACONNECT_URL"), os.Getenv("EXACONNECT_API_KEY"), nil)
	if err != nil {
		t.Fatal(err)
	}
	return c
}

type labIDs struct {
	Customer, SiteA, SiteB string
}

// lab finds the test organisation and its two sites.
func lab(t *testing.T) labIDs {
	t.Helper()
	if os.Getenv("TF_ACC") == "" {
		t.Skip("acceptance tests need TF_ACC=1")
	}
	testAccPreCheck(t)
	ctx := context.Background()
	c := testClient(t)
	ids := labIDs{Customer: os.Getenv("EXACONNECT_TEST_CUSTOMER_ID")}
	if ids.Customer == "" {
		me, err := c.Me(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if me.CustomerID != nil {
			ids.Customer = *me.CustomerID
		} else {
			var customers []struct {
				ID   string `json:"id"`
				Name string `json:"name"`
			}
			if err := c.Do(ctx, "GET", "/customers", nil, &customers); err != nil {
				t.Fatal(err)
			}
			for _, cu := range customers {
				if cu.Name == "Demo Organisation" {
					ids.Customer = cu.ID
				}
			}
		}
	}
	if ids.Customer == "" {
		t.Fatal("no test organisation: set EXACONNECT_TEST_CUSTOMER_ID or seed the lab")
	}
	sites, err := c.Sites(ctx)
	if err != nil {
		t.Fatal(err)
	}
	for _, s := range sites {
		if s.CustomerID != ids.Customer {
			continue
		}
		switch s.Name {
		case "site-a":
			ids.SiteA = s.ID
		case "site-b":
			ids.SiteB = s.ID
		}
	}
	if ids.SiteA == "" || ids.SiteB == "" {
		t.Fatal("the test organisation needs sites site-a and site-b (seed --lab)")
	}
	return ids
}

// importID is "<customer_id>/<id>" from a resource in state.
func importID(addr string) func(*terraform.State) (string, error) {
	return func(s *terraform.State) (string, error) {
		rs, ok := s.RootModule().Resources[addr]
		if !ok {
			return "", fmt.Errorf("%s not in state", addr)
		}
		return rs.Primary.Attributes["customer_id"] + "/" + rs.Primary.ID, nil
	}
}

// checkGone confirms every resource of this type in state is gone from the API.
func checkGone(t *testing.T, typ string, get func(c *client.Client, customer, id string) error) func(*terraform.State) error {
	return func(s *terraform.State) error {
		c := testClient(t)
		for _, rs := range s.RootModule().Resources {
			if rs.Type != typ {
				continue
			}
			err := get(c, rs.Primary.Attributes["customer_id"], rs.Primary.ID)
			if err == nil {
				return fmt.Errorf("%s %s still exists", typ, rs.Primary.ID)
			}
			if !client.IsNotFound(err) {
				return err
			}
		}
		return nil
	}
}

func atoi(s string) int64 {
	var n int64
	_, _ = fmt.Sscan(s, &n)
	return n
}

func TestProviderSchemaIsValid(t *testing.T) {
	ctx := context.Background()
	srv, err := testAccProviders["exaconnect"]()
	if err != nil {
		t.Fatal(err)
	}
	resp, err := srv.GetProviderSchema(ctx, &tfprotov6.GetProviderSchemaRequest{})
	if err != nil {
		t.Fatal(err)
	}
	for _, d := range resp.Diagnostics {
		t.Errorf("%s: %s", d.Summary, d.Detail)
	}
	for _, want := range []string{
		"exaconnect_cloud_circuit", "exaconnect_site_circuit", "exaconnect_firewall_rule",
		"exaconnect_port_forward", "exaconnect_site_internet", "exaconnect_traffic_rule",
	} {
		if _, ok := resp.ResourceSchemas[want]; !ok {
			t.Errorf("missing resource %s", want)
		}
	}
	for _, want := range []string{"exaconnect_me", "exaconnect_sites"} {
		if _, ok := resp.DataSourceSchemas[want]; !ok {
			t.Errorf("missing data source %s", want)
		}
	}
	for _, a := range resp.Provider.Block.Attributes {
		if a.Name == "api_key" && !a.Sensitive {
			t.Error("api_key must be sensitive")
		}
	}
	if psk := findAttr(resp.ResourceSchemas["exaconnect_cloud_circuit"], "psk"); psk == nil || !psk.Sensitive {
		t.Error("psk must be sensitive")
	}
}

func findAttr(s *tfprotov6.Schema, name string) *tfprotov6.SchemaAttribute {
	if s == nil {
		return nil
	}
	for _, a := range s.Block.Attributes {
		if a.Name == name {
			return a
		}
	}
	return nil
}
