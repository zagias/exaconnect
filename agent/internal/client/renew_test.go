package client

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"

	"github.com/zagias/exaconnect/agent/internal/keys"
)

// fakeController accepts only the latest certificate it issued, like the
// controller's nodes.cert_serial check, and renews over mTLS.
type fakeController struct {
	mu      sync.Mutex
	ca      *keys.TestCA
	current string // serial of the only certificate accepted
	life    time.Duration
	renews  int
}

func (f *fakeController) serve(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(r.TLS.PeerCertificates) == 0 || r.TLS.PeerCertificates[0].SerialNumber.Text(16) != f.current {
		http.Error(w, "unknown or replaced client certificate", http.StatusUnauthorized)
		return
	}
	switch r.URL.Path {
	case "/api/v1/agent/desired-state":
		w.WriteHeader(http.StatusNoContent)
	case "/api/v1/agent/renew":
		var in renewRequest
		json.NewDecoder(r.Body).Decode(&in)
		cert, err := f.ca.Sign([]byte(in.CSR), r.TLS.PeerCertificates[0].Subject.CommonName, time.Now().Add(-time.Minute), time.Now().Add(f.life))
		if err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		blk, _ := pem.Decode(cert)
		c, _ := x509.ParseCertificate(blk.Bytes)
		f.current = c.SerialNumber.Text(16)
		f.renews++
		json.NewEncoder(w).Encode(renewResponse{Cert: string(cert), CA: string(f.ca.PEM)})
	}
}

// setup enrols a store with a certificate that has `left` of `life` to run.
func setup(t *testing.T, life, left time.Duration) (*fakeController, keys.Store, *httptest.Server) {
	t.Helper()
	ca, err := keys.NewTestCA()
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeController{ca: ca, life: life}
	srv := httptest.NewUnstartedServer(http.HandlerFunc(f.serve))
	pool := x509.NewCertPool()
	pool.AppendCertsFromPEM(ca.PEM)
	srv.TLS = &tls.Config{ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: pool}
	srv.StartTLS()
	t.Cleanup(srv.Close)

	ks := keys.Store{Dir: t.TempDir()}
	csr, _ := ks.CSR("site-a")
	notAfter := time.Now().Add(left)
	cert, err := ca.Sign(csr, "site-a", notAfter.Add(-life), notAfter)
	if err != nil {
		t.Fatal(err)
	}
	blk, _ := pem.Decode(cert)
	c, _ := x509.ParseCertificate(blk.Bytes)
	f.current = c.SerialNumber.Text(16)
	// The stored CA file holds the agent CA and, here, the test server's certificate.
	server := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	if err := ks.SaveCerts(cert, append(append([]byte{}, ca.PEM...), server...)); err != nil {
		t.Fatal(err)
	}
	return f, ks, srv
}

func TestRenewDueAtTwoThirds(t *testing.T) {
	_, ks, srv := setup(t, 300*24*time.Hour, 150*24*time.Hour)
	c, err := New(srv.URL, ks)
	if err != nil {
		t.Fatal(err)
	}
	due, at, err := c.RenewDue(time.Now())
	if err != nil || due {
		t.Fatalf("half way through: due=%v err=%v", due, err)
	}
	if want := time.Now().Add(50 * 24 * time.Hour); at.Sub(want).Abs() > time.Minute {
		t.Fatalf("due at %v, want about %v", at, want)
	}
	if due, _, _ := c.RenewDue(time.Now().Add(51 * 24 * time.Hour)); !due {
		t.Fatal("not due after two thirds")
	}
}

func TestRenewSwapsTheCertificateWithoutARestart(t *testing.T) {
	f, ks, srv := setup(t, 300*24*time.Hour, 50*24*time.Hour)
	c, err := New(srv.URL, ks)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := c.DesiredState(ctx, 1); err != nil {
		t.Fatalf("before renewal: %v", err)
	}
	old, _ := ks.Cert()
	due, _, _ := c.RenewDue(time.Now())
	if !due {
		t.Fatal("expected renewal to be due")
	}
	cert, err := c.Renew(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if cert.SerialNumber.Cmp(old.SerialNumber) == 0 || cert.Subject.CommonName != "site-a" {
		t.Fatalf("renewed certificate: serial %v cn %q", cert.SerialNumber, cert.Subject.CommonName)
	}
	if due, _, _ := c.RenewDue(time.Now()); due {
		t.Fatal("still due after renewal")
	}
	// The same client now presents the new certificate, which the controller accepts.
	if _, err := c.DesiredState(ctx, 1); err != nil {
		t.Fatalf("after renewal: %v", err)
	}
	if f.renews != 1 {
		t.Fatalf("renews = %d", f.renews)
	}
}

func TestRunningClientPicksUpACertificateRenewedElsewhere(t *testing.T) {
	_, ks, srv := setup(t, 300*24*time.Hour, 50*24*time.Hour)
	running, err := New(srv.URL, ks)
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	if _, err := running.DesiredState(ctx, 1); err != nil {
		t.Fatal(err)
	}
	// exa-agent renew in another process.
	time.Sleep(20 * time.Millisecond) // a distinct modification time
	cli, _ := New(srv.URL, ks)
	if _, err := cli.Renew(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := running.DesiredState(ctx, 1); err != nil {
		t.Fatalf("running client after renewal elsewhere: %v", err)
	}
}

func TestFailedRenewalKeepsTheCurrentCertificate(t *testing.T) {
	f, ks, srv := setup(t, 300*24*time.Hour, 50*24*time.Hour)
	c, _ := New(srv.URL, ks)
	f.mu.Lock()
	f.ca, _ = keys.NewTestCA() // the controller now signs with a CA the agent does not trust
	f.mu.Unlock()
	before, _ := ks.Cert()
	if _, err := c.Renew(context.Background()); err == nil {
		t.Fatal("certificate from an unknown CA installed")
	}
	after, _ := ks.Cert()
	if after.SerialNumber.Cmp(before.SerialNumber) != 0 {
		t.Fatal("certificate on disk changed")
	}
}
