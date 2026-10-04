package client

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

const testKey = "exa_test_not_a_real_key"

type call struct {
	Method, Path, Auth string
	Body               map[string]any
}

// fake answers every request with the handler's (status, body) and records it.
func fake(t *testing.T, handler func(c call) (int, string)) (*Client, *[]call) {
	t.Helper()
	var calls []call
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c := call{Method: r.Method, Path: r.URL.Path, Auth: r.Header.Get("Authorization")}
		if b, _ := io.ReadAll(r.Body); len(b) > 0 {
			if err := json.Unmarshal(b, &c.Body); err != nil {
				t.Errorf("request body is not JSON: %s", b)
			}
		}
		calls = append(calls, c)
		status, body := handler(c)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, body)
	}))
	t.Cleanup(srv.Close)
	cl, err := New(srv.URL, testKey, srv.Client())
	if err != nil {
		t.Fatal(err)
	}
	return cl, &calls
}

func TestNewChecksURLAndKey(t *testing.T) {
	for _, u := range []string{"", "connect.exacarib.com", "ftp://x", "https://"} {
		if _, err := New(u, testKey, nil); err == nil {
			t.Errorf("New(%q) should fail", u)
		}
	}
	if _, err := New("https://connect.exacarib.com", "", nil); err == nil {
		t.Error("an empty key should fail")
	}
	for _, u := range []string{"https://c.example", "https://c.example/", "https://c.example/api/v1", "https://c.example/api/v1/"} {
		c, err := New(u, testKey, nil)
		if err != nil {
			t.Fatal(err)
		}
		if c.baseURL != "https://c.example/api/v1" {
			t.Errorf("New(%q).baseURL = %q", u, c.baseURL)
		}
		if strings.Contains(c.String(), testKey) || strings.Contains(c.String(), "exa_") {
			t.Errorf("String() shows the key: %s", c)
		}
	}
}

