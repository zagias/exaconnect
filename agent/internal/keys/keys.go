// Package keys holds the agent's local secrets: the WireGuard key pair and the
// mTLS client key and certificate. Private keys are generated here and never
// leave the node; only the public key and a CSR are sent to the controller.
package keys

import (
	"crypto/ecdh"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/hex"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type Store struct{ Dir string }

func (s Store) path(name string) string { return filepath.Join(s.Dir, name) }

func (s Store) WGPrivatePath() string { return s.path("wg.key") }
func (s Store) TLSKeyPath() string    { return s.path("client.key") }
func (s Store) CertPath() string      { return s.path("client.crt") }
func (s Store) CAPath() string        { return s.path("ca.crt") }
func (s Store) IdentityPath() string  { return s.path("identity.json") }

// WireGuard returns the base64 private and public keys, generating them on first use.
func (s Store) WireGuard() (priv, pub string, err error) {
	if b, err := os.ReadFile(s.WGPrivatePath()); err == nil {
		priv = strings.TrimSpace(string(b))
	} else if errors.Is(err, os.ErrNotExist) {
		k, err := ecdh.X25519().GenerateKey(rand.Reader)
		if err != nil {
			return "", "", err
		}
		priv = base64.StdEncoding.EncodeToString(k.Bytes())
		if err := writeSecret(s.WGPrivatePath(), []byte(priv+"\n")); err != nil {
			return "", "", err
		}
	} else {
		return "", "", err
	}
	pub, err = WGPublic(priv)
	return priv, pub, err
}

// WGPublic derives the WireGuard public key from a base64 private key.
func WGPublic(priv string) (string, error) {
	raw, err := base64.StdEncoding.DecodeString(priv)
	if err != nil || len(raw) != 32 {
		return "", fmt.Errorf("bad wireguard private key")
	}
	k, err := ecdh.X25519().NewPrivateKey(raw)
	if err != nil {
		return "", err
	}
	return base64.StdEncoding.EncodeToString(k.PublicKey().Bytes()), nil
}

// CSR returns a PEM certificate request for the node, generating the TLS key on first use.
func (s Store) CSR(nodeName string) ([]byte, error) {
	key, err := s.tlsKey()
	if err != nil {
		return nil, err
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: nodeName},
	}, key)
	if err != nil {
		return nil, err
	}
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der}), nil
}

func (s Store) tlsKey() (*ecdsa.PrivateKey, error) {
	if b, err := os.ReadFile(s.TLSKeyPath()); err == nil {
		blk, _ := pem.Decode(b)
		if blk == nil {
			return nil, fmt.Errorf("bad %s", s.TLSKeyPath())
		}
		return x509.ParseECPrivateKey(blk.Bytes)
	} else if !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	k, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return nil, err
	}
	der, err := x509.MarshalECPrivateKey(k)
	if err != nil {
		return nil, err
	}
	if err := writeSecret(s.TLSKeyPath(), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: der})); err != nil {
		return nil, err
	}
	return k, nil
}

// SaveCerts stores the signed client certificate and the controller CA.
func (s Store) SaveCerts(cert, ca []byte) error {
	if err := writeSecret(s.CertPath(), cert); err != nil {
		return err
	}
	return writeSecret(s.CAPath(), ca)
}

// Fingerprint is the SHA-256 of a PEM certificate's DER bytes, as lowercase hex.
func Fingerprint(certPEM []byte) (string, error) {
	blk, _ := pem.Decode(certPEM)
	if blk == nil || blk.Type != "CERTIFICATE" {
		return "", fmt.Errorf("not a PEM certificate")
	}
	sum := sha256.Sum256(blk.Bytes)
	return hex.EncodeToString(sum[:]), nil
}

func writeSecret(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, data, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

func (s Store) ReadCA() ([]byte, error) { return os.ReadFile(s.CAPath()) }

// Cert returns the stored client certificate.
func (s Store) Cert() (*x509.Certificate, error) {
	b, err := os.ReadFile(s.CertPath())
	if err != nil {
		return nil, err
	}
	return parseCert(b)
}

func parseCert(certPEM []byte) (*x509.Certificate, error) {
	blk, _ := pem.Decode(certPEM)
	if blk == nil || blk.Type != "CERTIFICATE" {
		return nil, fmt.Errorf("not a PEM certificate")
	}
	return x509.ParseCertificate(blk.Bytes)
}

// RenewAt is when a certificate should be renewed: two thirds of the way
// through its lifetime.
func RenewAt(c *x509.Certificate) time.Time {
	life := c.NotAfter.Sub(c.NotBefore)
	return c.NotBefore.Add(life * 2 / 3)
}

// InstallCert replaces the client certificate with a renewed one. The new
// certificate is written beside the old one and checked first: it must parse,
// be for this node's private key and node name, chain to the stored CA and be
// valid now. Only then is it renamed over the old one, so a crash or a bad
// certificate at any point leaves the old certificate in place.
func (s Store) InstallCert(certPEM []byte, now time.Time) error {
	old, err := s.Cert()
	if err != nil {
		return fmt.Errorf("read current certificate: %w", err)
	}
	if err := s.verifyCert(certPEM, old.Subject.CommonName, now); err != nil {
		return fmt.Errorf("renewed certificate rejected: %w", err)
	}
	tmp := s.CertPath() + ".new"
	if err := writeSynced(tmp, certPEM); err != nil {
		return err
	}
	// Check what is on disk, not what is in memory.
	saved, err := os.ReadFile(tmp)
	if err != nil {
		return err
	}
	if err := s.verifyCert(saved, old.Subject.CommonName, now); err != nil {
		os.Remove(tmp)
		return fmt.Errorf("saved certificate rejected: %w", err)
	}
	if _, err := tls.X509KeyPair(saved, mustRead(s.TLSKeyPath())); err != nil {
		os.Remove(tmp)
		return fmt.Errorf("saved certificate does not load with the key: %w", err)
	}
	if err := os.Rename(tmp, s.CertPath()); err != nil {
		return err
	}
	return syncDir(s.Dir)
}

func (s Store) verifyCert(certPEM []byte, name string, now time.Time) error {
	c, err := parseCert(certPEM)
	if err != nil {
		return err
	}
	key, err := s.tlsKey()
	if err != nil {
		return err
	}
	pub, ok := c.PublicKey.(*ecdsa.PublicKey)
	if !ok || !pub.Equal(&key.PublicKey) {
		return fmt.Errorf("not issued for this node's key")
	}
	if c.Subject.CommonName != name {
		return fmt.Errorf("issued for %q, not %q", c.Subject.CommonName, name)
	}
	ca, err := s.ReadCA()
	if err != nil {
		return err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(ca) {
		return fmt.Errorf("bad CA in %s", s.CAPath())
	}
	_, err = c.Verify(x509.VerifyOptions{Roots: pool, CurrentTime: now, KeyUsages: []x509.ExtKeyUsage{x509.ExtKeyUsageClientAuth}})
	return err
}

func mustRead(p string) []byte { b, _ := os.ReadFile(p); return b }

// writeSynced writes a secret file and flushes it to disk before returning.
func writeSynced(path string, data []byte) error {
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o600)
	if err != nil {
		return err
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return err
	}
	if err := f.Sync(); err != nil {
		f.Close()
		return err
	}
	return f.Close()
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	// Some filesystems cannot sync a directory; the rename has still happened.
	_ = d.Sync()
	return nil
}
