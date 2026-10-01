package probe

import (
	"context"
	"net"
	"testing"
	"time"
)

func TestPacketRoundTrip(t *testing.T) {
	p := packet{Seq: 7, T1: 1, T2: 2, T3: 3}
	got, err := unmarshal(p.marshal(make([]byte, Size)))
	if err != nil || got != p {
		t.Fatalf("got %+v, %v", got, err)
	}
	if _, err := unmarshal([]byte("hello world, not a probe packet!")); err == nil {
		t.Fatal("expected bad magic to be rejected")
	}
}

func TestStatsLossAndJitter(t *testing.T) {
	s := NewStats("wg-a", time.Second)
	t0 := time.Unix(1000, 0)
	rtts := []time.Duration{20, 30, -1, 20, 30} // ms; -1 = lost
	for i, r := range rtts {
		s.Sent(uint32(i+1), t0.Add(time.Duration(i)*time.Second))
		if r > 0 {
			s.Received(uint32(i+1), r*time.Millisecond)
		}
	}
	w, ok := s.Collect(t0.Add(10 * time.Second))
	if !ok {
		t.Fatal("no window")
	}
	if w.Sent != 5 || w.Received != 4 || w.LossPct != 20 {
		t.Fatalf("sent/recv/loss = %d/%d/%v", w.Sent, w.Received, w.LossPct)
	}
	if w.RTTAvgMS != 25 || w.RTTMinMS != 20 || w.RTTMaxMS != 30 {
		t.Fatalf("rtt avg/min/max = %v/%v/%v", w.RTTAvgMS, w.RTTMinMS, w.RTTMaxMS)
	}
	// Three 10 ms differences: J = 10*(1-(15/16)^3) ≈ 1.76 ms.
	if w.JitterMS < 1.7 || w.JitterMS > 1.8 {
		t.Fatalf("jitter = %v", w.JitterMS)
	}
	if _, ok := s.Collect(t0.Add(20 * time.Second)); ok {
		t.Fatal("records should be reported once")
	}
}

func TestStatsWaitsForTimeout(t *testing.T) {
	s := NewStats("wg-a", 2*time.Second)
	now := time.Unix(1000, 0)
	s.Sent(1, now.Add(-time.Second)) // still in flight
	if _, ok := s.Collect(now); ok {
		t.Fatal("in-flight probe must not be counted yet")
	}
}

func TestSenderAndReflector(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	go Reflect(ctx, pc)
	st := NewStats("lo", 500*time.Millisecond)
	snd := &Sender{Path: "lo", Target: pc.LocalAddr().String(), Interval: 20 * time.Millisecond, Stats: st}
	go snd.Run(ctx)
	time.Sleep(400 * time.Millisecond)
	cancel()
	w, ok := st.Collect(time.Now().Add(time.Second))
	if !ok || w.Received == 0 || w.LossPct > 50 || w.RTTAvgMS <= 0 || w.RTTAvgMS > 50 {
		t.Fatalf("unexpected window %+v", w)
	}
}
