// Package client is a small client for the ExaConnect controller's REST API
// (/api/v1), covering what the Terraform provider manages.
package client

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// Client talks to one controller as one person (an API key or a session token).
type Client struct {
	baseURL   string // ends in /api/v1, no trailing slash
	apiKey    string
	http      *http.Client
	UserAgent string
}

// New returns a client for the controller at rawURL (for example
// https://connect.exacarib.com). "/api/v1" is added unless it is already there.
func New(rawURL, apiKey string, httpClient *http.Client) (*Client, error) {
	u, err := url.Parse(strings.TrimSpace(rawURL))
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, fmt.Errorf("the controller URL must look like https://connect.exacarib.com, not %q", rawURL)
	}
	base := strings.TrimRight(u.String(), "/")
	if !strings.HasSuffix(base, "/api/v1") {
		base += "/api/v1"
	}
	if apiKey == "" {
		return nil, errors.New("an API key is needed")
	}
	if httpClient == nil {
		httpClient = &http.Client{Timeout: 30 * time.Second}
	}
	return &Client{baseURL: base, apiKey: apiKey, http: httpClient, UserAgent: "terraform-provider-exaconnect"}, nil
}

// String never shows the key.
func (c *Client) String() string { return fmt.Sprintf("exaconnect.Client(%s)", c.baseURL) }

// APIError is a non-2xx answer, with the API's plain-English detail.
type APIError struct {
	Method string
	Path   string
	Status int
	Detail string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("%s %s: %d %s: %s", e.Method, e.Path, e.Status, http.StatusText(e.Status), e.Detail)
}

// IsNotFound reports whether err is a 404 from the API.
func IsNotFound(err error) bool {
	var e *APIError
	return errors.As(err, &e) && e.Status == http.StatusNotFound
}

// detail turns FastAPI's error body into one line: {"detail": "..."} or,
// for request validation, {"detail": [{"loc": [...], "msg": "..."}]}.
func detail(body []byte) string {
	var d struct {
		Detail json.RawMessage `json:"detail"`
	}
	if json.Unmarshal(body, &d) != nil || len(d.Detail) == 0 {
		s := strings.TrimSpace(string(body))
		if len(s) > 300 {
			s = s[:300] + "..."
		}
		return s
	}
	var s string
	if json.Unmarshal(d.Detail, &s) == nil {
		return s
	}
	var items []struct {
		Loc []any  `json:"loc"`
		Msg string `json:"msg"`
	}
	if json.Unmarshal(d.Detail, &items) == nil && len(items) > 0 {
		parts := make([]string, 0, len(items))
		for _, it := range items {
			loc := make([]string, 0, len(it.Loc))
			for _, l := range it.Loc {
				if l == "body" {
					continue
				}
				loc = append(loc, fmt.Sprint(l))
			}
			if len(loc) > 0 {
				parts = append(parts, strings.Join(loc, ".")+": "+it.Msg)
			} else {
				parts = append(parts, it.Msg)
			}
		}
		return strings.Join(parts, "; ")
	}
	return string(d.Detail)
}

// Do sends a request and decodes a JSON answer into out (when out is not nil).
func (c *Client) Do(ctx context.Context, method, path string, in, out any) error {
	var body io.Reader
	if in != nil {
		b, err := json.Marshal(in)
		if err != nil {
			return err
		}
		body = bytes.NewReader(b)
	}
	req, err := http.NewRequestWithContext(ctx, method, c.baseURL+path, body)
	if err != nil {
		return err
	}
	req.Header.Set("Authorization", "Bearer "+c.apiKey)
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", c.UserAgent)
	if in != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer resp.Body.Close()
	data, err := io.ReadAll(io.LimitReader(resp.Body, 16<<20))
	if err != nil {
		return fmt.Errorf("%s %s: reading the answer: %w", method, path, err)
	}
	if resp.StatusCode < 200 || resp.StatusCode > 299 {
		return &APIError{Method: method, Path: path, Status: resp.StatusCode, Detail: detail(data)}
	}
	if out == nil || resp.StatusCode == http.StatusNoContent || len(data) == 0 {
		return nil
	}
	if err := json.Unmarshal(data, out); err != nil {
		return fmt.Errorf("%s %s: unexpected answer: %w", method, path, err)
	}
	return nil
}

