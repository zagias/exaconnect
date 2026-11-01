package apply

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/zagias/exaconnect/agent/internal/desired"
	"github.com/zagias/exaconnect/agent/internal/render"
)

type lastGoodFailure struct {
	*fake
	fail bool
}

func (f *lastGoodFailure) WriteFile(path string, data []byte, mode os.FileMode) error {
	if f.fail && strings.HasSuffix(path, "/last-good.json") {
		f.fail = false
		return errors.New("last-good disk write refused")
	}
	return f.fake.WriteFile(path, data, mode)
}

func TestFirewallRollsBackWhenLastGoodCannotBeSaved(t *testing.T) {
	f := &fake{files: map[string]string{}}
	a := applier(f)
	previous := inetState(1, desired.InternetLocal)
	if err := a.Apply(context.Background(), previous); err != nil {
		t.Fatal(err)
	}
	a.Sys = &lastGoodFailure{fake: f, fail: true}
	newState := inetState(2, desired.InternetLocal)
	newState.Internet.Firewall[0].Dst = []string{"203.0.113.0/24"}
	err := a.Apply(context.Background(), newState)
	if !IsRolledBack(err) || !strings.Contains(err.Error(), "last-good disk write refused") {
		t.Fatalf("persistence failure must restore the old firewall: %v", err)
	}
	if a.LastGood().Version != 1 || f.files["/state/internet-applied.nft"] != render.InternetNFT(previous) {
		t.Fatal("the unrecorded policy remained live after the persistence failure")
	}
}
