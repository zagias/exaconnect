// Package client talks to the controller: enrolment over server-authenticated
// TLS (the CA is pinned by fingerprint), then everything else over mutual TLS.
package client

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"strconv"
	"sync"
	"sync/atomic"
	"time"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/keys"
	"github.com/zagias/exaconnect/agent/internal/steer"
)

type Client struct {
	Base string
	HTTP *http.Client

	// keys and cert are set by New: the client certificate is read through
	// cert on every handshake, so a renewal takes effect without a restart.
	keys    *keys.Store
	cert    atomic.Pointer[tls.Certificate]
	certMu  sync.Mutex
	certMod time.Time // modification time of the loaded certificate file
}

// FetchCA downloads the controller CA and checks it against the pinned
// fingerprint given with the enrolment token.
func FetchCA(ctx context.Context, base, fingerprint string) ([]byte, error) {
	// Verification is done by fingerprint below, not by the TLS chain, because
	// the agent has no trust anchor yet.
	hc := &http.Client{Timeout: 10 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{InsecureSkipVerify: true, MinVersion: tls.VersionTLS12}, //nolint:gosec
	}}
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, base+"/api/v1/ca.pem", nil)
	resp, err := hc.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("fetch CA: %s", resp.Status)
	}
	ca, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return nil, err
	}
	fp, err := keys.Fingerprint(ca)
	if err != nil {
		return nil, err
	}
	if fp != fingerprint {
		return nil, fmt.Errorf("controller CA fingerprint %s does not match the expected %s", fp, fingerprint)
	}
	return ca, nil
}

type EnrolRequest struct {
	Token       string `json:"token"`
	NodeName    string `json:"node_name"`
	CSR         string `json:"csr_pem"`
	WGPublicKey string `json:"wg_public_key"`
}

type EnrolResponse struct {
	NodeID string `json:"node_id"`
	Cert   string `json:"cert_pem"`
	CA     string `json:"ca_pem"`
}

func Enrol(ctx context.Context, base string, ca []byte, req EnrolRequest) (*EnrolResponse, error) {
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("bad CA")
	}
	c := &Client{Base: base, HTTP: &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12},
	}}}
	var resp EnrolResponse
	if err := c.do(ctx, http.MethodPost, "/api/v1/enrol", req, &resp); err != nil {
		return nil, err
	}
	return &resp, nil
}

// New builds an mTLS client from the stored key, certificate and CA.
func New(base string, ks keys.Store) (*Client, error) {
	pool := x509.NewCertPool()
	ca, err := ks.ReadCA()
	if err != nil {
		return nil, err
	}
	if !pool.AppendCertsFromPEM(ca) {
		return nil, fmt.Errorf("bad CA in %s", ks.CAPath())
	}
	c := &Client{Base: base, keys: &ks}
	if err := c.loadCert(); err != nil {
		return nil, err
	}
	c.HTTP = &http.Client{Timeout: 15 * time.Second, Transport: &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12,
			GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
				return c.cert.Load(), nil
			}},
	}}
	return c, nil
}

func (c *Client) loadCert() error {
	c.certMu.Lock()
	defer c.certMu.Unlock()
	fi, err := os.Stat(c.keys.CertPath())
	if err != nil {
		return fmt.Errorf("load client certificate: %w", err)
	}
	cert, err := tls.LoadX509KeyPair(c.keys.CertPath(), c.keys.TLSKeyPath())
	if err != nil {
		return fmt.Errorf("load client certificate: %w", err)
	}
	c.cert.Store(&cert)
	c.certMod = fi.ModTime()
	return nil
}

// refresh picks up a certificate renewed by another process (exa-agent
// renew) before the next request, and drops connections made with the old one.
func (c *Client) refresh() {
	if c.keys == nil {
		return
	}
	fi, err := os.Stat(c.keys.CertPath())
	c.certMu.Lock()
	changed := err == nil && !fi.ModTime().Equal(c.certMod)
	c.certMu.Unlock()
	if !changed {
		return
	}
	if c.loadCert() == nil {
		c.closeIdle()
	}
}

func (c *Client) closeIdle() {
	if t, ok := c.HTTP.Transport.(*http.Transport); ok {
		t.CloseIdleConnections()
	}
}

