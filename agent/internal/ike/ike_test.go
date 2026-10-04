package ike

import "testing"

// Captured from strongSwan 5.9 `swanctl --list-sas`, trimmed and with the
// addresses changed to the lab's.
const sample = `exa-vc7: #3, ESTABLISHED, IKEv2, 5f3c1b2a9d8e7f60_i* 0a1b2c3d4e5f6071_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.10.2' @ 100.64.10.2[500]
  AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
  established 42s ago, rekeying in 13932s
  exa-vc7: #4, reqid 2, INSTALLED, TUNNEL, ESP:AES_CBC-256/HMAC_SHA2_256_128
    installed 42s ago, rekeying in 3311s, expires in 3918s
    in  c4a1b2c3 (0x00000007),   1260 bytes,    15 packets,     2s ago
    out ce5d6f70 (0x00000007),   1344 bytes,    16 packets,     2s ago
    local  0.0.0.0/0
    remote 0.0.0.0/0
exa-vc8: #5, CONNECTING, IKEv2, 1122334455667788_i* 0000000000000000_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.11.2' @ 100.64.11.2[500]
  establishing 3s ago
exa-vc9: #6, ESTABLISHED, IKEv2, 99aabbccddeeff00_i* 0011223344556677_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.12.2' @ 100.64.12.2[500]
exa-vc10: #7, DELETING, IKEv2, 99aabbccddeeff01_i* 0011223344556678_r
exa-vc11: #8, DELETING, IKEv2, 99aabbccddeeff02_i* 0011223344556679_r
exa-vc11: #9, ESTABLISHED, IKEv2, 99aabbccddeeff03_i* 001122334455667a_r
  exa-vc11: #10, reqid 5, INSTALLED, TUNNEL, ESP:AES_GCM_16-256
`

func TestParse(t *testing.T) {
	got := Parse(sample)
	want := map[string]string{
		"exa-vc7":  Up,
		"exa-vc8":  Connecting,
		"exa-vc9":  Connecting, // IKE up, no CHILD_SA yet
		"exa-vc10": Down,
		"exa-vc11": Up, // reauthentication: the new SA wins
	}
	if len(got) != len(want) {
		t.Fatalf("got %v", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %q, want %q", k, got[k], v)
		}
	}
	if len(Parse("")) != 0 {
		t.Fatal("no SAs should give no states")
	}
}

