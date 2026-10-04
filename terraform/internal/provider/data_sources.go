package provider

import (
	"context"
	"strings"

	"github.com/hashicorp/terraform-plugin-framework/attr"
	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/datasource/schema"
	"github.com/hashicorp/terraform-plugin-framework/types"
)

// ---- exaconnect_me ------------------------------------------------------------

type meDataSource struct{ dsBase }

func NewMeDataSource() datasource.DataSource { return &meDataSource{} }

type meModel struct {
	ID         types.String `tfsdk:"id"`
	Email      types.String `tfsdk:"email"`
	Role       types.String `tfsdk:"role"`
	CustomerID types.String `tfsdk:"customer_id"`
}

func (d *meDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_me"
}

func (d *meDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "The person the API key belongs to (GET /auth/me).",
		Attributes: map[string]schema.Attribute{
			"id":          schema.StringAttribute{Computed: true, Description: "The email address."},
			"email":       schema.StringAttribute{Computed: true},
			"role":        schema.StringAttribute{Computed: true, Description: "admin, customer or carrier."},
			"customer_id": schema.StringAttribute{Computed: true, Description: "The person's organisation; null for admins and carriers."},
		},
	}
}

func (d *meDataSource) Read(ctx context.Context, _ datasource.ReadRequest, resp *datasource.ReadResponse) {
	me, err := d.client.Me(ctx)
	if err != nil {
		apiError(&resp.Diagnostics, "read who the API key belongs to", err)
		return
	}
	m := meModel{
		ID:         types.StringValue(me.Email),
		Email:      types.StringValue(me.Email),
		Role:       types.StringValue(me.Role),
		CustomerID: optString(me.CustomerID),
	}
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}

// ---- exaconnect_sites ---------------------------------------------------------

type sitesDataSource struct{ dsBase }

func NewSitesDataSource() datasource.DataSource { return &sitesDataSource{} }

type sitesModel struct {
	ID         types.String `tfsdk:"id"`
	CustomerID types.String `tfsdk:"customer_id"`
	Sites      types.Map    `tfsdk:"sites"`
}

var siteAttrTypes = map[string]attr.Type{
	"id":           types.StringType,
	"name":         types.StringType,
	"kind":         types.StringType,
	"location":     types.StringType,
	"lan_prefixes": types.ListType{ElemType: types.StringType},
}

func (d *sitesDataSource) Metadata(_ context.Context, req datasource.MetadataRequest, resp *datasource.MetadataResponse) {
	resp.TypeName = req.ProviderTypeName + "_sites"
}

func (d *sitesDataSource) Schema(_ context.Context, _ datasource.SchemaRequest, resp *datasource.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "An organisation's sites and PoPs (GET /sites), keyed by site name.",
		Attributes: map[string]schema.Attribute{
			"id": schema.StringAttribute{Computed: true, Description: "The customer_id."},
			"customer_id": schema.StringAttribute{
				Optional: true, Computed: true,
				Description: "The organisation. Defaults to the API key's own; admins must set it.",
			},
			"sites": schema.MapNestedAttribute{
				Computed:    true,
				Description: "Keyed by site name.",
				NestedObject: schema.NestedAttributeObject{
					Attributes: map[string]schema.Attribute{
						"id":           schema.StringAttribute{Computed: true},
						"name":         schema.StringAttribute{Computed: true},
						"kind":         schema.StringAttribute{Computed: true, Description: "site or pop."},
						"location":     schema.StringAttribute{Computed: true},
						"lan_prefixes": schema.ListAttribute{Computed: true, ElementType: types.StringType},
					},
				},
			},
		},
	}
}

func (d *sitesDataSource) Read(ctx context.Context, req datasource.ReadRequest, resp *datasource.ReadResponse) {
	var m sitesModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &m)...)
	if resp.Diagnostics.HasError() {
		return
	}
	cid := m.CustomerID.ValueString()
	if cid == "" {
		me, err := d.client.Me(ctx)
		if err != nil {
			apiError(&resp.Diagnostics, "read who the API key belongs to", err)
			return
		}
		if me.CustomerID == nil || *me.CustomerID == "" {
			resp.Diagnostics.AddError("customer_id is needed",
				"This API key is not tied to one organisation (an admin key); set customer_id.")
			return
		}
		cid = *me.CustomerID
	}
	sites, err := d.client.Sites(ctx)
	if err != nil {
		apiError(&resp.Diagnostics, "list sites", err)
		return
	}
	objs := map[string]attr.Value{}
	for _, s := range sites {
		if !strings.EqualFold(s.CustomerID, cid) {
			continue
		}
		prefixes := make([]attr.Value, 0, len(s.LANPrefixes))
		for _, p := range s.LANPrefixes {
			prefixes = append(prefixes, types.StringValue(p))
		}
		lp, diags := types.ListValue(types.StringType, prefixes)
		resp.Diagnostics.Append(diags...)
		obj, diags := types.ObjectValue(siteAttrTypes, map[string]attr.Value{
			"id":           types.StringValue(s.ID),
			"name":         types.StringValue(s.Name),
			"kind":         types.StringValue(s.Kind),
			"location":     types.StringValue(s.Location),
			"lan_prefixes": lp,
		})
		resp.Diagnostics.Append(diags...)
		objs[s.Name] = obj
	}
	sm, diags := types.MapValue(types.ObjectType{AttrTypes: siteAttrTypes}, objs)
	resp.Diagnostics.Append(diags...)
	m.ID = types.StringValue(cid)
	m.CustomerID = types.StringValue(cid)
	m.Sites = sm
	resp.Diagnostics.Append(resp.State.Set(ctx, &m)...)
}
