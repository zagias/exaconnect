package provider

import (
	"context"
	"fmt"
	"net"
	"net/netip"
	"sort"
	"strconv"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

// clientFrom takes the API client the provider configured. Nil while the
// provider is not configured yet (validation runs before Configure).
func clientFrom(data any, diags *diag.Diagnostics) *client.Client {
	if data == nil {
		return nil
	}
	c, ok := data.(*client.Client)
	if !ok {
		diags.AddError("Unexpected provider data", fmt.Sprintf("expected *client.Client, got %T", data))
		return nil
	}
	return c
}

// base is embedded by every resource: the client and the usual Configure.
type base struct {
	client *client.Client
}

func (b *base) Configure(_ context.Context, req resource.ConfigureRequest, resp *resource.ConfigureResponse) {
	b.client = clientFrom(req.ProviderData, &resp.Diagnostics)
}

type dsBase struct {
	client *client.Client
}

func (b *dsBase) Configure(_ context.Context, req datasource.ConfigureRequest, resp *datasource.ConfigureResponse) {
	b.client = clientFrom(req.ProviderData, &resp.Diagnostics)
}

// apiError adds the API's error, with its plain-English detail, as a diagnostic.
func apiError(diags *diag.Diagnostics, what string, err error) {
	diags.AddError("Could not "+what, err.Error())
}

// splitImportID parses "<customer_id>/<id>".
func splitImportID(id, what string) (string, string, error) {
	parts := strings.Split(id, "/")
	if len(parts) != 2 || parts[0] == "" || parts[1] == "" {
		return "", "", fmt.Errorf("import ID must be <customer_id>/<%s>, got %q", what, id)
	}
	return parts[0], parts[1], nil
}

// importNumeric sets customer_id and id from "<customer_id>/<numeric id>".
func importNumeric(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	cid, raw, err := splitImportID(req.ID, "id")
	if err == nil {
		if _, perr := strconv.ParseInt(raw, 10, 64); perr != nil {
			err = fmt.Errorf("import ID must be <customer_id>/<id> with a numeric id, got %q", req.ID)
		}
	}
	if err != nil {
		resp.Diagnostics.AddError("Bad import ID", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("customer_id"), cid)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), raw)...)
}

func parseID(id types.String, diags *diag.Diagnostics) int64 {
	n, err := strconv.ParseInt(id.ValueString(), 10, 64)
	if err != nil {
		diags.AddError("Bad resource ID", fmt.Sprintf("expected a number, got %q", id.ValueString()))
	}
	return n
}

func idString(n int64) types.String { return types.StringValue(strconv.FormatInt(n, 10)) }

// ---- values ---------------------------------------------------------------------

func strPtr(v types.String) *string {
	if v.IsNull() || v.IsUnknown() {
		return nil
	}
	s := v.ValueString()
	return &s
}

func int64Ptr(v types.Int64) *int64 {
	if v.IsNull() || v.IsUnknown() {
		return nil
	}
	n := v.ValueInt64()
	return &n
}

func boolPtr(v types.Bool) *bool {
	if v.IsNull() || v.IsUnknown() {
		return nil
	}
	b := v.ValueBool()
	return &b
}

// optString maps "" and nil to null.
func optString(s *string) types.String {
	if s == nil || *s == "" {
		return types.StringNull()
	}
	return types.StringValue(*s)
}

func optInt64(n *int64) types.Int64 {
	if n == nil {
		return types.Int64Null()
	}
	return types.Int64Value(*n)
}

func setStrings(ctx context.Context, s types.Set, diags *diag.Diagnostics) []string {
	out := []string{}
	if s.IsNull() || s.IsUnknown() {
		return out
	}
	diags.Append(s.ElementsAs(ctx, &out, false)...)
	sort.Strings(out)
	return out
}

func setInts(ctx context.Context, s types.Set, diags *diag.Diagnostics) []int64 {
	out := []int64{}
	if s.IsNull() || s.IsUnknown() {
		return out
	}
	diags.Append(s.ElementsAs(ctx, &out, false)...)
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	return out
}

// stringSet keeps the prior value when it means the same as what the API
// returned (the API normalises: 10.1.2.3 becomes 10.1.2.3/32, upper case
// becomes lower), so Terraform sees no change. Otherwise it takes the API's.
func stringSet(ctx context.Context, prior types.Set, api []string, norm func(string) string, diags *diag.Diagnostics) types.Set {
	if norm == nil {
		norm = func(s string) string { return s }
	}
	want := map[string]bool{}
	for _, v := range api {
		want[norm(v)] = true
	}
	if !prior.IsNull() && !prior.IsUnknown() {
		var have []string
		diags.Append(prior.ElementsAs(ctx, &have, false)...)
		got := map[string]bool{}
		for _, v := range have {
			got[norm(v)] = true
		}
		if sameKeys(got, want) {
			return prior
		}
	}
	elems := make([]attr.Value, 0, len(api))
	for _, v := range api {
		elems = append(elems, types.StringValue(v))
	}
	s, d := types.SetValue(types.StringType, elems)
	diags.Append(d...)
	return s
}

