package client

import (
	"context"
	"crypto/tls"
	"encoding/json"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/keys"
)

func TestFetchCAChecksFingerprint(t *testing.T) {
	var caPEM []byte
	srv := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write(caPEM) }))
	defer srv.Close()
	caPEM = pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})
	fp, _ := keys.Fingerprint(caPEM)

	if _, err := FetchCA(context.Background(), srv.URL, fp); err != nil {
		t.Fatalf("matching fingerprint rejected: %v", err)
	}
	if _, err := FetchCA(context.Background(), srv.URL, strings.Repeat("0", 64)); err == nil {
		t.Fatal("wrong fingerprint accepted")
	}
}

func TestEnrolAndMTLS(t *testing.T) {
	var seen EnrolRequest
	srv := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/api/v1/enrol":
			json.NewDecoder(r.Body).Decode(&seen)
			json.NewEncoder(w).Encode(EnrolResponse{NodeID: "n1"})
		case "/api/v1/agent/desired-state":
			if len(r.TLS.PeerCertificates) == 0 {
				http.Error(w, "no client cert", 401)
				return
			}
			w.WriteHeader(http.StatusNoContent)
		}
	}))
	srv.TLS = &tls.Config{ClientAuth: tls.RequestClientCert}
	srv.StartTLS()
	defer srv.Close()
	caPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: srv.Certificate().Raw})

	resp, err := Enrol(context.Background(), srv.URL, caPEM, EnrolRequest{Token: "t", NodeName: "site-a", WGPublicKey: "pub"})
	if err != nil || resp.NodeID != "n1" || seen.NodeName != "site-a" {
		t.Fatalf("enrol: %+v %v", resp, err)
	}

	// Build an mTLS client with a self-signed client cert (the server only requests one).
	ks := keys.Store{Dir: t.TempDir()}
	cert := selfSign(t, ks)
	if err := ks.SaveCerts(cert, caPEM); err != nil {
		t.Fatal(err)
	}
	c, err := New(srv.URL, ks)
	if err != nil {
		t.Fatal(err)
	}
	s, err := c.DesiredState(context.Background(), 1)
	if err != nil || s != nil {
		t.Fatalf("desired state: %v %v", s, err)
	}
}

func selfSign(t *testing.T, ks keys.Store) []byte {
	t.Helper()
	b, err := ks.SelfSignedForTest()
	if err != nil {
		t.Fatal(err)
	}
	return b
}
