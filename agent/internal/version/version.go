// Package version reports the agent build. Version and Commit are set with -ldflags at build time.
package version

import (
	"fmt"
	"runtime"
)

var (
	Version = "0.0.0-dev"
	Commit  = "unknown"
)

// String is the one-line version shown by `exa-agent version` and sent to the controller.
func String() string {
	return fmt.Sprintf("exa-agent %s (%s) %s/%s", Version, Commit, runtime.GOOS, runtime.GOARCH)
}