func notFound(path, what string) error {
	return &APIError{Method: http.MethodGet, Path: path, Status: http.StatusNotFound, Detail: what + " not found."}
}

func esc(s string) string { return url.PathEscape(s) }

// ---- who and where --------------------------------------------------------

type Me struct {
	Email      string  `json:"email"`
	Role       string  `json:"role"`
	CustomerID *string `json:"customer_id"`
}

func (c *Client) Me(ctx context.Context) (*Me, error) {
	var m Me
	return &m, c.Do(ctx, http.MethodGet, "/auth/me", nil, &m)
}

type Site struct {
	ID          string   `json:"id"`
	CustomerID  string   `json:"customer_id"`
	Name        string   `json:"name"`
	Kind        string   `json:"kind"`
	Location    string   `json:"location"`
	Timezone    string   `json:"timezone"`
	ASN         *int64   `json:"asn"`
	LANPrefixes []string `json:"lan_prefixes"`
}

// Sites lists the sites the signed-in person can see: their organisation's,
// or every organisation's for an admin.
func (c *Client) Sites(ctx context.Context) ([]Site, error) {
	var s []Site
	return s, c.Do(ctx, http.MethodGet, "/sites", nil, &s)
}

// ---- circuits ---------------------------------------------------------------

// CircuitIn is the body of POST and PATCH /customers/{cid}/circuits. Every
// field is sent; the API treats null as "leave as it is", except
// secondary_peer_address (null removes the second tunnel). Prefix lists are
// always sent, [] when empty, so a PATCH can clear them.
type CircuitIn struct {
	Name                 string   `json:"name"`
	Kind                 string   `json:"kind,omitempty"` // create only
	BandwidthMbps        int64    `json:"bandwidth_mbps"`
	Enabled              *bool    `json:"enabled"`
	ASiteID              *string  `json:"a_site_id"`
	APrefixes            []string `json:"a_prefixes"`
	AVLAN                *int64   `json:"a_vlan"`
	BSiteID              *string  `json:"b_site_id"`
	BVLAN                *int64   `json:"b_vlan"`
	Provider             *string  `json:"provider"`
	Region               *string  `json:"region"`
	PeerAddress          *string  `json:"peer_address"`
	PeerASN              *int64   `json:"peer_asn"`
	InsideCIDR           *string  `json:"inside_cidr"`
	SecondaryPeerAddress *string  `json:"secondary_peer_address"`
	SecondaryInsideCIDR  *string  `json:"secondary_inside_cidr"`
	PSK                  *string  `json:"psk"`
	CloudPrefixes        []string `json:"cloud_prefixes"`
	ClassName            *string  `json:"class_name"`
}

func nonNil[T any](s []T) []T {
	if s == nil {
		return []T{}
	}
	return s
}

type Tunnel struct {
	Which            string  `json:"which"`
	PeerAddress      *string `json:"peer_address"`
	IKE              string  `json:"ike"`
	BGP              string  `json:"bgp"`
	PrefixesReceived int64   `json:"prefixes_received"`
	Status           string  `json:"status"`
}

type Circuit struct {
	ID                   int64    `json:"id"`
	Name                 string   `json:"name"`
	Kind                 string   `json:"kind"`
	ASiteID              *string  `json:"a_site_id"`
	ASite                *string  `json:"a_site"`
	APrefixes            []string `json:"a_prefixes"`
	AVLAN                *int64   `json:"a_vlan"`
	BSiteID              *string  `json:"b_site_id"`
	BSite                *string  `json:"b_site"`
	BVLAN                *int64   `json:"b_vlan"`
	Provider             *string  `json:"provider"`
	Region               string   `json:"region"`
	PeerAddress          *string  `json:"peer_address"`
	PeerASN              *int64   `json:"peer_asn"`
	InsideCIDR           *string  `json:"inside_cidr"`
	OurInside            *string  `json:"our_inside"`
	CloudInside          *string  `json:"cloud_inside"`
	SecondaryPeerAddress *string  `json:"secondary_peer_address"`
	SecondaryInsideCIDR  *string  `json:"secondary_inside_cidr"`
	Resilient            bool     `json:"resilient"`
	Tunnels              []Tunnel `json:"tunnels"`
	CloudPrefixes        []string `json:"cloud_prefixes"`
	ClassName            *string  `json:"class_name"`
	BandwidthMbps        int64    `json:"bandwidth_mbps"`
	PricePerMbpsMonth    float64  `json:"price_per_mbps_month"`
	Enabled              bool     `json:"enabled"`
	HasPSK               bool     `json:"has_psk"`
	Status               string   `json:"status"`
}

