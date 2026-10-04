package provider

import (
	"context"
	"fmt"

	"github.com/hashicorp/terraform-plugin-framework-validators/int64validator"
	"github.com/hashicorp/terraform-plugin-framework-validators/stringvalidator"
	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/diag"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/booldefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/planmodifier"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/setdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringdefault"
	"github.com/hashicorp/terraform-plugin-framework/resource/schema/stringplanmodifier"
	"github.com/hashicorp/terraform-plugin-framework/schema/validator"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

// Attributes every resource has.
func customerIDAttr() schema.StringAttribute {
	return schema.StringAttribute{
		Required:      true,
		Description:   "The organisation (data.exaconnect_me.<name>.customer_id). Changing it replaces the resource.",
		PlanModifiers: []planmodifier.String{stringplanmodifier.RequiresReplace()},
	}
}

func idAttr() schema.StringAttribute {
	return schema.StringAttribute{
		Computed:      true,
		PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
	}
}

func emptyStringSet() types.Set { return types.SetValueMust(types.StringType, []attr.Value{}) }
func emptyIntSet() types.Set    { return types.SetValueMust(types.Int64Type, []attr.Value{}) }

func statusAttr() schema.StringAttribute {
	return schema.StringAttribute{
		Computed: true,
		Description: "provisioning, up, down or off. Comes from the agents' telemetry, so it is " +
			"refreshed on every read and known only after an apply that changes the circuit.",
	}
}

// wrongKind says what to do when the ID names a circuit of the other kind.
func wrongKind(diags *diag.Diagnostics, id int64, kind string) {
	other := map[string]string{"cloud": "exaconnect_cloud_circuit", "site": "exaconnect_site_circuit"}[kind]
	diags.AddError("Wrong kind of circuit",
		fmt.Sprintf("Circuit %d is a %s circuit; manage it with %s.", id, kind, other))
}

// ---- exaconnect_cloud_circuit ---------------------------------------------------

type cloudCircuitResource struct{ base }

func NewCloudCircuitResource() resource.Resource { return &cloudCircuitResource{} }

var (
	_ resource.ResourceWithImportState = (*cloudCircuitResource)(nil)
	_ resource.ResourceWithConfigure   = (*cloudCircuitResource)(nil)
)

type cloudCircuitModel struct {
	CustomerID           types.String `tfsdk:"customer_id"`
	ID                   types.String `tfsdk:"id"`
	Name                 types.String `tfsdk:"name"`
	ProviderName         types.String `tfsdk:"provider_name"`
	Region               types.String `tfsdk:"region"`
	SiteID               types.String `tfsdk:"site_id"`
	SitePrefixes         types.Set    `tfsdk:"site_prefixes"`
	PeerAddress          types.String `tfsdk:"peer_address"`
	SecondaryPeerAddress types.String `tfsdk:"secondary_peer_address"`
	PeerASN              types.Int64  `tfsdk:"peer_asn"`
	PSK                  types.String `tfsdk:"psk"`
	CloudPrefixes        types.Set    `tfsdk:"cloud_prefixes"`
	BandwidthMbps        types.Int64  `tfsdk:"bandwidth_mbps"`
	Enabled              types.Bool   `tfsdk:"enabled"`
	ClassName            types.String `tfsdk:"class_name"`
	InsideCIDR           types.String `tfsdk:"inside_cidr"`
	SecondaryInsideCIDR  types.String `tfsdk:"secondary_inside_cidr"`
	OurInside            types.String `tfsdk:"our_inside"`
	CloudInside          types.String `tfsdk:"cloud_inside"`
	Status               types.String `tfsdk:"status"`
}

func (r *cloudCircuitResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_cloud_circuit"
}

func bandwidthAttr() schema.Int64Attribute {
	return schema.Int64Attribute{
		Required:    true,
		Description: "1 to 1,000 Mbps. A change takes effect within 10 seconds and is billed from then.",
		Validators:  []validator.Int64{int64validator.Between(1, 1000)},
	}
}

func enabledAttr(what string) schema.BoolAttribute {
	return schema.BoolAttribute{
		Optional: true, Computed: true, Default: booldefault.StaticBool(true),
		Description: what + " Defaults to true.",
	}
}

func (r *cloudCircuitResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "An IPsec (IKEv2) circuit from the ExaCarib PoP to a cloud VPN gateway, with BGP over it. " +
			"Give secondary_peer_address for a resilient pair (two tunnels).",
		Attributes: map[string]schema.Attribute{
			"customer_id": customerIDAttr(),
			"id":          idAttr(),
			"name": schema.StringAttribute{
				Required: true, Validators: []validator.String{stringvalidator.LengthBetween(1, 80)},
			},
			"provider_name": schema.StringAttribute{
				Required:    true,
				Description: "aws, azure, gcp, oracle or other. (\"provider\" is reserved in Terraform.)",
				Validators:  []validator.String{stringvalidator.OneOf("aws", "azure", "gcp", "oracle", "other")},
			},
			"region": schema.StringAttribute{
				Optional: true, Computed: true, Default: stringdefault.StaticString(""),
				Validators: []validator.String{stringvalidator.LengthAtMost(40)},
			},
			"site_id": schema.StringAttribute{
				Optional:    true,
				Description: "The site this circuit is for (optional; data.exaconnect_sites.<name>.sites[\"site-a\"].id).",
			},
			"site_prefixes": schema.SetAttribute{
				Optional: true, Computed: true, ElementType: types.StringType, Default: setdefault.StaticValue(emptyStringSet()),
				Description: "Site subnets to announce to the cloud (inside your sites' LANs). Empty: all of them.",
			},
			"peer_address": schema.StringAttribute{
				Required: true, Description: "The cloud gateway's public IPv4 address (tunnel 1).",
			},
			"secondary_peer_address": schema.StringAttribute{
				Optional:    true,
				Description: "The cloud's second gateway address (tunnel 2). Makes the circuit a resilient pair; remove it to go back to one tunnel.",
			},
			"peer_asn": schema.Int64Attribute{
				Required: true, Description: "The cloud gateway's BGP ASN (AWS 64512, Azure 65515).",
				Validators: []validator.Int64{int64validator.Between(1, 4294967294)},
			},
			"psk": schema.StringAttribute{
				Required: true, Sensitive: true,
				Description: "The IKEv2 pre-shared key from the cloud console. Write-only: the API never returns it, " +
					"so a change here is always sent and an imported circuit sends it once on the next apply.",
			},
			"cloud_prefixes": schema.SetAttribute{
				Optional: true, Computed: true, ElementType: types.StringType, Default: setdefault.StaticValue(emptyStringSet()),
				Description: "Accept only these cloud subnets (and longer). Empty: accept any.",
			},
			"bandwidth_mbps": bandwidthAttr(),
			"enabled":        enabledAttr("Off stops the circuit without deleting it."),
			"class_name": schema.StringAttribute{
				Optional: true, Description: "Put the circuit's traffic in this application class.",
			},
			"inside_cidr": schema.StringAttribute{
				Optional: true, Computed: true,
				Description:   "Tunnel 1's inside /30, as the cloud console shows. Allocated from 169.254.100.0/22 when not given.",
				PlanModifiers: []planmodifier.String{stringplanmodifier.UseStateForUnknown()},
			},
			"secondary_inside_cidr": schema.StringAttribute{
				Optional: true, Computed: true,
				Description:   "Tunnel 2's inside /30. Allocated when not given; null without a second tunnel.",
				PlanModifiers: []planmodifier.String{secondaryInside{}},
				Validators:    []validator.String{stringvalidator.AlsoRequires(path.MatchRoot("secondary_peer_address"))},
			},
			"our_inside": schema.StringAttribute{
				Computed: true, Description: "Our tunnel 1 inside address with its length.",
				PlanModifiers: []planmodifier.String{insideAddress{ours: true}},
			},
			"cloud_inside": schema.StringAttribute{
				Computed: true, Description: "The cloud's tunnel 1 inside address (its BGP neighbour).",
				PlanModifiers: []planmodifier.String{insideAddress{ours: false}},
			},
			"status": statusAttr(),
		},
	}
}

