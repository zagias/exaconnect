//go:build linux

package probe

import "syscall"

// bindControl pins the socket to a device so a probe can only leave through
// the path it measures (SO_BINDTODEVICE).
func bindControl(dev string) func(network, address string, c syscall.RawConn) error {
	if dev == "" {
		return nil
	}
	return func(_, _ string, c syscall.RawConn) error {
		var serr error
		if err := c.Control(func(fd uintptr) { serr = syscall.BindToDevice(int(fd), dev) }); err != nil {
			return err
		}
		return serr
	}
}