func TestBearerAuthAndMe(t *testing.T) {
	cl, calls := fake(t, func(c call) (int, string) {
		return 200, `{"email":"ops@example.org","role":"customer","customer_id":"c1"}`
	})
	me, err := cl.Me(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if me.Email != "ops@example.org" || me.Role != "customer" || me.CustomerID == nil || *me.CustomerID != "c1" {
		t.Errorf("me = %+v", me)
	}
	got := (*calls)[0]
	if got.Method != "GET" || got.Path != "/api/v1/auth/me" || got.Auth != "Bearer "+testKey {
		t.Errorf("request = %+v", got)
	}
}

func TestErrorsCarryDetail(t *testing.T) {
	cases := []struct {
		status int
		body   string
		want   string
	}{
		{400, `{"detail":"The second gateway address must differ from the first."}`, "The second gateway address must differ from the first."},
		{422, `{"detail":[{"loc":["body","bandwidth_mbps"],"msg":"Input should be less than or equal to 1000","type":"x"}]}`,
			"bandwidth_mbps: Input should be less than or equal to 1000"},
		{502, `Bad gateway`, "Bad gateway"},
		{401, `{"detail":"This API key has been revoked or has expired."}`, "This API key has been revoked or has expired."},
	}
	for _, tc := range cases {
		cl, _ := fake(t, func(call) (int, string) { return tc.status, tc.body })
		_, err := cl.Me(context.Background())
		var apiErr *APIError
		if err == nil || !asAPIError(err, &apiErr) {
			t.Fatalf("want an APIError, got %v", err)
		}
		if apiErr.Status != tc.status || apiErr.Detail != tc.want {
			t.Errorf("got %d %q, want %d %q", apiErr.Status, apiErr.Detail, tc.status, tc.want)
		}
		if !strings.Contains(err.Error(), tc.want) || strings.Contains(err.Error(), testKey) {
			t.Errorf("Error() = %q", err.Error())
		}
		if IsNotFound(err) {
			t.Errorf("%d is not a 404", tc.status)
		}
	}
}

func asAPIError(err error, target **APIError) bool {
	e, ok := err.(*APIError)
	if ok {
		*target = e
	}
	return ok
}

func TestGetCircuitFindsInListOr404(t *testing.T) {
	cl, calls := fake(t, func(call) (int, string) {
		return 200, `[{"id":1,"name":"a","kind":"cloud","provider":"aws","region":"us-east-1","peer_address":"52.1.2.3",
		  "peer_asn":64512,"inside_cidr":"169.254.100.0/30","our_inside":"169.254.100.2/30","cloud_inside":"169.254.100.1",
		  "cloud_prefixes":["10.100.0.0/16"],"a_prefixes":[],"bandwidth_mbps":50,"enabled":true,"has_psk":true,
		  "status":"provisioning","secondary_peer_address":null,"tunnels":[{"which":"primary","status":"provisioning"}]},
		 {"id":2,"name":"b","kind":"site","a_site_id":"s1","b_site_id":"s2","a_vlan":100,"b_vlan":100,
		  "bandwidth_mbps":10,"enabled":true,"status":"provisioning","region":""}]`
	})
	c, err := cl.GetCircuit(context.Background(), "cust", 2)
	if err != nil {
		t.Fatal(err)
	}
	if c.Kind != "site" || c.AVLAN == nil || *c.AVLAN != 100 || *c.BSiteID != "s2" {
		t.Errorf("circuit = %+v", c)
	}
	if (*calls)[0].Path != "/api/v1/customers/cust/circuits" {
		t.Errorf("path = %s", (*calls)[0].Path)
	}
	_, err = cl.GetCircuit(context.Background(), "cust", 3)
	if !IsNotFound(err) {
		t.Errorf("a missing circuit should be a 404, got %v", err)
	}
}

func TestCircuitBodies(t *testing.T) {
	cl, calls := fake(t, func(c call) (int, string) {
		if c.Method == "DELETE" {
			return 204, ""
		}
		return 200, `{"id":7,"name":"x","kind":"cloud","bandwidth_mbps":100,"enabled":true,"status":"off"}`
	})
	ctx := context.Background()
	psk := "abcdefgh12"
	provider := "aws"
	if _, err := cl.CreateCircuit(ctx, "cust", CircuitIn{Name: "x", Kind: "cloud", BandwidthMbps: 50, Provider: &provider, PSK: &psk}); err != nil {
		t.Fatal(err)
	}
	if _, err := cl.UpdateCircuit(ctx, "cust", 7, CircuitIn{Name: "x", Kind: "cloud", BandwidthMbps: 100, Provider: &provider}); err != nil {
		t.Fatal(err)
	}
	if err := cl.DeleteCircuit(ctx, "cust", 7); err != nil {
		t.Fatal(err)
	}
	create, patch, del := (*calls)[0], (*calls)[1], (*calls)[2]
	if create.Method != "POST" || create.Body["kind"] != "cloud" || create.Body["psk"] != psk {
		t.Errorf("create = %+v", create)
	}
	// Lists go as [] so a PATCH can clear them; null means "leave as it is".
	if v, ok := create.Body["cloud_prefixes"].([]any); !ok || len(v) != 0 {
		t.Errorf("cloud_prefixes = %#v", create.Body["cloud_prefixes"])
	}
	if patch.Method != "PATCH" || patch.Path != "/api/v1/customers/cust/circuits/7" {
		t.Errorf("patch = %+v", patch)
	}
	if _, ok := patch.Body["kind"]; ok {
		t.Error("PATCH must not send kind")
	}
	if v, ok := patch.Body["secondary_peer_address"]; !ok || v != nil {
		t.Error("PATCH must send secondary_peer_address: null to remove a second tunnel")
	}
	if v, ok := patch.Body["psk"]; !ok || v != nil {
		t.Errorf("an unchanged psk is sent as null, got %#v", v)
	}
	if del.Method != "DELETE" || del.Path != "/api/v1/customers/cust/circuits/7" {
		t.Errorf("delete = %+v", del)
	}
}

func TestInternetItems(t *testing.T) {
	cl, calls := fake(t, func(c call) (int, string) {
		switch {
		case c.Method == "PATCH" && strings.Contains(c.Path, "/internet/sites/"):
			return 200, `{"sites":[{"id":"s1","name":"site-a","mode":"local"}],"rules":[],"forwards":[]}`
		case c.Method == "GET":
			return 200, `{"public_address":"100.64.0.2","sites":[{"id":"s1","name":"site-a","mode":"pop"}],
			  "rules":[{"id":4,"position":1,"site_id":null,"action":"deny","src":[],"dst":["198.51.100.0/24"],
			            "protocol":"icmp","ports":"","description":"","enabled":true,"packets":0,"bytes":0}],
			  "forwards":[{"id":9,"description":"web","protocol":"tcp","port":8080,"to_site_id":"s1",
			               "to_address":"192.168.10.10","to_port":80,"allow_from":[],"enabled":true,"active":true}]}`
		case c.Method == "POST" && strings.HasSuffix(c.Path, "/firewall/rules"):
			return 201, `{"id":5,"position":2,"action":"allow","src":[],"dst":[],"protocol":"any","ports":"","description":"","enabled":true}`
		}
		return 404, `{"detail":"Not Found"}`
	})
	ctx := context.Background()
	s, err := cl.GetSiteInternet(ctx, "cust", "S1")
	if err != nil || s.Mode != "pop" {
		t.Fatalf("site = %+v, %v", s, err)
	}
	if _, err := cl.GetSiteInternet(ctx, "cust", "pop"); !IsNotFound(err) {
		t.Errorf("an unknown site should be a 404, got %v", err)
	}
	s, err = cl.SetSiteInternet(ctx, "cust", "s1", "local")
	if err != nil || s.Mode != "local" {
		t.Fatalf("set = %+v, %v", s, err)
	}
	r, err := cl.GetFirewallRule(ctx, "cust", 4)
	if err != nil || r.Action != "deny" || r.SiteID != nil || r.Dst[0] != "198.51.100.0/24" {
		t.Fatalf("rule = %+v, %v", r, err)
	}
	if _, err := cl.GetFirewallRule(ctx, "cust", 99); !IsNotFound(err) {
		t.Errorf("want 404, got %v", err)
	}
	f, err := cl.GetPortForward(ctx, "cust", 9)
	if err != nil || f.ToPort != 80 || !f.Active {
		t.Fatalf("forward = %+v, %v", f, err)
	}
	if _, err := cl.CreateFirewallRule(ctx, "cust", FirewallRuleIn{Action: "allow"}); err != nil {
		t.Fatal(err)
	}
	last := (*calls)[len(*calls)-1]
	if v, ok := last.Body["src"].([]any); !ok || len(v) != 0 {
		t.Errorf("src should be sent as [], got %#v", last.Body["src"])
	}
	if v, ok := last.Body["site_id"]; !ok || v != nil {
		t.Errorf("site_id should be sent as null for every site, got %#v", v)
	}
}

func TestTrafficRuleUsesPut(t *testing.T) {
	cl, calls := fake(t, func(c call) (int, string) {
		if c.Method == "GET" {
			return 200, `[{"id":3,"name":"Teams","class_name":"voice","site_ids":[],"apps":["teams"],"ports":"",
			  "dst_subnets":[],"src_subnets":[],"vlans":[],"domains":[],"dscp":[46],"enabled":true,"ordinal":100}]`
		}
		return 200, `{"id":3,"name":"Teams","class_name":"voice","enabled":false,"ordinal":100}`
	})
	ctx := context.Background()
	if _, err := cl.UpdateTrafficRule(ctx, "cust", 3, TrafficRuleIn{Name: "Teams", ClassName: "voice"}); err != nil {
		t.Fatal(err)
	}
	put := (*calls)[0]
	if put.Method != "PUT" || put.Path != "/api/v1/customers/cust/rules/3" {
		t.Errorf("update = %+v", put)
	}
	for _, k := range []string{"site_ids", "apps", "dst_subnets", "src_subnets", "vlans", "domains", "dscp"} {
		if v, ok := put.Body[k].([]any); !ok || len(v) != 0 {
			t.Errorf("%s should be sent as [], got %#v", k, put.Body[k])
		}
	}
	r, err := cl.GetTrafficRule(ctx, "cust", 3)
	if err != nil || r.DSCP[0] != 46 || r.Apps[0] != "teams" {
		t.Fatalf("rule = %+v, %v", r, err)
	}
	if _, err := cl.GetTrafficRule(ctx, "cust", 4); !IsNotFound(err) {
		t.Errorf("want 404, got %v", err)
	}
}
