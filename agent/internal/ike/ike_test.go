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