func (r *cloudCircuitResource) body(ctx context.Context, m *cloudCircuitModel, prior *cloudCircuitModel, diags *diag.Diagnostics) client.CircuitIn {
	provider := m.ProviderName.ValueString()
	in := client.CircuitIn{
		Name:                 m.Name.ValueString(),
		Kind:                 "cloud",
		BandwidthMbps:        m.BandwidthMbps.ValueInt64(),
		Enabled:              boolPtr(m.Enabled),
		ASiteID:              strPtr(m.SiteID),
		APrefixes:            setStrings(ctx, m.SitePrefixes, diags),
		Provider:             &provider,
		Region:               strPtr(m.Region),
		PeerAddress:          strPtr(m.PeerAddress),
		PeerASN:              int64Ptr(m.PeerASN),
		InsideCIDR:           strPtr(m.InsideCIDR),
		SecondaryPeerAddress: strPtr(m.SecondaryPeerAddress),
		SecondaryInsideCIDR:  strPtr(m.SecondaryInsideCIDR),
		CloudPrefixes:        setStrings(ctx, m.CloudPrefixes, diags),
		ClassName:            strPtr(m.ClassName),
	}
	if prior == nil || !m.PSK.Equal(prior.PSK) {
		in.PSK = strPtr(m.PSK)
	}
	// On a PATCH the API reads null as "leave as it is"; "" clears these.
	// (A POST must send null: it stores "" as given.)
	if prior != nil {
		empty := ""
		if in.ASiteID == nil {
			in.ASiteID = &empty
		}
		if in.ClassName == nil {
			in.ClassName = &empty
		}
	}
	return in
}

