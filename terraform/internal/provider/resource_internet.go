package provider

import (
	"context"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework-validators/int64validator"
	"github.com/hashicorp/terraform-plugin-framework-validators/stringvalidator"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

func noSpaces(s string) string { return strings.ReplaceAll(s, " ", "") }

func descriptionAttr() schema.StringAttribute {
	return schema.StringAttribute{
		Optional: true, Computed: true, Default: stringdefault.StaticString(""),
		Validators: []validator.String{stringvalidator.LengthAtMost(120)},
	}
}

func cidrSetAttr(desc string) schema.SetAttribute {
	return schema.SetAttribute{
		Optional: true, Computed: true, ElementType: types.StringType,
		Default:     setdefault.StaticValue(emptyStringSet()),
		Description: desc,
	}
}

// ---- exaconnect_firewall_rule -------------------------------------------------------

type firewallRuleResource struct{ base }

func NewFirewallRuleResource() resource.Resource { return &firewallRuleResource{} }

var _ resource.ResourceWithImportState = (*firewallRuleResource)(nil)

type firewallRuleModel struct {
	CustomerID  types.String `tfsdk:"customer_id"`
	ID          types.String `tfsdk:"id"`
	Action      types.String `tfsdk:"action"`
	SiteID      types.String `tfsdk:"site_id"`
	Src         types.Set    `tfsdk:"src"`
	Dst         types.Set    `tfsdk:"dst"`
	Protocol    types.String `tfsdk:"protocol"`
	Ports       types.String `tfsdk:"ports"`
	Description types.String `tfsdk:"description"`
	Enabled     types.Bool   `tfsdk:"enabled"`
	Position    types.Int64  `tfsdk:"position"`
}

func (r *firewallRuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_firewall_rule"
}

func (r *firewallRuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "A firewall rule for internet traffic, applied at the PoP and at local-breakout sites. " +
			"Rules apply in order, first match wins, then allow. A new rule goes to the end of the list.",
		Attributes: map[string]schema.Attribute{
			"customer_id": customerIDAttr(),
			"id":          idAttr(),
			"action": schema.StringAttribute{
				Required: true, Validators: []validator.String{stringvalidator.OneOf("allow", "deny")},
			},
			"site_id": schema.StringAttribute{Optional: true, Description: "Only this site's traffic. Unset: every site."},
			"src":     cidrSetAttr("Source subnets. Empty: any."),
			"dst":     cidrSetAttr("Destination subnets. Empty: any."),
			"protocol": schema.StringAttribute{
				Optional: true, Computed: true, Default: stringdefault.StaticString("any"),
				Validators: []validator.String{stringvalidator.OneOf("any", "tcp", "udp", "icmp")},
			},
			"ports": schema.StringAttribute{
				Optional: true, Computed: true, Default: stringdefault.StaticString(""),
				Description: "Ports or ranges, like \"443,8000-8100\" (tcp or udp only).",
				Validators:  []validator.String{stringvalidator.LengthAtMost(200)},
			},
			"description": descriptionAttr(),
			"enabled":     enabledAttr(""),
			"position": schema.Int64Attribute{
				Computed:    true,
				Description: "Where the rule is in the organisation's list (1 is checked first). Changes when other rules are added or removed.",
			},
		},
	}
}

func (r *firewallRuleResource) body(ctx context.Context, m *firewallRuleModel, diags *diag.Diagnostics) client.FirewallRuleIn {
	return client.FirewallRuleIn{
		SiteID:      strPtr(m.SiteID),
		Action:      m.Action.ValueString(),
		Src:         setStrings(ctx, m.Src, diags),
		Dst:         setStrings(ctx, m.Dst, diags),
		Protocol:    m.Protocol.ValueString(),
		Ports:       m.Ports.ValueString(),
		Description: m.Description.ValueString(),
		Enabled:     m.Enabled.ValueBool(),
	}
}

func (r *firewallRuleResource) fromAPI(ctx context.Context, f *client.FirewallRule, m *firewallRuleModel, diags *diag.Diagnostics) {
	m.ID = idString(f.ID)
	m.Action = types.StringValue(f.Action)
	if f.SiteID == nil {
		m.SiteID = types.StringNull()
	} else {
		m.SiteID = sameString(m.SiteID, *f.SiteID, normLower)
	}
	m.Src = stringSet(ctx, m.Src, f.Src, normCIDR, diags)
	m.Dst = stringSet(ctx, m.Dst, f.Dst, normCIDR, diags)
	m.Protocol = types.StringValue(f.Protocol)
	m.Ports = sameString(m.Ports, f.Ports, noSpaces)
	m.Description = sameString(m.Description, f.Description, strings.TrimSpace)
	m.Enabled = types.BoolValue(f.Enabled)
	m.Position = types.Int64Value(f.Position)
}

