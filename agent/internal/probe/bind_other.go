//go:build !linux

package probe

import "syscall"

func bindControl(string) func(network, address string, c syscall.RawConn) error { return nil }
