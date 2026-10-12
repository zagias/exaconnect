package console

import (
	"bytes"
	"context"
	"crypto/ed25519"
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"
)

// Zero-touch installation (ADR 0050): a box that has not joined Connect calls
// home over its internet port, says who it is (its serial number, signed with a
// key it made on first boot), and waits. Once ExaCarib staff have assigned that
// serial number to a customer's location in the portal, the answer carries what
// the join step needs and the box joins by itself: nobody types a setup code or
// opens the console. The console still works alongside, for boxes nobody assigned.

// DefaultZTPURL is where boxes call home unless told otherwise. It is the
// portal's own address, so the box can check it with the public certificate
// authorities it already trusts; the agent gateway's certificate comes later,
// pinned by the fingerprint in the answer.
const DefaultZTPURL = "https://connect.exacarib.com"

const (
	ztpKeyFile  = "ztp-key"
	ztpURLFile  = "/etc/exacarib/ztp-url"
	ztpSignWhat = "exacarib-ztp-v1"
)

var serialClean = regexp.MustCompile(`[^A-Z0-9._-]`)

// placeholder serials firmware leaves in place of a real one.
var placeholder = map[string]bool{
	"": true, "0": true, "00000000": true, "0123456789": true, "123456789": true, "NONE": true, "NA": true, "N-A": true,
	"DEFAULT-STRING": true, "DEFAULTSTRING": true, "TO-BE-FILLED-BY-O.E.M.": true, "TOBEFILLEDBYO.E.M.": true,
	"SYSTEM-SERIAL-NUMBER": true, "SYSTEMSERIALNUMBER": true, "NOT-SPECIFIED": true, "NOT-APPLICABLE": true,
}

// NormaliseSerial makes a serial number comparable however it was typed:
// upper case, spaces as dashes, nothing but letters, digits, '.', '_' and '-'.
func NormaliseSerial(s string) string {
	s = strings.ToUpper(strings.TrimSpace(s))
	s = strings.Join(strings.Fields(s), "-")
	s = serialClean.ReplaceAllString(s, "")
	if len(s) > 64 {
		s = s[:64]
	}
	return s
}

// Serial is the box's serial number: EXA_BOX_SERIAL if set, else the one in
// the firmware (DMI), else "MAC-" and the first port's MAC address, which is
// also on most boxes' labels.
func Serial(root string) string {
	if s := NormaliseSerial(os.Getenv("EXA_BOX_SERIAL")); s != "" {
		return s
	}
	for _, f := range []string{"product_serial", "board_serial", "chassis_serial"} {
		b, err := os.ReadFile(filepath.Join(root, "/sys/class/dmi/id", f))
		if err != nil {
			continue
		}
		if s := NormaliseSerial(string(b)); !placeholder[s] {
			return s
		}
	}
	if ps, err := Ports(root); err == nil {
		for _, p := range ps {
			if p.MAC != "" {
				return "MAC-" + strings.ToUpper(strings.ReplaceAll(p.MAC, ":", ""))
			}
		}
	}
	return ""
}

// ztpURL is where this box calls home, or "" when zero-touch is off.
func (s *Server) ztpURL() string {
	u := s.ZTPURL
	if u == "" {
		u = os.Getenv("EXA_ZTP_URL")
	}
	if u == "" {
		if b, err := os.ReadFile(ztpURLFile); err == nil {
			u = strings.TrimSpace(strings.SplitN(string(b), "\n", 2)[0])
		}
	}
	if u == "" {
		u = DefaultZTPURL
	}
	if strings.EqualFold(u, "off") || !strings.HasPrefix(u, "https://") {
		return ""
	}
	return strings.TrimRight(u, "/")
}

// ztpKey is the box's own zero-touch key, made once.
func ztpKey(dir string) (ed25519.PrivateKey, error) {
	p := filepath.Join(dir, ztpKeyFile)
	if b, err := os.ReadFile(p); err == nil && len(b) == ed25519.SeedSize {
		return ed25519.NewKeyFromSeed(b), nil
	}
	_, k, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return nil, err
	}
	if err := os.WriteFile(p, k.Seed(), 0o600); err != nil {
		return nil, err
	}
	return k, nil
}

// ZTPKeyID is a short, readable fingerprint of the box's zero-touch key, shown
// on the console so staff can compare it with the portal's device list.
func ZTPKeyID(pub ed25519.PublicKey) string {
	b := base64.RawStdEncoding.EncodeToString(pub)
	return b[:8] + "…" + b[len(b)-4:]
}

type ztpHello struct {
	Serial    string `json:"serial"`
	PublicKey string `json:"public_key"`
	At        string `json:"at"`
	Signature string `json:"signature"`
	MAC       string `json:"mac,omitempty"`
	Model     string `json:"model,omitempty"`
	Version   string `json:"version,omitempty"`
}

type ztpAnswer struct {
	State       string `json:"state"` // waiting | assigned | enrolled
	Detail      string `json:"detail,omitempty"`
	Controller  string `json:"controller,omitempty"`
	Token       string `json:"token,omitempty"`
	Fingerprint string `json:"ca_fingerprint,omitempty"`
	Site        string `json:"site,omitempty"`
	Retry       int    `json:"retry_s,omitempty"`
}

