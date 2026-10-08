// Package counters reads interface byte counters from sysfs.
package counters

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type Sample struct {
	At      time.Time `json:"at"`
	IfName  string    `json:"ifname"`
	RxBytes uint64    `json:"rx_bytes"`
	TxBytes uint64    `json:"tx_bytes"`
}

// Root is the sysfs net directory; tests point it elsewhere.
var Root = "/sys/class/net"

// Read returns counters for the named interfaces, skipping any that are missing.
func Read(names []string, at time.Time) []Sample {
	var out []Sample
	for _, n := range names {
		rx, err1 := readUint(filepath.Join(Root, n, "statistics", "rx_bytes"))
		tx, err2 := readUint(filepath.Join(Root, n, "statistics", "tx_bytes"))
		if err1 != nil || err2 != nil {
			continue
		}
		out = append(out, Sample{At: at, IfName: n, RxBytes: rx, TxBytes: tx})
	}
	return out
}

func readUint(p string) (uint64, error) {
	b, err := os.ReadFile(p)
	if err != nil {
		return 0, err
	}
	return strconv.ParseUint(strings.TrimSpace(string(b)), 10, 64)
}