type renewRequest struct {
	CSR string `json:"csr_pem"`
}

type renewResponse struct {
	Cert string `json:"cert_pem"`
	CA   string `json:"ca_pem"`
}

// RenewDue reports whether the client certificate is two thirds through its
// lifetime, and when that point is.
func (c *Client) RenewDue(now time.Time) (bool, time.Time, error) {
	if c.keys == nil {
		return false, time.Time{}, nil
	}
	cert, err := c.keys.Cert()
	if err != nil {
		return false, time.Time{}, err
	}
	at := keys.RenewAt(cert)
	return !now.Before(at), at, nil
}

// Renew asks the controller for a new client certificate over the current
// one, installs it (the old one stays on disk until the new one is saved and
// checked) and switches new connections to it. The controller retires the
// old certificate as soon as it issues the new one.
func (c *Client) Renew(ctx context.Context) (*x509.Certificate, error) {
	if c.keys == nil {
		return nil, fmt.Errorf("renew: client has no key store")
	}
	cur, err := c.keys.Cert()
	if err != nil {
		return nil, err
	}
	csr, err := c.keys.CSR(cur.Subject.CommonName)
	if err != nil {
		return nil, err
	}
	var resp renewResponse
	if err := c.do(ctx, http.MethodPost, "/api/v1/agent/renew", renewRequest{CSR: string(csr)}, &resp); err != nil {
		return nil, err
	}
	if err := c.keys.InstallCert([]byte(resp.Cert), time.Now()); err != nil {
		return nil, err
	}
	if err := c.loadCert(); err != nil {
		return nil, err
	}
	// Connections kept alive still present the old certificate.
	c.closeIdle()
	return c.keys.Cert()
}

// DesiredState returns the controller's desired state, or nil if it is still `have`.
func (c *Client) DesiredState(ctx context.Context, have int64) (*desired.State, error) {
	c.refresh()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.Base+"/api/v1/agent/desired-state?have="+strconv.FormatInt(have, 10), nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusNoContent:
		return nil, nil
	case http.StatusOK:
		var s desired.State
		if err := json.NewDecoder(io.LimitReader(resp.Body, 4<<20)).Decode(&s); err != nil {
			return nil, err
		}
		return &s, nil
	default:
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("desired state: %s: %s", resp.Status, bytes.TrimSpace(b))
	}
}

// Steering returns the controller's steering map, or nil if it is still `have`.
func (c *Client) Steering(ctx context.Context, have int64) (*steer.Map, error) {
	c.refresh()
	req, _ := http.NewRequestWithContext(ctx, http.MethodGet, c.Base+"/api/v1/agent/steering?have="+strconv.FormatInt(have, 10), nil)
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	switch resp.StatusCode {
	case http.StatusNoContent:
		return nil, nil
	case http.StatusOK:
		var m steer.Map
		if err := json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(&m); err != nil {
			return nil, err
		}
		return &m, nil
	default:
		b, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return nil, fmt.Errorf("steering: %s: %s", resp.Status, bytes.TrimSpace(b))
	}
}

type Status struct {
	AppliedVersion int64  `json:"applied_version"`
	OK             bool   `json:"ok"`
	Error          string `json:"error,omitempty"`
	AgentVersion   string `json:"agent_version"`
}

func (c *Client) ReportStatus(ctx context.Context, s Status) error {
	return c.do(ctx, http.MethodPost, "/api/v1/agent/status", s, nil)
}

func (c *Client) PostTelemetry(ctx context.Context, t any) error {
	return c.do(ctx, http.MethodPost, "/api/v1/agent/telemetry", t, nil)
}

func (c *Client) do(ctx context.Context, method, path string, body, out any) error {
	c.refresh()
	b, err := json.Marshal(body)
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, method, c.Base+path, bytes.NewReader(b))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.HTTP.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode/100 != 2 {
		msg, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		return fmt.Errorf("%s %s: %s: %s", method, path, resp.Status, bytes.TrimSpace(msg))
	}
	if out != nil {
		return json.NewDecoder(io.LimitReader(resp.Body, 1<<20)).Decode(out)
	}
	return nil
}