// Several circuits as strongSwan 5.9 lists them mid-way through a
// reauthentication (vc11), a CHILD_SA rekeying (vc12), and a resilient
// circuit's second tunnel (vc1000007) still connecting.
const multi = `exa-vc7: #3, ESTABLISHED, IKEv2, 5f3c1b2a9d8e7f60_i* 0a1b2c3d4e5f6071_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.10.2' @ 100.64.10.2[500]
  AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
  established 120s ago, rekeying in 13000s
  exa-vc7: #4, reqid 1, INSTALLED, TUNNEL, ESP:AES_CBC-256/HMAC_SHA2_256_128
    installed 120s ago, rekeying in 3311s, expires in 3918s
    in  c4a1b2c3 (0x00000007),   1260 bytes,    15 packets,     2s ago
    out ce5d6f70 (0x00000007),   1344 bytes,    16 packets,     2s ago
    local  0.0.0.0/0
    remote 0.0.0.0/0
exa-vc1000007: #5, CONNECTING, IKEv2, 1122334455667788_i* 0000000000000000_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.10.3' @ 100.64.10.3[500]
  establishing 3s ago
exa-vc11: #8, ESTABLISHED, IKEv2, 99aabbccddeeff02_i 0011223344556679_r*
  local  '100.64.0.2' @ 100.64.0.2[4500]
  remote '100.64.12.2' @ 100.64.12.2[4500]
  AES_CBC-128/HMAC_SHA1_96/PRF_HMAC_SHA1/MODP_1024
  established 86000s ago, reauth in 400s
  exa-vc11: #9, reqid 5, INSTALLED, TUNNEL-in-UDP, ESP:AES_CBC-128/HMAC_SHA1_96
    installed 3000s ago, rekeying in 300s, expires in 600s
exa-vc11: #10, ESTABLISHED, IKEv2, 99aabbccddeeff03_i* 001122334455667a_r
  local  '100.64.0.2' @ 100.64.0.2[4500]
  remote '100.64.12.2' @ 100.64.12.2[4500]
  AES_GCM_16-256/PRF_HMAC_SHA2_256/MODP_2048
  established 5s ago, rekeying in 14000s
  exa-vc11: #11, reqid 5, INSTALLED, TUNNEL-in-UDP, ESP:AES_GCM_16-256
    installed 5s ago, rekeying in 3500s, expires in 3900s
exa-vc12: #12, ESTABLISHED, IKEv2, aa00000000000001_i* bb00000000000001_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.13.2' @ 100.64.13.2[500]
  AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
  established 7300s ago, rekeying in 6000s
  exa-vc12: #13, reqid 6, REKEYED, TUNNEL, ESP:AES_CBC-128/HMAC_SHA2_256_128
    installed 3600s ago, expires in 10s
  exa-vc12: #14, reqid 6, INSTALLED, TUNNEL, ESP:AES_CBC-256/HMAC_SHA2_256_128/MODP_2048
    installed 2s ago, rekeying in 3400s, expires in 3998s
exa-vc13: #15, DELETING, IKEv2, cc00000000000001_i* dd00000000000001_r
  local  '100.64.0.2' @ 100.64.0.2[500]
  remote '100.64.14.2' @ 100.64.14.2[500]
  AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048
  established 900s ago, rekeying in 12000s
`

func TestParseSAs(t *testing.T) {
	got := ParseSAs(multi)
	want := map[string]SA{
		"exa-vc7": {Up, "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048", "AES_CBC-256/HMAC_SHA2_256_128", 120},
		// Connecting: no algorithms yet, and no age.
		"exa-vc1000007": {Connecting, "", "", -1},
		// Two up SAs: the new one, which replaces the old.
		"exa-vc11": {Up, "AES_GCM_16-256/PRF_HMAC_SHA2_256/MODP_2048", "AES_GCM_16-256", 5},
		// The installed CHILD_SA, not the one being replaced.
		"exa-vc12": {Up, "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048", "AES_CBC-256/HMAC_SHA2_256_128/MODP_2048", 7300},
		"exa-vc13": {Down, "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048", "", 900},
	}
	if len(got) != len(want) {
		t.Fatalf("got %+v", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %+v, want %+v", k, got[k], v)
		}
	}
	// Parse agrees, so States' callers see the same states.
	for k, v := range Parse(multi) {
		if want[k].State != v {
			t.Errorf("Parse: %s = %q", k, v)
		}
	}
	// The earlier sample, through the richer parser.
	old := ParseSAs(sample)
	if sa := old["exa-vc7"]; sa.ESPCipher != "AES_CBC-256/HMAC_SHA2_256_128" || sa.EstablishedS != 42 ||
		sa.IKECipher != "AES_CBC-256/HMAC_SHA2_256_128/PRF_HMAC_SHA2_256/MODP_2048" {
		t.Errorf("vc7: %+v", sa)
	}
	if sa := old["exa-vc9"]; sa != (SA{Connecting, "", "", -1}) {
		t.Errorf("vc9: %+v", sa) // established, but with no algorithm or age lines in the capture
	}
	if sa := old["exa-vc11"]; sa != (SA{Up, "", "AES_GCM_16-256", -1}) {
		t.Errorf("vc11: %+v", sa)
	}
	if len(ParseSAs("")) != 0 || NoSA != (SA{Down, "", "", -1}) {
		t.Fatal("no SAs should give none")
	}
}