func circuitsPath(customerID string) string { return "/customers/" + esc(customerID) + "/circuits" }

func (c *Client) ListCircuits(ctx context.Context, customerID string) ([]Circuit, error) {
	var out []Circuit
	return out, c.Do(ctx, http.MethodGet, circuitsPath(customerID), nil, &out)
}

// GetCircuit finds one circuit in the list (the API has no GET for one).
// A deleted or unknown circuit is a 404 APIError.
func (c *Client) GetCircuit(ctx context.Context, customerID string, id int64) (*Circuit, error) {
	all, err := c.ListCircuits(ctx, customerID)
	if err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].ID == id {
			return &all[i], nil
		}
	}
	return nil, notFound(fmt.Sprintf("%s/%d", circuitsPath(customerID), id), "Circuit")
}

func (c *Client) CreateCircuit(ctx context.Context, customerID string, in CircuitIn) (*Circuit, error) {
	var out Circuit
	in.APrefixes, in.CloudPrefixes = nonNil(in.APrefixes), nonNil(in.CloudPrefixes)
	return &out, c.Do(ctx, http.MethodPost, circuitsPath(customerID), in, &out)
}

func (c *Client) UpdateCircuit(ctx context.Context, customerID string, id int64, in CircuitIn) (*Circuit, error) {
	var out Circuit
	in.Kind = ""
	in.APrefixes, in.CloudPrefixes = nonNil(in.APrefixes), nonNil(in.CloudPrefixes)
	return &out, c.Do(ctx, http.MethodPatch, fmt.Sprintf("%s/%d", circuitsPath(customerID), id), in, &out)
}

func (c *Client) DeleteCircuit(ctx context.Context, customerID string, id int64) error {
	return c.Do(ctx, http.MethodDelete, fmt.Sprintf("%s/%d", circuitsPath(customerID), id), nil, nil)
}

// ---- internet: breakout mode, firewall rules, port forwards -------------------

type InternetSite struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Mode     string `json:"mode"`
	Via      string `json:"via"`
	ViaLabel string `json:"via_label"`
}

type FirewallRule struct {
	ID          int64    `json:"id"`
	Position    int64    `json:"position"`
	SiteID      *string  `json:"site_id"`
	Action      string   `json:"action"`
	Src         []string `json:"src"`
	Dst         []string `json:"dst"`
	Protocol    string   `json:"protocol"`
	Ports       string   `json:"ports"`
	Description string   `json:"description"`
	Enabled     bool     `json:"enabled"`
}

type PortForward struct {
	ID          int64    `json:"id"`
	Description string   `json:"description"`
	Protocol    string   `json:"protocol"`
	Port        int64    `json:"port"`
	ToSiteID    string   `json:"to_site_id"`
	ToAddress   string   `json:"to_address"`
	ToPort      int64    `json:"to_port"`
	AllowFrom   []string `json:"allow_from"`
	Enabled     bool     `json:"enabled"`
	Active      bool     `json:"active"`
}

type Internet struct {
	PublicAddress *string        `json:"public_address"`
	Sites         []InternetSite `json:"sites"`
	Rules         []FirewallRule `json:"rules"`
	Forwards      []PortForward  `json:"forwards"`
}

func (c *Client) GetInternet(ctx context.Context, customerID string) (*Internet, error) {
	var out Internet
	return &out, c.Do(ctx, http.MethodGet, "/customers/"+esc(customerID)+"/internet", nil, &out)
}

