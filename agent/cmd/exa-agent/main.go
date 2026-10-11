// Command exa-agent is the ExaConnect edge agent.
//
//	exa-agent version
//	exa-agent keygen                       print this node's WireGuard public key
//	exa-agent enrol --controller URL --token T --ca-fingerprint FP --name site-a
//	exa-agent renew [--force]              renew the mTLS client certificate now
//	exa-agent run [--console :443]         poll, apply, probe, report (long-running); with
//	                                       --console, also serve the box console (ADR 0046)
//	exa-agent console-code                 print the box console's setup code
//	exa-agent console-reset                forget the console password; print a new setup code
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
	"time"

	"github.com/zagias/exaconnect/agent/internal/agent"
	"github.com/zagias/exaconnect/agent/internal/apply"
	"github.com/zagias/exaconnect/agent/internal/client"
	"github.com/zagias/exaconnect/agent/internal/console"
	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/keys"
	"github.com/zagias/exaconnect/agent/internal/steer"
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
		con := fs.String("console", os.Getenv("EXA_CONSOLE"), "serve the box console on this address, e.g. :443")
		fs.Parse(args)
		err = run(ctx, ks(), *con, log)
	case "console-code":
		fs.Parse(args)
		err = consoleCode(ks().Dir, false)
	case "console-reset":
		fs.Parse(args)
		err = consoleCode(ks().Dir, true)
	case "renew":
		force := fs.Bool("force", true, "renew even if the certificate is not yet due")
		fs.Parse(args)
		err = renew(ctx, ks(), *force, log)
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

func readIdentity(ks keys.Store) (identity, error) {
	var id identity
	b, err := os.ReadFile(ks.IdentityPath())
	if err != nil {
		return id, err
	}
	return id, json.Unmarshal(b, &id)
}

// startConsole serves the box console and waits until the box has joined
// Connect, through the console or already.
func startConsole(ctx context.Context, ks keys.Store, addr string, log *slog.Logger) (*console.Server, error) {
	con := &console.Server{
		Dir: ks.Dir, Addr: addr, Version: version.String(), Log: log.With("part", "console"),
		Join: func(ctx context.Context, j console.JoinInfo) error {
			return enrol(ctx, ks, j.Controller, j.Token, j.Fingerprint, j.Name, log)
		},
		Identity: func() (string, string, bool) {
			id, err := readIdentity(ks)
			return id.NodeName, id.Controller, err == nil && id.NodeID != ""
		},
	}
	if err := con.Init(); err != nil {
		return nil, err
	}
	con.Restore(ctx)
	go func() {
		if err := con.Serve(ctx); err != nil {
			log.Error("box console stopped", "err", err)
		}
	}()
	select {
	case <-con.Joined():
		return con, nil
	case <-ctx.Done():
		return nil, nil
	}
}

func run(ctx context.Context, ks keys.Store, consoleAddr string, log *slog.Logger) error {
	var con *console.Server
	if consoleAddr != "" {
		var err error
		if con, err = startConsole(ctx, ks, consoleAddr, log); err != nil || con == nil {
			return err
		}
	}
	id, err := readIdentity(ks)
	if err != nil {
		return fmt.Errorf("not enrolled (run exa-agent enrol first): %w", err)
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
		Steerer: &steer.Steerer{Sys: sys, StateDir: ks.Dir},
	}
	if con != nil {
		a.Box = con.BoxInfo
		con.SetAgent(a)
	}
	return a.Run(ctx)
}

func renew(ctx context.Context, ks keys.Store, force bool, log *slog.Logger) error {
	b, err := os.ReadFile(ks.IdentityPath())
	if err != nil {
		return fmt.Errorf("not enrolled (run exa-agent enrol first): %w", err)
	}
	var id identity
	if err := json.Unmarshal(b, &id); err != nil {
		return err
	}
	c, err := client.New(id.Controller, ks)
	if err != nil {
		return err
	}
	due, at, err := c.RenewDue(time.Now())
	if err != nil {
		return err
	}
	if !due && !force {
		log.Info("client certificate not yet due for renewal", "due", at.Format(time.RFC3339))
		return nil
	}
	cert, err := c.Renew(ctx)
	if err != nil {
		return err
	}
	// A running agent notices the new file before its next request.
	log.Info("client certificate renewed", "serial", cert.SerialNumber.Text(16), "not_after", cert.NotAfter.Format(time.RFC3339))
	return nil
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

func consoleCode(dir string, reset bool) error {
	var code string
	var err error
	if reset {
		code, err = console.ResetPassword(dir)
	} else {
		code, err = console.SetupCode(dir)
	}
	if err != nil {
		return err
	}
	if code == "" {
		fmt.Println("The box console already has an admin password. To set a new one, run: exa-agent console-reset")
		return nil
	}
	fmt.Println("Box console setup code:", code)
	if fp := console.CertFingerprint(dir); fp != "" {
		fmt.Println("Console certificate SHA-256:", fp)
	}
	return nil
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: exa-agent version|keygen|enrol|renew|run|apply|console-code|console-reset [flags]")
	os.Exit(2)
}

func envOr(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func hostname() string { h, _ := os.Hostname(); return h }
