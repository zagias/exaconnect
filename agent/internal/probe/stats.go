package probe

import (
	"math"
	"sort"
	"sync"
	"time"
)

// Window is one aggregate of probe results for one path.
type Window struct {
	Path     string    `json:"path"`
	Start    time.Time `json:"start"`
	End      time.Time `json:"end"`
	Sent     int       `json:"sent"`
	Received int       `json:"received"`
	LossPct  float64   `json:"loss_pct"`
	RTTAvgMS float64   `json:"rtt_avg_ms"`
	RTTMinMS float64   `json:"rtt_min_ms"`
	RTTMaxMS float64   `json:"rtt_max_ms"`
	JitterMS float64   `json:"jitter_ms"`
}

type record struct {
	sent time.Time
	rtt  time.Duration // <0 until a reply arrives
}

// Stats accumulates probe results. A probe counts as lost if no reply arrives
// within Timeout. Jitter is the RFC 3550 estimator applied to round-trip
// times: J += (|D| - J) / 16, carried across windows.
type Stats struct {
	Path    string
	Timeout time.Duration

	mu      sync.Mutex
	recs    map[uint32]*record
	jitter  float64 // seconds
	lastRTT time.Duration
	haveRTT bool
}

func NewStats(path string, timeout time.Duration) *Stats {
	return &Stats{Path: path, Timeout: timeout, recs: map[uint32]*record{}}
}

func (s *Stats) Sent(seq uint32, at time.Time) {
	s.mu.Lock()
	s.recs[seq] = &record{sent: at, rtt: -1}
	s.mu.Unlock()
}

// Received records a reply; late replies (after Timeout) are ignored.
func (s *Stats) Received(seq uint32, rtt time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, ok := s.recs[seq]
	if !ok || r.rtt >= 0 || rtt > s.Timeout || rtt < 0 {
		return
	}
	r.rtt = rtt
	if s.haveRTT {
		d := math.Abs((rtt - s.lastRTT).Seconds())
		s.jitter += (d - s.jitter) / 16
	}
	s.lastRTT, s.haveRTT = rtt, true
}

// Collect returns the aggregate for probes sent before now-Timeout that have
// not been reported yet, and forgets them. ok is false when there are none.
func (s *Stats) Collect(now time.Time) (w Window, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	cutoff := now.Add(-s.Timeout)
	var seqs []uint32
	for seq, r := range s.recs {
		if r.sent.Before(cutoff) {
			seqs = append(seqs, seq)
		}
	}
	if len(seqs) == 0 {
		return Window{}, false
	}
	sort.Slice(seqs, func(i, j int) bool { return s.recs[seqs[i]].sent.Before(s.recs[seqs[j]].sent) })
	w = Window{Path: s.Path, Start: s.recs[seqs[0]].sent, End: s.recs[seqs[len(seqs)-1]].sent, RTTMinMS: math.Inf(1)}
	var sum float64
	for _, seq := range seqs {
		r := s.recs[seq]
		w.Sent++
		if r.rtt >= 0 {
			ms := float64(r.rtt) / float64(time.Millisecond)
			w.Received++
			sum += ms
			w.RTTMinMS = math.Min(w.RTTMinMS, ms)
			w.RTTMaxMS = math.Max(w.RTTMaxMS, ms)
		}
		delete(s.recs, seq)
	}
	if w.Received > 0 {
		w.RTTAvgMS = round3(sum / float64(w.Received))
		w.RTTMinMS = round3(w.RTTMinMS)
		w.RTTMaxMS = round3(w.RTTMaxMS)
	} else {
		w.RTTMinMS = 0
	}
	w.LossPct = round3(100 * float64(w.Sent-w.Received) / float64(w.Sent))
	w.JitterMS = round3(s.jitter * 1000)
	return w, true
}

func round3(x float64) float64 { return math.Round(x*1000) / 1000 }
