// Command exa-agent is the ExaConnect edge agent.
//
//	exa-agent version
//	exa-agent keygen                       print this node's WireGuard public key
//	exa-agent enrol --controller URL --token T --ca-fingerprint FP --name site-a
//	exa-agent run                          poll, apply, probe, report (long-running)
//	exa-agent apply -f state.json          apply a desired-state file once (debugging)
package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"log/slog"
	"os"
	"os/signal"
	"syscall"

	"github.com/zagias/exaconnect/agent/internal/agent"
	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/keys"
	"github.com/zagias/exaconnect/agent/internal/system"
	"github.com/zagias/exaconnect/agent/internal/version"
)

type identity struct {
	NodeID     string `json:"node_id"`
	NodeName   string `json:"node_name"`
	Controller string `json:"controller"`
}

func main() {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	if len(os.Args) < 2 {
		usage()
	}
	cmd, args := os.Args[1], os.Args[2:]
	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	stateDir := fs.String("state-dir", envOr("EXA_STATE_DIR", "/var/lib/exaconnect"), "keys, certificates and last good state")
	ks := func() keys.Store { return keys.Store{Dir: *stateDir} }

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	var err error
	switch cmd {
	case "version":
		fmt.Println(version.String())
	case "keygen":
		fs.Parse(args)
		var pub string
		_, pub, err = ks().WireGuard()
		if err == nil {
			fmt.Println(pub)
		}
	case "enrol", "enroll":
		ctrl := fs.String("controller", envOr("EXA_CONTROLLER", "https://controller:8443"), "controller base URL")
		token := fs.String("token", os.Getenv("EXA_ENROL_TOKEN"), "one-time enrolment token")
		fp := fs.String("ca-fingerprint", os.Getenv("EXA_CA_FINGERPRINT"), "SHA-256 fingerprint of the controller CA")
		name := fs.String("name", envOr("EXA_NODE_NAME", hostname()), "node name")
		fs.Parse(args)
		err = enrol(ctx, ks(), *ctrl, *token, *fp, *name, log)
	case "run":
		fs.Parse(args)
		err = run(ctx, ks(), log)
	case "apply":
		file := fs.String("f", "", "desired-state JSON file")
		fs.Parse(args)
		err = applyFile(ctx, ks(), *file, log)
	default:
		usage()
	}
	if err != nil {
		log.Error(cmd+" failed", "err", err)
		os.Exit(1)
	}
}

func enrol(ctx context.Context, ks keys.Store, ctrl, token, fp, name string, log *slog.Logger) error {
	if token == "" || fp == "" {
		return errors.New("--token and --ca-fingerprint are required")
	}
	ca, err := client.FetchCA(ctx, ctrl, fp)
	if err != nil {
		return err
	}
	_, pub, err := ks.WireGuard()
	if err != nil {
		return err
	}
	csr, err := ks.CSR(name)
	if err != nil {
		return err
	}
	resp, err := client.Enrol(ctx, ctrl, ca, client.EnrolRequest{Token: token, NodeName: name, CSR: string(csr), WGPublicKey: pub})
	if err != nil {
		return err
	}
	if err := ks.SaveCerts([]byte(resp.Cert), []byte(resp.CA)); err != nil {
		return err
	}
	b, _ := json.MarshalIndent(identity{NodeID: resp.NodeID, NodeName: name, Controller: ctrl}, "", "  ")
	if err := os.WriteFile(ks.IdentityPath(), b, 0o600); err != nil {
		return err
	}
	log.Info("enrolled", "node", name, "node_id", resp.NodeID, "wg_public_key", pub)
	return nil
}

func run(ctx context.Context, ks keys.Store, log *slog.Logger) error {
	b, err := os.ReadFile(ks.IdentityPath())
	if err != nil {
		return fmt.Errorf("not enrolled (run exa-agent enrol first): %w", err)
	}
	var id identity
	if err := json.Unmarshal(b, &id); err != nil {
		return err
	}
	priv, _, err := ks.WireGuard()
	if err != nil {
		return err
	}
	c, err := client.New(id.Controller, ks)
	if err != nil {
		return err
	}
	log = log.With("node", id.NodeName)
	log.Info("starting", "version", version.String(), "controller", id.Controller)
	sys := system.Host{}
	a := &agent.Agent{
		Client:  c,
		Sys:     sys,
		Log:     log,
		Applier: &apply.Applier{Sys: sys, StateDir: ks.Dir, PrivateKey: priv, Log: log},
	}
	return a.Run(ctx)
}

func applyFile(ctx context.Context, ks keys.Store, file string, log *slog.Logger) error {
	s, err := desired.Load(file)
	if err != nil {
		return err
	}
	priv, _, err := ks.WireGuard()
	if err != nil {
		return err
	}
	ap := &apply.Applier{Sys: system.Host{}, StateDir: ks.Dir, PrivateKey: priv, Log: log}
	if err := ap.Apply(ctx, s); err != nil {
		return err
	}
	log.Info("applied", "version", s.Version)
	return nil
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: exa-agent version|keygen|enrol|run|apply [flags]")
	os.Exit(2)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func hostname() string { h, _ := os.Hostname(); return h }