// GetSiteInternet is one site's breakout mode; a 404 APIError when the site is
// not one of the organisation's sites (the PoP has no mode).
func (c *Client) GetSiteInternet(ctx context.Context, customerID, siteID string) (*InternetSite, error) {
	inet, err := c.GetInternet(ctx, customerID)
	if err != nil {
		return nil, err
	}
	for i := range inet.Sites {
		if strings.EqualFold(inet.Sites[i].ID, siteID) {
			return &inet.Sites[i], nil
		}
	}
	return nil, notFound("/customers/"+customerID+"/internet/sites/"+siteID, "Site")
}

func (c *Client) SetSiteInternet(ctx context.Context, customerID, siteID, mode string) (*InternetSite, error) {
	var out Internet
	path := "/customers/" + esc(customerID) + "/internet/sites/" + esc(siteID)
	if err := c.Do(ctx, http.MethodPatch, path, map[string]string{"mode": mode}, &out); err != nil {
		return nil, err
	}
	for i := range out.Sites {
		if strings.EqualFold(out.Sites[i].ID, siteID) {
			return &out.Sites[i], nil
		}
	}
	return nil, notFound(path, "Site")
}

// FirewallRuleIn is the body of POST and PATCH /customers/{cid}/firewall/rules.
// site_id null means every site.
type FirewallRuleIn struct {
	SiteID      *string  `json:"site_id"`
	Action      string   `json:"action"`
	Src         []string `json:"src"`
	Dst         []string `json:"dst"`
	Protocol    string   `json:"protocol"`
	Ports       string   `json:"ports"`
	Description string   `json:"description"`
	Enabled     bool     `json:"enabled"`
}

func (c *Client) GetFirewallRule(ctx context.Context, customerID string, id int64) (*FirewallRule, error) {
	inet, err := c.GetInternet(ctx, customerID)
	if err != nil {
		return nil, err
	}
	for i := range inet.Rules {
		if inet.Rules[i].ID == id {
			return &inet.Rules[i], nil
		}
	}
	return nil, notFound(fmt.Sprintf("/customers/%s/firewall/rules/%d", customerID, id), "Rule")
}

func (c *Client) CreateFirewallRule(ctx context.Context, customerID string, in FirewallRuleIn) (*FirewallRule, error) {
	var out FirewallRule
	in.Src, in.Dst = nonNil(in.Src), nonNil(in.Dst)
	return &out, c.Do(ctx, http.MethodPost, "/customers/"+esc(customerID)+"/firewall/rules", in, &out)
}

func (c *Client) UpdateFirewallRule(ctx context.Context, customerID string, id int64, in FirewallRuleIn) (*FirewallRule, error) {
	var out FirewallRule
	in.Src, in.Dst = nonNil(in.Src), nonNil(in.Dst)
	return &out, c.Do(ctx, http.MethodPatch, fmt.Sprintf("/customers/%s/firewall/rules/%d", esc(customerID), id), in, &out)
}

func (c *Client) DeleteFirewallRule(ctx context.Context, customerID string, id int64) error {
	return c.Do(ctx, http.MethodDelete, fmt.Sprintf("/customers/%s/firewall/rules/%d", esc(customerID), id), nil, nil)
}

// PortForwardIn is the body of POST and PATCH /customers/{cid}/port-forwards.
// to_port null means the same as the public port.
type PortForwardIn struct {
	Description string   `json:"description"`
	Protocol    string   `json:"protocol"`
	Port        int64    `json:"port"`
	ToSiteID    string   `json:"to_site_id"`
	ToAddress   string   `json:"to_address"`
	ToPort      *int64   `json:"to_port"`
	AllowFrom   []string `json:"allow_from"`
	Enabled     bool     `json:"enabled"`
}

func (c *Client) GetPortForward(ctx context.Context, customerID string, id int64) (*PortForward, error) {
	inet, err := c.GetInternet(ctx, customerID)
	if err != nil {
		return nil, err
	}
	for i := range inet.Forwards {
		if inet.Forwards[i].ID == id {
			return &inet.Forwards[i], nil
		}
	}
	return nil, notFound(fmt.Sprintf("/customers/%s/port-forwards/%d", customerID, id), "Port forward")
}

