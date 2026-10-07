package keys

import (
	"bytes"
	"os"
	"testing"
	"time"
)

// enrolled returns a store holding a key, a CA and a certificate for site-a.
func enrolled(t *testing.T) (Store, *TestCA) {
	t.Helper()
	s := Store{Dir: t.TempDir()}
	ca, err := NewTestCA()
	if err != nil {
		t.Fatal(err)
	}
	csr, err := s.CSR("site-a")
	if err != nil {
		t.Fatal(err)
	}
	cert, err := ca.Sign(csr, "site-a", time.Now().Add(-time.Hour), time.Now().Add(2*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.SaveCerts(cert, ca.PEM); err != nil {
		t.Fatal(err)
	}
	return s, ca
}

func TestRenewAtIsTwoThirdsOfTheLifetime(t *testing.T) {
	s, _ := enrolled(t)
	c, err := s.Cert()
	if err != nil {
		t.Fatal(err)
	}
	life := c.NotAfter.Sub(c.NotBefore)
	if got, want := RenewAt(c), c.NotBefore.Add(life*2/3); !got.Equal(want) {
		t.Fatalf("RenewAt = %v, want %v", got, want)
	}
	// 365 days from issue: due after 243 days and a bit.
	c.NotBefore = time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	c.NotAfter = c.NotBefore.Add(365 * 24 * time.Hour)
	if got := RenewAt(c); got.Sub(c.NotBefore) != 365*24*time.Hour*2/3 {
		t.Fatalf("RenewAt = %v", got)
	}
}

func TestInstallCertReplacesTheCertificate(t *testing.T) {
	s, ca := enrolled(t)
	csr, _ := s.CSR("site-a")
	fresh, err := ca.Sign(csr, "site-a", time.Now().Add(-time.Minute), time.Now().Add(365*24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if err := s.InstallCert(fresh, time.Now()); err != nil {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(s.CertPath()); !bytes.Equal(got, fresh) {
		t.Fatal("certificate not replaced")
	}
	if _, err := os.Stat(s.CertPath() + ".new"); !os.IsNotExist(err) {
		t.Fatal("temporary file left behind")
	}
	if st, _ := os.Stat(s.CertPath()); st.Mode().Perm() != 0o600 {
		t.Fatalf("mode %v", st.Mode())
	}
}

func TestInstallCertKeepsTheOldOneWhenTheNewOneIsBad(t *testing.T) {
	s, ca := enrolled(t)
	old, _ := os.ReadFile(s.CertPath())
	csr, _ := s.CSR("site-a")
	now := time.Now()
	week := now.Add(7 * 24 * time.Hour)

	otherCA, _ := NewTestCA()
	other := Store{Dir: t.TempDir()}
	otherCSR, _ := other.CSR("site-a")

	wrongName, _ := ca.Sign(csr, "site-b", now.Add(-time.Minute), week)
	wrongCA, _ := otherCA.Sign(csr, "site-a", now.Add(-time.Minute), week)
	wrongKey, _ := ca.Sign(otherCSR, "site-a", now.Add(-time.Minute), week)
	expired, _ := ca.Sign(csr, "site-a", now.Add(-2*time.Hour), now.Add(-time.Hour))
	for name, cert := range map[string][]byte{
		"garbage": []byte("not a certificate"), "wrong name": wrongName, "wrong CA": wrongCA,
		"wrong key": wrongKey, "expired": expired,
	} {
		if err := s.InstallCert(cert, now); err == nil {
			t.Errorf("%s: accepted", name)
		}
		if got, _ := os.ReadFile(s.CertPath()); !bytes.Equal(got, old) {
			t.Fatalf("%s: old certificate replaced", name)
		}
	}
}
