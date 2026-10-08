package keys

import (
	"crypto/x509"
	"encoding/pem"
	"os"
	"testing"
)

func TestWGPublicKnownVector(t *testing.T) {
	// RFC 7748 §6.1, Alice's key pair, base64 encoded.
	pub, err := WGPublic("dwdtCnMYpX08FsFyUbJmRd9ML4frwJkqsXf7pR25LCo=")
	if err != nil {
		t.Fatal(err)
	}
	if want := "hSDwCYkwp1R0i33ctD73Wg2/Og0mOBr066SpjqqbTmo="; pub != want {
		t.Fatalf("pub = %s, want %s", pub, want)
	}
}

func TestWireGuardPersists(t *testing.T) {
	s := Store{Dir: t.TempDir()}
	p1, pub1, err := s.WireGuard()
	if err != nil {
		t.Fatal(err)
	}
	p2, pub2, _ := s.WireGuard()
	if p1 != p2 || pub1 != pub2 {
		t.Fatal("key changed between calls")
	}
	if fi, _ := os.Stat(s.WGPrivatePath()); fi.Mode().Perm() != 0o600 {
		t.Fatalf("private key mode %v", fi.Mode().Perm())
	}
}

func TestCSR(t *testing.T) {
	s := Store{Dir: t.TempDir()}
	b, err := s.CSR("site-a")
	if err != nil {
		t.Fatal(err)
	}
	blk, _ := pem.Decode(b)
	csr, err := x509.ParseCertificateRequest(blk.Bytes)
	if err != nil {
		t.Fatal(err)
	}
	if csr.Subject.CommonName != "site-a" || csr.CheckSignature() != nil {
		t.Fatalf("bad csr: %+v", csr.Subject)
	}
}
