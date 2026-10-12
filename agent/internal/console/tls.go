package console

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/hex"
	"encoding/pem"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"time"
)

// consoleCert loads the console's own certificate, making a self-signed one
// on first start. Browsers warn about it once; the console shows its
// fingerprint so it can be checked against `exa-agent console-code`.
func consoleCert(dir, host string) (tls.Certificate, string, error) {
	crt, key := filepath.Join(dir, "console-tls.crt"), filepath.Join(dir, "console-tls.key")
	if c, err := tls.LoadX509KeyPair(crt, key); err == nil {
		return c, certFingerprint(c), nil
	}
	pk, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	serial, _ := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 120))
	tpl := &x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{CommonName: host, Organization: []string{"ExaCarib box console"}},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().AddDate(10, 0, 0),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     []string{host, "localhost"},
	}
	if ifs, err := net.InterfaceAddrs(); err == nil {
		for _, a := range ifs {
			if ip, _, err := net.ParseCIDR(a.String()); err == nil {
				tpl.IPAddresses = append(tpl.IPAddresses, ip)
			}
		}
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &pk.PublicKey, pk)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	kb, err := x509.MarshalECPrivateKey(pk)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return tls.Certificate{}, "", err
	}
	if err := os.WriteFile(key, pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: kb}), 0o600); err != nil {
		return tls.Certificate{}, "", err
	}
	if err := os.WriteFile(crt, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), 0o644); err != nil {
		return tls.Certificate{}, "", err
	}
	c, err := tls.LoadX509KeyPair(crt, key)
	if err != nil {
		return tls.Certificate{}, "", err
	}
	return c, certFingerprint(c), nil
}

// CertFingerprint is the SHA-256 of the console certificate, if it exists.
func CertFingerprint(dir string) string {
	c, err := tls.LoadX509KeyPair(filepath.Join(dir, "console-tls.crt"), filepath.Join(dir, "console-tls.key"))
	if err != nil {
		return ""
	}
	return certFingerprint(c)
}

func certFingerprint(c tls.Certificate) string {
	if len(c.Certificate) == 0 {
		return ""
	}
	s := sha256.Sum256(c.Certificate[0])
	return hex.EncodeToString(s[:])
}