// ZTPStatus is what the console shows about zero-touch.
type ZTPStatus struct {
	On      bool      `json:"on"`
	URL     string    `json:"url,omitempty"`
	Serial  string    `json:"serial"`
	KeyID   string    `json:"key_id,omitempty"`
	State   string    `json:"state"` // off | calling | waiting | assigned | joined | error
	Detail  string    `json:"detail,omitempty"`
	LastTry time.Time `json:"last_try,omitempty"`
}

func (s *Server) ztpStatus() ZTPStatus {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.ztp
}

func (s *Server) setZTP(f func(*ZTPStatus)) {
	s.mu.Lock()
	f(&s.ztp)
	s.mu.Unlock()
}

// ZeroTouch calls home until the box has joined, through zero-touch or the
// console. It returns at once when zero-touch is off or the box has joined.
func (s *Server) ZeroTouch(ctx context.Context) {
	url := s.ztpURL()
	serial := s.ztpStatus().Serial
	s.setZTP(func(z *ZTPStatus) { z.URL, z.On = url, url != "" })
	if url == "" || serial == "" {
		if url != "" {
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "error", "this box has no serial number" })
		}
		return
	}
	key, err := ztpKey(s.Dir)
	if err != nil {
		s.Log.Warn("zero-touch key", "err", err)
		s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "error", err.Error() })
		return
	}
	pub := key.Public().(ed25519.PublicKey)
	s.setZTP(func(z *ZTPStatus) { z.KeyID, z.State = ZTPKeyID(pub), "calling" })
	s.Log.Info("zero-touch: calling home", "url", url, "serial", serial)
	client := s.ZTPClient
	if client == nil {
		client = &http.Client{Timeout: 20 * time.Second}
	}
	every := s.ZTPEvery
	if every == 0 {
		every = 30 * time.Second
	}
	start := time.Now()
	for {
		if _, _, ok := s.identity(); ok {
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "joined", "" })
			return
		}
		wait := every
		ans, err := s.callHome(ctx, client, url, serial, key)
		s.setZTP(func(z *ZTPStatus) { z.LastTry = time.Now().UTC() })
		switch {
		case err != nil:
			if ctx.Err() != nil {
				return
			}
			s.Log.Debug("zero-touch: no answer", "err", err)
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "error", err.Error() })
		case ans.State == "assigned":
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "assigned", "Joining Connect as "+ans.Site })
			j := JoinInfo{Controller: ans.Controller, Token: ans.Token, Fingerprint: ans.Fingerprint, Name: ans.Site}
			if err := j.Check(); err != nil {
				s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "error", "the answer was not usable: "+err.Error() })
				break
			}
			jerr := s.join(ctx, j, "zero-touch", "Joined Connect by zero-touch as "+j.Name+" at "+j.Controller)
			if jerr == nil {
				s.Log.Info("zero-touch: joined", "name", j.Name)
				s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "joined", "" })
				return
			}
			if errors.Is(jerr, errJoined) {
				return
			}
			s.Log.Warn("zero-touch: join failed", "err", jerr)
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "error", "joining failed: "+joinError(jerr) })
		default:
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = ans.State, ans.Detail })
			if ans.Retry > 0 {
				wait = time.Duration(ans.Retry) * time.Second
			}
		}
		// After the first ten minutes, call less often.
		if time.Since(start) > 10*time.Minute && wait < 2*time.Minute {
			wait = 2 * time.Minute
		}
		select {
		case <-ctx.Done():
			return
		case <-s.joined:
			s.setZTP(func(z *ZTPStatus) { z.State, z.Detail = "joined", "" })
			return
		case <-time.After(wait):
		}
	}
}

func (s *Server) callHome(ctx context.Context, c *http.Client, url, serial string, key ed25519.PrivateKey) (ztpAnswer, error) {
	at := time.Now().UTC().Format(time.RFC3339)
	pub := key.Public().(ed25519.PublicKey)
	h := ztpHello{
		Serial:    serial,
		PublicKey: base64.StdEncoding.EncodeToString(pub),
		At:        at,
		Signature: base64.StdEncoding.EncodeToString(ed25519.Sign(key, []byte(ztpSignWhat+"\n"+serial+"\n"+at))),
		Version:   s.Version,
	}
	if ps, err := Ports(s.Root); err == nil && len(ps) > 0 {
		h.MAC = ps[0].MAC
	}
	if b, err := os.ReadFile(filepath.Join(s.Root, "/sys/class/dmi/id/product_name")); err == nil {
		h.Model = strings.TrimSpace(string(b))
		if len(h.Model) > 80 {
			h.Model = h.Model[:80]
		}
	}
	body, _ := json.Marshal(h)
	cctx, cancel := context.WithTimeout(ctx, 20*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(cctx, http.MethodPost, url+"/api/v1/ztp/hello", bytes.NewReader(body))
	if err != nil {
		return ztpAnswer{}, err
	}
	req.Header.Set("Content-Type", "application/json")
	resp, err := c.Do(req)
	if err != nil {
		return ztpAnswer{}, err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	var a ztpAnswer
	if resp.StatusCode != http.StatusOK {
		var d struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(b, &d)
		if d.Detail == "" {
			d.Detail = resp.Status
		}
		return a, fmt.Errorf("ExaCarib answered %d: %s", resp.StatusCode, d.Detail)
	}
	if err := json.Unmarshal(b, &a); err != nil {
		return a, errors.New("ExaCarib's answer was not JSON")
	}
	return a, nil
}

func (s *Server) handleZTP(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.ztpStatus())
}
