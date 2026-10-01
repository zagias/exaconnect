package frrstate

import (
	"testing"
	"time"
)

func TestParseBFD(t *testing.T) {
	peers, err := ParseBFD([]byte(`[{"multihop":false,"peer":"100.64.1.1","local":"100.64.1.11","interface":"wg-a","status":"up","uptime":12}]`))
	if err != nil || len(peers) != 1 || peers[0].Status != "up" || peers[0].Interface != "wg-a" {
		t.Fatalf("%+v %v", peers, err)
	}
}

func TestParseHandshakes(t *testing.T) {
	now := time.Unix(1000, 0)
	if got := ParseHandshakes("KEY1=\t990\nKEY2=\t0\n", now); got != 10 {
		t.Fatalf("age = %d", got)
	}
	if got := ParseHandshakes("KEY1=\t0\n", now); got != -1 {
		t.Fatalf("no handshake should be -1, got %d", got)
	}
}
