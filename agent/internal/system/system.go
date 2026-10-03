// Package system wraps the commands and files the agent touches, so apply
// logic can be tested with a fake.
package system

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

type Runner interface {
	// Run executes a command and returns its stdout. Stderr goes into the
	// error only: tools such as vtysh print warnings there even on success,
	// which would corrupt output the agent parses.
	Run(ctx context.Context, name string, args ...string) ([]byte, error)
	// WriteFile atomically replaces a file.
	WriteFile(path string, data []byte, perm os.FileMode) error
	Exists(path string) bool
	ReadFile(path string) ([]byte, error)
}

type Host struct{}

func (Host) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, name, args...)
	var out, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &out, &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String() + "\n" + out.String())
		return out.Bytes(), fmt.Errorf("%s %s: %w: %s", name, strings.Join(args, " "), err, msg)
	}
	return out.Bytes(), nil
}

func (Host) WriteFile(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(perm); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

func (Host) Exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

func (Host) ReadFile(path string) ([]byte, error) { return os.ReadFile(path) }

// Files lists and removes files. Host implements it; a Runner that does not
// (an older test fake) simply has no files to list.
type Files interface {
	Glob(pattern string) ([]string, error)
	Remove(path string) error
}

func (Host) Glob(pattern string) ([]string, error) { return filepath.Glob(pattern) }

func (Host) Remove(path string) error {
	if err := os.Remove(path); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}
