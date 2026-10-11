package console

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// The box console has one local account, "admin" (ADR 0046). Until it has a
// password, whoever holds the setup code (printed by `exa-agent console-code`
// on the box, or put on its label at the factory) sets one.
const (
	adminUser     = "admin"
	pbkdf2Iter    = 210_000 // OWASP 2023 for PBKDF2-HMAC-SHA256
	minPassword   = 10
	sessionIdle   = 30 * time.Minute
	sessionMax    = 12 * time.Hour
	maxFails      = 5
	lockout       = time.Minute
	codeAlphabet  = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789" // no 0/O or 1/I
	credsFile     = "console.json"
	setupCodeFile = "console-setup-code"
)

var errLocked = errors.New("too many failed attempts; wait a minute and try again")

type creds struct {
	User  string    `json:"user"`
	Salt  string    `json:"salt"`
	Hash  string    `json:"hash"`
	Iter  int       `json:"iter"`
	SetAt time.Time `json:"set_at"`
}

// pbkdf2 is PBKDF2 with HMAC-SHA-256 (RFC 8018). The agent builds with Go
// 1.22, which has no crypto/pbkdf2.
func pbkdf2(password, salt []byte, iter, keyLen int) []byte {
	prf := hmac.New(sha256.New, password)
	n := prf.Size()
	out := make([]byte, 0, (keyLen+n-1)/n*n)
	u := make([]byte, n)
	var idx [4]byte
	for block := uint32(1); len(out) < keyLen; block++ {
		prf.Reset()
		prf.Write(salt)
		binary.BigEndian.PutUint32(idx[:], block)
		prf.Write(idx[:])
		out = prf.Sum(out)
		t := out[len(out)-n:]
		copy(u, t)
		for i := 1; i < iter; i++ {
			prf.Reset()
			prf.Write(u)
			u = prf.Sum(u[:0])
			for x := range u {
				t[x] ^= u[x]
			}
		}
	}
	return out[:keyLen]
}

func hashPassword(pw string) creds {
	salt := make([]byte, 16)
	_, _ = rand.Read(salt)
	return creds{User: adminUser, Iter: pbkdf2Iter, SetAt: time.Now().UTC(),
		Salt: base64.StdEncoding.EncodeToString(salt),
		Hash: base64.StdEncoding.EncodeToString(pbkdf2([]byte(pw), salt, pbkdf2Iter, 32))}
}

func (c creds) check(user, pw string) bool {
	salt, err1 := base64.StdEncoding.DecodeString(c.Salt)
	want, err2 := base64.StdEncoding.DecodeString(c.Hash)
	if err1 != nil || err2 != nil || c.Iter < 1 {
		return false
	}
	got := pbkdf2([]byte(pw), salt, c.Iter, len(want))
	return subtle.ConstantTimeCompare(got, want) == 1 && subtle.ConstantTimeCompare([]byte(user), []byte(c.User)) == 1
}

func checkNewPassword(pw string) error {
	if len([]rune(pw)) < minPassword {
		return errors.New("use at least 10 characters for the password")
	}
	if strings.EqualFold(pw, adminUser) || strings.EqualFold(pw, "password123") {
		return errors.New("choose a password that is harder to guess")
	}
	return nil
}

func randomToken(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return base64.RawURLEncoding.EncodeToString(b)
}

func newSetupCode() string {
	b := make([]byte, 10)
	_, _ = rand.Read(b)
	var sb strings.Builder
	for i, v := range b {
		if i == 5 {
			sb.WriteByte('-')
		}
		sb.WriteByte(codeAlphabet[int(v)%len(codeAlphabet)])
	}
	return sb.String()
}

func normaliseCode(s string) string {
	s = strings.ToUpper(s)
	return strings.Map(func(r rune) rune {
		if r == '-' || r == ' ' {
			return -1
		}
		return r
	}, s)
}

// SetupCode returns the box's setup code, making one if there is none. It is
// empty once the admin password is set; ResetPassword brings a new one.
func SetupCode(dir string) (string, error) {
	if _, err := os.Stat(filepath.Join(dir, credsFile)); err == nil {
		return "", nil
	}
	p := filepath.Join(dir, setupCodeFile)
	if b, err := os.ReadFile(p); err == nil && len(strings.TrimSpace(string(b))) >= 8 {
		return strings.TrimSpace(string(b)), nil
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return "", err
	}
	code := newSetupCode()
	return code, os.WriteFile(p, []byte(code+"\n"), 0o600)
}

