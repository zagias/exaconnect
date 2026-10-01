// Package probe implements TWAMP-light style path probes: the site sends a
// sequence number and its send time, the PoP reflector stamps its receive and
// send times and echoes the packet back. RTT excludes the reflector's own
// processing time: (t4 - t1) - (t3 - t2).
package probe

import (
	"encoding/binary"
	"errors"
)

const (
	Size  = 64
	magic = 0x45584150 // "EXAP"
)

type packet struct {
	Seq        uint32
	T1, T2, T3 int64 // unix nanoseconds: sender tx, reflector rx, reflector tx
}

func (p packet) marshal(b []byte) []byte {
	b = b[:Size]
	clear(b)
	binary.BigEndian.PutUint32(b[0:], magic)
	binary.BigEndian.PutUint32(b[4:], p.Seq)
	binary.BigEndian.PutUint64(b[8:], uint64(p.T1))
	binary.BigEndian.PutUint64(b[16:], uint64(p.T2))
	binary.BigEndian.PutUint64(b[24:], uint64(p.T3))
	return b
}

var errBadPacket = errors.New("not an exaconnect probe")

func unmarshal(b []byte) (packet, error) {
	if len(b) < 32 || binary.BigEndian.Uint32(b[0:]) != magic {
		return packet{}, errBadPacket
	}
	return packet{
		Seq: binary.BigEndian.Uint32(b[4:]),
		T1:  int64(binary.BigEndian.Uint64(b[8:])),
		T2:  int64(binary.BigEndian.Uint64(b[16:])),
		T3:  int64(binary.BigEndian.Uint64(b[24:])),
	}, nil
}