// fromAPI copies the API's view into m, keeping m's spelling of values that mean the same.
func (r *cloudCircuitResource) fromAPI(ctx context.Context, c *client.Circuit, m *cloudCircuitModel, diags *diag.Diagnostics) {
	m.ID = idString(c.ID)
	m.Name = types.StringValue(c.Name)
	m.ProviderName = optString(c.Provider)
	m.Region = types.StringValue(c.Region)
	if c.ASiteID == nil {
		m.SiteID = types.StringNull()
	} else {
		m.SiteID = sameString(m.SiteID, *c.ASiteID, normLower)
	}
	m.SitePrefixes = stringSet(ctx, m.SitePrefixes, c.APrefixes, normCIDR, diags)
	m.PeerAddress = optString(c.PeerAddress)
	m.SecondaryPeerAddress = optString(c.SecondaryPeerAddress)
	m.PeerASN = optInt64(c.PeerASN)
	m.CloudPrefixes = stringSet(ctx, m.CloudPrefixes, c.CloudPrefixes, normCIDR, diags)
	m.BandwidthMbps = types.Int64Value(c.BandwidthMbps)
	m.Enabled = types.BoolValue(c.Enabled)
	m.ClassName = optString(c.ClassName)
	m.InsideCIDR = optString(c.InsideCIDR)
	m.SecondaryInsideCIDR = optString(c.SecondaryInsideCIDR)
	m.OurInside = optString(c.OurInside)
	m.CloudInside = optString(c.CloudInside)
	m.Status = types.StringValue(c.Status)
	// psk is never returned: keep what Terraform has.
}

func (r *cloudCircuitResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m cloudCircuitModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	in := r.body(ctx, &m, nil, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := r.client.CreateCircuit(ctx, m.CustomerID.ValueString(), in)
	if err != nil {
		apiError(&resp.Diagnostics, "create the cloud circuit", err)
		return
	}
	r.fromAPI(ctx, c, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *cloudCircuitResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m cloudCircuitModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := r.client.GetCircuit(ctx, m.CustomerID.ValueString(), id)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		apiError(&resp.Diagnostics, "read the cloud circuit", err)
		return
	}
	if c.Kind != "cloud" {
		wrongKind(&resp.Diagnostics, c.ID, c.Kind)
		return
	}
	r.fromAPI(ctx, c, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *cloudCircuitResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m, prior cloudCircuitModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &prior)...)
	id := parseID(prior.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	in := r.body(ctx, &m, &prior, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := r.client.UpdateCircuit(ctx, m.CustomerID.ValueString(), id, in)
	if err != nil {
		apiError(&resp.Diagnostics, "update the cloud circuit", err)
		return
	}
	r.fromAPI(ctx, c, &m, &resp.Diagnostics)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *cloudCircuitResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m cloudCircuitModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.DeleteCircuit(ctx, m.CustomerID.ValueString(), id); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "delete the cloud circuit", err)
	}
}

func (r *cloudCircuitResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importNumeric(ctx, req, resp)
}

// ---- exaconnect_site_circuit ------------------------------------------------------

type siteCircuitResource struct{ base }

func NewSiteCircuitResource() resource.Resource { return &siteCircuitResource{} }

var _ resource.ResourceWithImportState = (*siteCircuitResource)(nil)

type siteCircuitModel struct {
	CustomerID    types.String `tfsdk:"customer_id"`
	ID            types.String `tfsdk:"id"`
	Name          types.String `tfsdk:"name"`
	ASiteID       types.String `tfsdk:"a_site_id"`
	BSiteID       types.String `tfsdk:"b_site_id"`
	AVLAN         types.Int64  `tfsdk:"a_vlan"`
	BVLAN         types.Int64  `tfsdk:"b_vlan"`
	BandwidthMbps types.Int64  `tfsdk:"bandwidth_mbps"`
	Enabled       types.Bool   `tfsdk:"enabled"`
	Status        types.String `tfsdk:"status"`
}

func (r *siteCircuitResource) Metadata(_ context.Context, req resource.MetadataRequest, resp *resource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_site_circuit"
}