func intSet(api []int64, diags *diag.Diagnostics) types.Set {
	elems := make([]attr.Value, 0, len(api))
	for _, v := range api {
		elems = append(elems, types.Int64Value(v))
	}
	s, d := types.SetValue(types.Int64Type, elems)
	diags.Append(d...)
	return s
}

func sameKeys(a, b map[string]bool) bool {
	if len(a) != len(b) {
		return false
	}
	for k := range a {
		if !b[k] {
			return false
		}
	}
	return true
}

// normCIDR is the API's ip_network(v, strict=False): a bare address is a
// /32, host bits are dropped. Anything it cannot parse is left as it is.
func normCIDR(v string) string {
	v = strings.TrimSpace(v)
	if !strings.Contains(v, "/") {
		v += "/32"
	}
	_, n, err := net.ParseCIDR(v)
	if err != nil {
		return v
	}
	return n.String()
}

// normDomain is the API's clean_domain: lower case, no scheme, path,
// trailing dot or leading "*.".
func normDomain(d string) string {
	d = strings.ToLower(strings.TrimSpace(d))
	d = strings.TrimPrefix(strings.TrimPrefix(d, "https://"), "http://")
	d, _, _ = strings.Cut(d, "/")
	return strings.TrimPrefix(strings.TrimRight(d, "."), "*.")
}

func normLower(s string) string { return strings.ToLower(strings.TrimSpace(s)) }

// sameString keeps the prior value when it means the same as the API's.
func sameString(prior types.String, api string, norm func(string) string) types.String {
	if !prior.IsNull() && !prior.IsUnknown() && norm(prior.ValueString()) == norm(api) {
		return prior
	}
	return types.StringValue(api)
}

// insidePair is the API's inside_pair: (our address/len, the cloud's address).
// The cloud takes the first host of the /30 and we take the second.
func insidePair(cidr string) (string, string, bool) {
	p, err := netip.ParsePrefix(cidr)
	if err != nil || !p.Addr().Is4() || p.Bits() > 30 {
		return "", "", false
	}
	p = p.Masked()
	cloud := p.Addr().Next()
	ours := cloud.Next()
	return fmt.Sprintf("%s/%d", ours, p.Bits()), cloud.String(), true
}

// ---- plan modifiers -----------------------------------------------------------

// insideAddress plans our_inside or cloud_inside from the planned inside_cidr,
// so they are known whenever inside_cidr is.
type insideAddress struct{ ours bool }

func (m insideAddress) Description(context.Context) string {
	return "Worked out from inside_cidr."
}
func (m insideAddress) MarkdownDescription(ctx context.Context) string { return m.Description(ctx) }

func (m insideAddress) PlanModifyString(ctx context.Context, req planmodifier.StringRequest, resp *planmodifier.StringResponse) {
	var cidr types.String
	resp.Diagnostics.Append(req.Plan.GetAttribute(ctx, path.Root("inside_cidr"), &cidr)...)
	if cidr.IsUnknown() || cidr.IsNull() {
		resp.PlanValue = types.StringUnknown()
		return
	}
	ours, cloud, ok := insidePair(cidr.ValueString())
	if !ok {
		resp.PlanValue = types.StringUnknown()
		return
	}
	if m.ours {
		resp.PlanValue = types.StringValue(ours)
	} else {
		resp.PlanValue = types.StringValue(cloud)
	}
}

// secondaryInside plans secondary_inside_cidr when it is not configured: null
// without a second tunnel, the current value while the second tunnel stays,
// otherwise allocated by the controller (unknown).
type secondaryInside struct{}

func (secondaryInside) Description(context.Context) string {
	return "Kept while the second tunnel stays; allocated when one is added; removed with it."
}
func (m secondaryInside) MarkdownDescription(ctx context.Context) string { return m.Description(ctx) }

func (secondaryInside) PlanModifyString(ctx context.Context, req planmodifier.StringRequest, resp *planmodifier.StringResponse) {
	if !req.ConfigValue.IsNull() {
		return
	}
	var peer types.String
	resp.Diagnostics.Append(req.Plan.GetAttribute(ctx, path.Root("secondary_peer_address"), &peer)...)
	switch {
	case peer.IsUnknown():
		resp.PlanValue = types.StringUnknown()
	case peer.IsNull():
		resp.PlanValue = types.StringNull()
	case !req.StateValue.IsNull() && !req.StateValue.IsUnknown():
		resp.PlanValue = req.StateValue
	default:
		resp.PlanValue = types.StringUnknown()
	}
}

// defaultFrom plans an unset Int64 as the planned value of another attribute
// (b_vlan from a_vlan, to_port from port), as the API does.
type defaultFrom struct{ other string }

func (m defaultFrom) Description(context.Context) string {
	return "Defaults to " + m.other + "."
}
func (m defaultFrom) MarkdownDescription(ctx context.Context) string { return m.Description(ctx) }

func (m defaultFrom) PlanModifyInt64(ctx context.Context, req planmodifier.Int64Request, resp *planmodifier.Int64Response) {
	if !req.ConfigValue.IsNull() {
		return
	}
	var other types.Int64
	resp.Diagnostics.Append(req.Plan.GetAttribute(ctx, path.Root(m.other), &other)...)
	resp.PlanValue = other
}
