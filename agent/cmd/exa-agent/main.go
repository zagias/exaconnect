// Command exa-agent is the ExaConnect edge agent.
//
// M0 is a skeleton: it parses its configuration, reports its version and
// checks the controller's health endpoint. Enrolment, desired-state apply,
// probing, reporting and steering arrive in M1 to M4 (see CLAUDE.md §7).
package main

import (
	"context"
	"flag"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/zagias/exaconnect/agent/internal/version"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "version" {
		fmt.Println(version.String())
		return
	}

	controller := flag.String("controller", envOr("EXA_CONTROLLER", "http://controller:8000"), "controller base URL")
	stateDir := flag.String("state-dir", envOr("EXA_STATE_DIR", "/var/lib/exaconnect"), "where keys and the last good config are kept")
	flag.Parse()

	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	log.Info("starting", "version", version.String(), "controller", *controller, "state_dir", *stateDir)

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	tick := time.NewTicker(10 * time.Second)
	defer tick.Stop()
	for {
		if err := checkController(ctx, *controller); err != nil {
			log.Warn("controller unreachable", "err", err)
		} else {
			log.Info("controller healthy")
		}
		select {
		case <-ctx.Done():
			log.Info("stopping")
			return
		case <-tick.C:
		}
	}
}

func checkController(ctx context.Context, base string) error {
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, base+"/healthz", nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("healthz returned %s", resp.Status)
	}
	return nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
