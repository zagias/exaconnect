// Package provider is the ExaConnect Terraform provider
// (registry.terraform.io/exacarib/exaconnect), built on terraform-plugin-framework.
package provider

import (
	"context"
	"os"

	"github.com/hashicorp/terraform-plugin-framework/datasource"
	"github.com/hashicorp/terraform-plugin-framework/path"
	"github.com/hashicorp/terraform-plugin-framework/provider"
	"github.com/hashicorp/terraform-plugin-framework/provider/schema"
	"github.com/hashicorp/terraform-plugin-framework/resource"
	"github.com/hashicorp/terraform-plugin-framework/types"

	"github.com/zagias/exaconnect/terraform/internal/client"
)

var _ provider.Provider = (*exaProvider)(nil)

type exaProvider struct {
	version string
}

// New returns the provider factory used by main and the tests.
func New(version string) func() provider.Provider {
	return func() provider.Provider { return &exaProvider{version: version} }
}

type providerModel struct {
	URL    types.String `tfsdk:"url"`
	APIKey types.String `tfsdk:"api_key"`
}

func (p *exaProvider) Metadata(_ context.Context, _ provider.MetadataRequest, resp *provider.MetadataResponse) {
	resp.TypeName = "exaconnect"
	resp.Version = p.version
}

func (p *exaProvider) Schema(_ context.Context, _ provider.SchemaRequest, resp *provider.SchemaResponse) {
	resp.Schema = schema.Schema{
		Description: "Manage ExaConnect circuits, internet breakout, firewall rules, port forwards and traffic rules " +
			"with the same REST API the portal uses.",
		Attributes: map[string]schema.Attribute{
			"url": schema.StringAttribute{
				Optional:    true,
				Description: "The controller, like https://connect.exacarib.com. Defaults to EXACONNECT_URL.",
			},
			"api_key": schema.StringAttribute{
				Optional:    true,
				Sensitive:   true,
				Description: "An API key (exa_...) from the portal's Account screen. Defaults to EXACONNECT_API_KEY.",
			},
		},
	}
}

func (p *exaProvider) Configure(ctx context.Context, req provider.ConfigureRequest, resp *provider.ConfigureResponse) {
	var cfg providerModel
	resp.Diagnostics.Append(req.Config.Get(ctx, &cfg)...)
	if resp.Diagnostics.HasError() {
		return
	}
	if cfg.URL.IsUnknown() || cfg.APIKey.IsUnknown() {
		resp.Diagnostics.AddError("Provider settings not known yet",
			"url and api_key must be known when Terraform plans; set them from variables or the environment.")
		return
	}
	url := os.Getenv("EXACONNECT_URL")
	if !cfg.URL.IsNull() {
		url = cfg.URL.ValueString()
	}
	key := os.Getenv("EXACONNECT_API_KEY")
	if !cfg.APIKey.IsNull() {
		key = cfg.APIKey.ValueString()
	}
	if url == "" {
		resp.Diagnostics.AddAttributeError(path.Root("url"), "No controller URL",
			"Set url in the provider block or EXACONNECT_URL, like https://connect.exacarib.com.")
	}
	if key == "" {
		resp.Diagnostics.AddAttributeError(path.Root("api_key"), "No API key",
			"Set api_key in the provider block or EXACONNECT_API_KEY. Create a key on the portal's Account screen.")
	}
	if resp.Diagnostics.HasError() {
		return
	}
	c, err := client.New(url, key, nil)
	if err != nil {
		resp.Diagnostics.AddError("Cannot set up the ExaConnect client", err.Error())
		return
	}
	c.UserAgent = "terraform-provider-exaconnect/" + p.version
	resp.DataSourceData = c
	resp.ResourceData = c
}

func (p *exaProvider) Resources(_ context.Context) []func() resource.Resource {
	return []func() resource.Resource{
		NewCloudCircuitResource,
		NewSiteCircuitResource,
		NewFirewallRuleResource,
		NewPortForwardResource,
		NewSiteInternetResource,
		NewTrafficRuleResource,
	}
}

func (p *exaProvider) DataSources(_ context.Context) []func() datasource.DataSource {
	return []func() datasource.DataSource{
		NewMeDataSource,
		NewSitesDataSource,
	}
}