func (r *firewallRuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m firewallRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	in := r.body(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	f, err := r.client.CreateFirewallRule(ctx, m.CustomerID.ValueString(), in)
	if err != nil {
		apiError(&resp.Diagnostics, "create the firewall rule", err)
		return
	}
	r.fromAPI(ctx, f, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *firewallRuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m firewallRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	f, err := r.client.GetFirewallRule(ctx, m.CustomerID.ValueString(), id)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		apiError(&resp.Diagnostics, "read the firewall rule", err)
		return
	}
	r.fromAPI(ctx, f, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *firewallRuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m, prior firewallRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &prior)...)
	id := parseID(prior.ID, &resp.Diagnostics)
	in := r.body(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	f, err := r.client.UpdateFirewallRule(ctx, m.CustomerID.ValueString(), id, in)
	if err != nil {
		apiError(&resp.Diagnostics, "update the firewall rule", err)
		return
	}
	r.fromAPI(ctx, f, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *firewallRuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m firewallRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.DeleteFirewallRule(ctx, m.CustomerID.ValueString(), id); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "delete the firewall rule", err)
	}
}

func (r *firewallRuleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importNumeric(ctx, req, resp)
}

// ---- exaconnect_port_forward ---------------------------------------------------------

type portForwardResource struct{ base }

func NewPortForwardResource() resource.Resource { return &portForwardResource{} }

var _ resource.ResourceWithImportState = (*portForwardResource)(nil)

type portForwardModel struct {
	CustomerID  types.String `tfsdk:"customer_id"`
	ID          types.String `tfsdk:"id"`
	Protocol    types.String `tfsdk:"protocol"`
	Port        types.Int64  `tfsdk:"port"`
	ToSiteID    types.String `tfsdk:"to_site_id"`
	ToAddress   types.String `tfsdk:"to_address"`
	ToPort      types.Int64  `tfsdk:"to_port"`
	AllowFrom   types.Set    `tfsdk:"allow_from"`
	Description types.String `tfsdk:"description"`
	Enabled     types.Bool   `tfsdk:"enabled"`
	Active      types.Bool   `tfsdk:"active"`
}

func (r *portForwardResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_port_forward"
}

func (r *portForwardResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	port := []validator.Int64{int64validator.Between(1, 65535)}
	resp.Schema = schema.Schema{
		Description: "Forward a public port on the PoP's shared address to an address at a site. " +
			"Works while that site's internet goes through the PoP (mode \"pop\").",
		Attributes: map[string]schema.Attribute{
			"customer_id": customerIDAttr(),
			"id":          idAttr(),
			"protocol": schema.StringAttribute{
				Required: true, Validators: []validator.String{stringvalidator.OneOf("tcp", "udp")},
			},
			"port": schema.Int64Attribute{
				Required: true, Validators: port,
				Description: "The public port. Unique on the PoP across every organisation, since they share its address.",
			},
			"to_site_id": schema.StringAttribute{Required: true},
			"to_address": schema.StringAttribute{Required: true, Description: "An IPv4 address inside the site's LAN."},
			"to_port": schema.Int64Attribute{
				Optional: true, Computed: true, Validators: port, Description: "Defaults to port.",
				PlanModifiers: []planmodifier.Int64{defaultFrom{other: "port"}},
			},
			"allow_from":  cidrSetAttr("Only from these subnets. Empty: from anywhere."),
			"description": descriptionAttr(),
			"enabled":     enabledAttr(""),
			"active": schema.BoolAttribute{
				Computed:    true,
				Description: "Enabled and its site goes through the PoP, so the forward is in place.",
			},
		},
	}
}

func (r *portForwardResource) body(ctx context.Context, m *portForwardModel, diags *diag.Diagnostics) client.PortForwardIn {
	return client.PortForwardIn{
		Description: m.Description.ValueString(),
		Protocol:    m.Protocol.ValueString(),
		Port:        m.Port.ValueInt64(),
		ToSiteID:    m.ToSiteID.ValueString(),
		ToAddress:   m.ToAddress.ValueString(),
		ToPort:      int64Ptr(m.ToPort),
		AllowFrom:   setStrings(ctx, m.AllowFrom, diags),
		Enabled:     m.Enabled.ValueBool(),
	}
}