// ResetPassword forgets the admin password, for someone with root on the box
// who has lost it. The next visit to the console asks for a new setup code.
func ResetPassword(dir string) (string, error) {
	if err := os.Remove(filepath.Join(dir, credsFile)); err != nil && !os.IsNotExist(err) {
		return "", err
	}
	_ = os.Remove(filepath.Join(dir, setupCodeFile))
	return SetupCode(dir)
}

type session struct {
	user    string
	created time.Time
	last    time.Time
}

type auth struct {
	dir      string
	mu       sync.Mutex
	sessions map[string]*session
	fails    map[string]*failCount
}

type failCount struct {
	n     int
	until time.Time
}

func newAuth(dir string) *auth {
	return &auth{dir: dir, sessions: map[string]*session{}, fails: map[string]*failCount{}}
}

func (a *auth) creds() (*creds, error) {
	b, err := os.ReadFile(filepath.Join(a.dir, credsFile))
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var c creds
	if err := json.Unmarshal(b, &c); err != nil {
		return nil, err
	}
	return &c, nil
}

func (a *auth) save(c creds) error {
	b, _ := json.MarshalIndent(c, "", "  ")
	p := filepath.Join(a.dir, credsFile)
	tmp := p + ".tmp"
	if err := os.WriteFile(tmp, b, 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, p)
}

// limited reports whether this address must wait before trying again.
func (a *auth) limited(ip string, now time.Time) bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	f := a.fails[ip]
	return f != nil && now.Before(f.until)
}

func (a *auth) failed(ip string, now time.Time) {
	a.mu.Lock()
	defer a.mu.Unlock()
	f := a.fails[ip]
	if f == nil {
		f = &failCount{}
		a.fails[ip] = f
	}
	f.n++
	if f.n >= maxFails {
		f.n, f.until = 0, now.Add(lockout)
	}
}

// claim sets the first admin password, given the setup code.
func (a *auth) claim(ip, code, pw string, now time.Time) (string, error) {
	if a.limited(ip, now) {
		return "", errLocked
	}
	if c, err := a.creds(); err != nil || c != nil {
		return "", errors.New("this box already has an admin password; sign in instead")
	}
	want, err := SetupCode(a.dir)
	if err != nil {
		return "", err
	}
	if subtle.ConstantTimeCompare([]byte(normaliseCode(code)), []byte(normaliseCode(want))) != 1 {
		a.failed(ip, now)
		return "", errors.New("that setup code is not right; run exa-agent console-code on the box to see it")
	}
	if err := checkNewPassword(pw); err != nil {
		return "", err
	}
	if err := a.save(hashPassword(pw)); err != nil {
		return "", err
	}
	_ = os.Remove(filepath.Join(a.dir, setupCodeFile))
	return a.open(adminUser, now), nil
}

func (a *auth) login(ip, user, pw string, now time.Time) (string, error) {
	if a.limited(ip, now) {
		return "", errLocked
	}
	c, err := a.creds()
	if err != nil {
		return "", err
	}
	if c == nil || !c.check(user, pw) {
		a.failed(ip, now)
		return "", errors.New("the user name or password is not right")
	}
	return a.open(c.User, now), nil
}

func (a *auth) changePassword(user, current, next string) error {
	c, err := a.creds()
	if err != nil || c == nil {
		return errors.New("no admin password is set")
	}
	if !c.check(user, current) {
		return errors.New("the current password is not right")
	}
	if err := checkNewPassword(next); err != nil {
		return err
	}
	n := hashPassword(next)
	n.User = c.User
	if err := a.save(n); err != nil {
		return err
	}
	// Every other session ends with the old password.
	a.mu.Lock()
	a.sessions = map[string]*session{}
	a.mu.Unlock()
	return nil
}

func (a *auth) open(user string, now time.Time) string {
	tok := randomToken(32)
	a.mu.Lock()
	a.sessions[tok] = &session{user: user, created: now, last: now}
	a.mu.Unlock()
	return tok
}

func (a *auth) session(tok string, now time.Time) (string, bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	s := a.sessions[tok]
	if s == nil {
		return "", false
	}
	if now.Sub(s.last) > sessionIdle || now.Sub(s.created) > sessionMax {
		delete(a.sessions, tok)
		return "", false
	}
	s.last = now
	return s.user, true
}

func (a *auth) close(tok string) {
	a.mu.Lock()
	delete(a.sessions, tok)
	a.mu.Unlock()
}
