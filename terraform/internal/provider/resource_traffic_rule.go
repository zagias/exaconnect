package provider

import (
	"context"

	"github.com/hashicorp/terraform-plugin-framework-validators/int64validator"
	"github.com/hashicorp/terraform-plugin-framework-validators/setvalidator"
	"github.com/hashicorp/terraform-plugin-framework-validators/stringvalidator"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/int64default"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

type trafficRuleResource struct{ base }

func NewTrafficRuleResource() resource.Resource { return &trafficRuleResource{} }

var _ resource.ResourceWithImportState = (*trafficRuleResource)(nil)

type trafficRuleModel struct {
	CustomerID types.String `tfsdk:"customer_id"`
	ID         types.String `tfsdk:"id"`
	Name       types.String `tfsdk:"name"`
	ClassName  types.String `tfsdk:"class_name"`
	SiteIDs    types.Set    `tfsdk:"site_ids"`
	Apps       types.Set    `tfsdk:"apps"`
	Ports      types.String `tfsdk:"ports"`
	DstSubnets types.Set    `tfsdk:"dst_subnets"`
	SrcSubnets types.Set    `tfsdk:"src_subnets"`
	VLANs      types.Set    `tfsdk:"vlans"`
	Domains    types.Set    `tfsdk:"domains"`
	DSCP       types.Set    `tfsdk:"dscp"`
	Enabled    types.Bool   `tfsdk:"enabled"`
	Ordinal    types.Int64  `tfsdk:"ordinal"`
}

func (r *trafficRuleResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_traffic_rule"
}

func stringSetAttr(desc string, max int) schema.SetAttribute {
	return schema.SetAttribute{
		Optional: true, Computed: true, ElementType: types.StringType,
		Default:     setdefault.StaticValue(emptyStringSet()),
		Description: desc,
		Validators:  []validator.Set{setvalidator.SizeAtMost(max)},
	}
}

func intSetAttr(desc string, max int, lo, hi int64) schema.SetAttribute {
	return schema.SetAttribute{
		Optional: true, Computed: true, ElementType: types.Int64Type,
		Default:     setdefault.StaticValue(emptyIntSet()),
		Description: desc,
		Validators: []validator.Set{
			setvalidator.SizeAtMost(max),
			setvalidator.ValueInt64sAre(int64validator.Between(lo, hi)),
		},
	}
}

func (r *trafficRuleResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Put matching traffic in an application class (POST /customers/{cid}/rules). " +
			"A rule needs at least one of apps, ports, dst_subnets, domains, src_subnets, vlans or dscp.",
		Attributes: map[string]schema.Attribute{
			"customer_id": customerIDAttr(),
			"id":          idAttr(),
			"name": schema.StringAttribute{
				Required: true, Validators: []validator.String{stringvalidator.LengthBetween(1, 80)},
			},
			"class_name": schema.StringAttribute{
				Required: true, Description: "The class, like voice, business or bulk.",
				Validators: []validator.String{stringvalidator.LengthAtMost(20)},
			},
			"site_ids":    stringSetAttr("Only at these sites. Empty: every site.", 100),
			"apps":        stringSetAttr("Applications from GET /applications/catalogue, like \"teams\".", 20),
			"ports":       schema.StringAttribute{Optional: true, Computed: true, Default: stringdefault.StaticString(""), Description: "Like \"udp:5060,10000-20000 tcp:443\"."},
			"dst_subnets": stringSetAttr("Destination subnets.", 50),
			"src_subnets": stringSetAttr("Source subnets.", 50),
			"vlans":       intSetAttr("VLAN IDs.", 50, 1, 4094),
			"domains":     stringSetAttr("Website names, like teams.microsoft.com.", 50),
			"dscp":        intSetAttr("DSCP values.", 64, 0, 63),
			"enabled":     enabledAttr(""),
			"ordinal": schema.Int64Attribute{
				Optional: true, Computed: true, Default: int64default.StaticInt64(100),
				Description: "Lower is checked first. Defaults to 100.",
				Validators:  []validator.Int64{int64validator.Between(1, 1000)},
			},
		},
	}
}

func (r *trafficRuleResource) body(ctx context.Context, m *trafficRuleModel, diags *diag.Diagnostics) client.TrafficRuleIn {
	return client.TrafficRuleIn{
		Name:       m.Name.ValueString(),
		ClassName:  m.ClassName.ValueString(),
		SiteIDs:    setStrings(ctx, m.SiteIDs, diags),
		Apps:       setStrings(ctx, m.Apps, diags),
		Ports:      m.Ports.ValueString(),
		DstSubnets: setStrings(ctx, m.DstSubnets, diags),
		SrcSubnets: setStrings(ctx, m.SrcSubnets, diags),
		VLANs:      setInts(ctx, m.VLANs, diags),
		Domains:    setStrings(ctx, m.Domains, diags),
		DSCP:       setInts(ctx, m.DSCP, diags),
		Enabled:    m.Enabled.ValueBool(),
		Ordinal:    m.Ordinal.ValueInt64(),
	}
}

func (r *trafficRuleResource) fromAPI(ctx context.Context, t *client.TrafficRule, m *trafficRuleModel, diags *diag.Diagnostics) {
	m.ID = idString(t.ID)
	m.Name = types.StringValue(t.Name)
	m.ClassName = types.StringValue(t.ClassName)
	m.SiteIDs = stringSet(ctx, m.SiteIDs, t.SiteIDs, normLower, diags)
	m.Apps = stringSet(ctx, m.Apps, t.Apps, nil, diags)
	m.Ports = types.StringValue(t.Ports)
	m.DstSubnets = stringSet(ctx, m.DstSubnets, t.DstSubnets, normCIDR, diags)
	m.SrcSubnets = stringSet(ctx, m.SrcSubnets, t.SrcSubnets, normCIDR, diags)
	m.VLANs = intSet(t.VLANs, diags)
	m.Domains = stringSet(ctx, m.Domains, t.Domains, normDomain, diags)
	m.DSCP = intSet(t.DSCP, diags)
	m.Enabled = types.BoolValue(t.Enabled)
	m.Ordinal = types.Int64Value(t.Ordinal)
}

func (r *trafficRuleResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m trafficRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	in := r.body(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	t, err := r.client.CreateTrafficRule(ctx, m.CustomerID.ValueString(), in)
	if err != nil {
		apiError(&resp.Diagnostics, "create the traffic rule", err)
		return
	}
	r.fromAPI(ctx, t, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *trafficRuleResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m trafficRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	t, err := r.client.GetTrafficRule(ctx, m.CustomerID.ValueString(), id)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		apiError(&resp.Diagnostics, "read the traffic rule", err)
		return
	}
	r.fromAPI(ctx, t, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *trafficRuleResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m, prior trafficRuleModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &prior)...)
	id := parseID(prior.ID, &resp.Diagnostics)
	in := r.body(ctx, &m, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	t, err := r.client.UpdateTrafficRule(ctx, m.CustomerID.ValueString(), id, in)
	if err != nil {
		apiError(&resp.Diagnostics, "update the traffic rule", err)
		return
	}
	r.fromAPI(ctx, t, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *trafficRuleResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m trafficRuleModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.DeleteTrafficRule(ctx, m.CustomerID.ValueString(), id); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "delete the traffic rule", err)
	}
}

func (r *trafficRuleResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importNumeric(ctx, req, resp)
}