func (r *siteCircuitResource) Schema(_ context.Context, _ resource.SchemaRequest, resp *resource.SchemaResponse) {
	vlan := []validator.Int64{int64validator.Between(1, 4094)}
	resp.Schema = schema.Schema{
		Description: "A layer 2 circuit carrying a VLAN between two of the organisation's sites (VXLAN over the overlay).",
		Attributes: map[string]schema.Attribute{
			"customer_id": customerIDAttr(),
			"id":          idAttr(),
			"name": schema.StringAttribute{
				Required: true, Validators: []validator.String{stringvalidator.LengthBetween(1, 80)},
			},
			"a_site_id": schema.StringAttribute{Required: true, Description: "One end."},
			"b_site_id": schema.StringAttribute{Required: true, Description: "The other end (a different site)."},
			"a_vlan":    schema.Int64Attribute{Required: true, Description: "The VLAN at the A end.", Validators: vlan},
			"b_vlan": schema.Int64Attribute{
				Optional: true, Computed: true, Description: "The VLAN at the B end. Defaults to a_vlan.",
				Validators: vlan, PlanModifiers: []planmodifier.Int64{defaultFrom{other: "a_vlan"}},
			},
			"bandwidth_mbps": bandwidthAttr(),
			"enabled":        enabledAttr("Off stops the circuit without deleting it."),
			"status":         statusAttr(),
		},
	}
}

func (r *siteCircuitResource) body(m *siteCircuitModel) client.CircuitIn {
	return client.CircuitIn{
		Name:          m.Name.ValueString(),
		Kind:          "site",
		BandwidthMbps: m.BandwidthMbps.ValueInt64(),
		Enabled:       boolPtr(m.Enabled),
		ASiteID:       strPtr(m.ASiteID),
		AVLAN:         int64Ptr(m.AVLAN),
		BSiteID:       strPtr(m.BSiteID),
		BVLAN:         int64Ptr(m.BVLAN),
	}
}

func (r *siteCircuitResource) fromAPI(c *client.Circuit, m *siteCircuitModel) {
	m.ID = idString(c.ID)
	m.Name = types.StringValue(c.Name)
	if c.ASiteID != nil {
		m.ASiteID = sameString(m.ASiteID, *c.ASiteID, normLower)
	}
	if c.BSiteID != nil {
		m.BSiteID = sameString(m.BSiteID, *c.BSiteID, normLower)
	}
	m.AVLAN = optInt64(c.AVLAN)
	m.BVLAN = optInt64(c.BVLAN)
	m.BandwidthMbps = types.Int64Value(c.BandwidthMbps)
	m.Enabled = types.BoolValue(c.Enabled)
	m.Status = types.StringValue(c.Status)
}

func (r *siteCircuitResource) Create(ctx context.Context, req resource.CreateRequest, resp *resource.CreateResponse) {
	var m siteCircuitModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := r.client.CreateCircuit(ctx, m.CustomerID.ValueString(), r.body(&m))
	if err != nil {
		apiError(&resp.Diagnostics, "create the site circuit", err)
		return
	}
	r.fromAPI(c, &m)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *siteCircuitResource) Read(ctx context.Context, req resource.ReadRequest, resp *resource.ReadResponse) {
	var m siteCircuitModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := r.client.GetCircuit(ctx, m.CustomerID.ValueString(), id)
	if client.IsNotFound(err) {
		resp.State.RemoveResource(ctx)
		return
	}
	if err != nil {
		apiError(&resp.Diagnostics, "read the site circuit", err)
		return
	}
	if c.Kind != "site" {
		wrongKind(&resp.Diagnostics, c.ID, c.Kind)
		return
	}
	r.fromAPI(c, &m)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *siteCircuitResource) Update(ctx context.Context, req resource.UpdateRequest, resp *resource.UpdateResponse) {
	var m, prior siteCircuitModel
	resp.Diagnostics.Append(req.Plan.Get(ctx, &m)...)
	resp.Diagnostics.Append(req.State.Get(ctx, &prior)...)
	id := parseID(prior.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := r.client.UpdateCircuit(ctx, m.CustomerID.ValueString(), id, r.body(&m))
	if err != nil {
		apiError(&resp.Diagnostics, "update the site circuit", err)
		return
	}
	r.fromAPI(c, &m)
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

func (r *siteCircuitResource) Delete(ctx context.Context, req resource.DeleteRequest, resp *resource.DeleteResponse) {
	var m siteCircuitModel
	resp.Diagnostics.Append(req.State.Get(ctx, &m)...)
	id := parseID(m.ID, &resp.Diagnostics)
	if resp.Diagnostics.HasError() {
		return
	}
	if err := r.client.DeleteCircuit(ctx, m.CustomerID.ValueString(), id); err != nil && !client.IsNotFound(err) {
		apiError(&resp.Diagnostics, "delete the site circuit", err)
	}
}

func (r *siteCircuitResource) ImportState(ctx context.Context, req resource.ImportStateRequest, resp *resource.ImportStateResponse) {
	importNumeric(ctx, req, resp)
}