func (c *Client) CreatePortForward(ctx context.Context, customerID string, in PortForwardIn) (*PortForward, error) {
	var out PortForward
	in.AllowFrom = nonNil(in.AllowFrom)
	return &out, c.Do(ctx, http.MethodPost, "/customers/"+esc(customerID)+"/port-forwards", in, &out)
}

func (c *Client) UpdatePortForward(ctx context.Context, customerID string, id int64, in PortForwardIn) (*PortForward, error) {
	var out PortForward
	in.AllowFrom = nonNil(in.AllowFrom)
	return &out, c.Do(ctx, http.MethodPatch, fmt.Sprintf("/customers/%s/port-forwards/%d", esc(customerID), id), in, &out)
}

func (c *Client) DeletePortForward(ctx context.Context, customerID string, id int64) error {
	return c.Do(ctx, http.MethodDelete, fmt.Sprintf("/customers/%s/port-forwards/%d", esc(customerID), id), nil, nil)
}

// ---- traffic rules ------------------------------------------------------------

// TrafficRuleIn is the body of POST and PUT /customers/{cid}/rules (PUT
// replaces the whole rule).
type TrafficRuleIn struct {
	Name       string   `json:"name"`
	ClassName  string   `json:"class_name"`
	SiteIDs    []string `json:"site_ids"`
	Apps       []string `json:"apps"`
	Ports      string   `json:"ports"`
	DstSubnets []string `json:"dst_subnets"`
	SrcSubnets []string `json:"src_subnets"`
	VLANs      []int64  `json:"vlans"`
	Domains    []string `json:"domains"`
	DSCP       []int64  `json:"dscp"`
	Enabled    bool     `json:"enabled"`
	Ordinal    int64    `json:"ordinal"`
}

func (in TrafficRuleIn) withLists() TrafficRuleIn {
	in.SiteIDs, in.Apps, in.Domains = nonNil(in.SiteIDs), nonNil(in.Apps), nonNil(in.Domains)
	in.DstSubnets, in.SrcSubnets = nonNil(in.DstSubnets), nonNil(in.SrcSubnets)
	in.VLANs, in.DSCP = nonNil(in.VLANs), nonNil(in.DSCP)
	return in
}

type TrafficRule struct {
	ID         int64    `json:"id"`
	Name       string   `json:"name"`
	ClassName  string   `json:"class_name"`
	SiteIDs    []string `json:"site_ids"`
	Apps       []string `json:"apps"`
	Ports      string   `json:"ports"`
	DstSubnets []string `json:"dst_subnets"`
	SrcSubnets []string `json:"src_subnets"`
	VLANs      []int64  `json:"vlans"`
	Domains    []string `json:"domains"`
	DSCP       []int64  `json:"dscp"`
	Enabled    bool     `json:"enabled"`
	Ordinal    int64    `json:"ordinal"`
	Source     string   `json:"source"`
}

func (c *Client) GetTrafficRule(ctx context.Context, customerID string, id int64) (*TrafficRule, error) {
	var all []TrafficRule
	if err := c.Do(ctx, http.MethodGet, "/customers/"+esc(customerID)+"/rules", nil, &all); err != nil {
		return nil, err
	}
	for i := range all {
		if all[i].ID == id {
			return &all[i], nil
		}
	}
	return nil, notFound(fmt.Sprintf("/customers/%s/rules/%d", customerID, id), "Rule")
}

func (c *Client) CreateTrafficRule(ctx context.Context, customerID string, in TrafficRuleIn) (*TrafficRule, error) {
	var out TrafficRule
	in = in.withLists()
	return &out, c.Do(ctx, http.MethodPost, "/customers/"+esc(customerID)+"/rules", in, &out)
}

func (c *Client) UpdateTrafficRule(ctx context.Context, customerID string, id int64, in TrafficRuleIn) (*TrafficRule, error) {
	var out TrafficRule
	in = in.withLists()
	return &out, c.Do(ctx, http.MethodPut, fmt.Sprintf("/customers/%s/rules/%d", esc(customerID), id), in, &out)
}

func (c *Client) DeleteTrafficRule(ctx context.Context, customerID string, id int64) error {
	return c.Do(ctx, http.MethodDelete, fmt.Sprintf("/customers/%s/rules/%d", esc(customerID), id), nil, nil)
}