func (r *portForwardResource) fromAPI(ctx context.Context, f *client.PortForward, m *portForwardModel, diags *diag.Diagnostics) {
	m.ID = idString(f.ID)
	m.Protocol = types.StringValue(f.Protocol)
	m.Port = types.Int64Value(f.Port)
	m.ToSiteID = sameString(m.ToSiteID, f.ToSiteID, normLower)
	m.ToAddress = sameString(m.ToAddress, f.ToAddress, strings.TrimSpace)
	m.ToPort = types.Int64Value(f.ToPort)
	m.AllowFrom = stringSet(ctx, m.AllowFrom, f.AllowFrom, normCIDR, diags)
	m.Description = sameString(m.Description, f.Description, strings.TrimSpace)
	m.Enabled = types.BoolValue(f.Enabled)
	m.Active = types.BoolValue(f.Active)
}

func (r *portForwardResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m portForwardModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	in := r.body(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	f, err := r.client.CreatePortForward(ctx, m.CustomerID.ValueString(), in)
	if err != nil {
		apiError(&resp.Diagnostics, "create the port forward", err)
		return
	}
	r.fromAPI(ctx, f, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *portForwardResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m portForwardModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	f, err := r.client.GetPortForward(ctx, m.CustomerID.ValueString(), id)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		apiError(&resp.Diagnostics, "read the port forward", err)
		return
	}
	r.fromAPI(ctx, f, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *portForwardResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m, prior portForwardModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &prior)...)
	id := parseID(prior.ID, &resp.Diagnostics)
	in := r.body(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	f, err := r.client.UpdatePortForward(ctx, m.CustomerID.ValueString(), id, in)
	if err != nil {
		apiError(&resp.Diagnostics, "update the port forward", err)
		return
	}
	r.fromAPI(ctx, f, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *portForwardResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m portForwardModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.DeletePortForward(ctx, m.CustomerID.ValueString(), id); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "delete the port forward", err)
	}
}

func (r *portForwardResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importNumeric(ctx, req, resp)
}

// ---- exaconnect_site_internet --------------------------------------------------------

type siteInternetResource struct{ base }

func NewSiteInternetResource() resource.Resource { return &siteInternetResource{} }

var _ resource.ResourceWithImportState = (*siteInternetResource)(nil)

type siteInternetModel struct {
	CustomerID types.String `tfsdk:"customer_id"`
	ID         types.String `tfsdk:"id"`
	SiteID     types.String `tfsdk:"site_id"`
	Mode       types.String `tfsdk:"mode"`
}

func (r *siteInternetResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_site_internet"
}

func (r *siteInternetResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "How a site's internet traffic leaves: through the PoP (\"pop\", the default), straight out of " +
			"its own carrier links (\"local\") or not at all (\"off\"). Destroying it sets the site back to \"pop\".",
		Attributes: map[string]schema.Attribute{
			"customer_id": customerIDAttr(),
			"id":          idAttr(),
			"site_id": schema.StringAttribute{
				Required: true, Description: "A site (not the PoP). Changing it replaces the resource.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
			},
			"mode": schema.StringAttribute{
				Required: true, Validators: []validator.String{stringvalidator.OneOf("pop", "local", "off")},
			},
		},
	}
}

func (r *siteInternetResource) set(ctx context.Context, m *siteInternetModel, diags *diag.Diagnostics) {
	s, err := r.client.SetSiteInternet(ctx, m.CustomerID.ValueString(), m.SiteID.ValueString(), m.Mode.ValueString())
	if err != nil {
		apiError(diags, "set the site's internet mode", err)
		return
	}
	m.ID = m.SiteID
	m.Mode = types.StringValue(s.Mode)
}

func (r *siteInternetResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m siteInternetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.set(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *siteInternetResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m siteInternetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	siteID := m.SiteID.ValueString()
	if siteID == "" {
		siteID = m.ID.ValueString() // after import
	}
	s, err := r.client.GetSiteInternet(ctx, m.CustomerID.ValueString(), siteID)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		apiError(&resp.Diagnostics, "read the site's internet mode", err)
		return
	}
	m.SiteID = sameString(m.SiteID, s.ID, normLower)
	m.ID = m.SiteID
	m.Mode = types.StringValue(s.Mode)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *siteInternetResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m siteInternetModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	r.set(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *siteInternetResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m siteInternetModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	cid, sid := m.CustomerID.ValueString(), m.SiteID.ValueString()
	if _, err := r.client.GetSiteInternet(ctx, cid, sid); client.IsNotFound(err) {
		return // the site is gone
	}
	if _, err := r.client.SetSiteInternet(ctx, cid, sid, "pop"); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "set the site's internet back to the PoP", err)
	}
}

func (r *siteInternetResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	cid, sid, err := splitImportID(req.ID, "site_id")
	if err != nil {
		resp.Diagnostics.AddError("Bad import ID", err.Error())
		return
	}
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("customer_id"), cid)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("site_id"), sid)...)
	resp.Diagnostics.Append(resp.State.SetAttribute(ctx, path.Root("id"), sid)...)
}
