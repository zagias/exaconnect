// Command terraform-provider-exaconnect is the ExaConnect Terraform provider.
package main

import (
	"context"
	"flag"
	"log"

	"github.com/hashicorp/terraform-plugin-framework/providerserver"

	"github.com/zagias/exaconnect/terraform/internal/provider"
)

// version is set at release time with -ldflags "-X main.version=...".
var version = "dev"

func main() {
	var debug bool
	flag.BoolVar(&debug, "debug", false, "run with support for debuggers like delve")
	flag.Parse()

	err := providerserver.Serve(context.Background(), provider.New(version), providerserver.ServeOpts{
		Address: "registry.terraform.io/exacarib/exaconnect",
		Debug:   debug,
	})
	if err != nil {
		log.Fatal(err)
	}
}
