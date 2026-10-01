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
