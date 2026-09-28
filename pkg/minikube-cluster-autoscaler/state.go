package main

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	v1 "k8s.io/api/core/v1"
)

// Worker is a durable lifecycle intent. IDs survive retries and node-name reuse.
type Worker struct {
	ID    string
	Name  string
	UID   string
	Phase string
}

type State struct {
	Version    int
	Config     Config
	ClusterUID string
	Base       map[string]string
	Memory     int
	CPUs       int
	Driver     string
	Template   v1.Node
	Workers    []Worker
	LastChange time.Time
	Error      string
}

type provider struct {
	mu      sync.Mutex
	state   State
	path    string
	backend Backend
}

func atomicJSON(path string, value any) error {
	data, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(path), ".journal-*")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	if err = os.Rename(f.Name(), path); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(path))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

func lockFile(path string) (*os.File, error) {
	f, err := os.OpenFile(path, os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, err
	}
	if err = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, fmt.Errorf("another lifecycle owner holds %s", path)
	}
	return f, nil
}

func initialize(c Config, s Snapshot) (State, error) {
	st := State{Version: 1, Config: c, ClusterUID: s.ClusterUID, Base: map[string]string{}, Memory: s.Profile.Memory, CPUs: s.Profile.CPUs, Driver: s.Profile.Driver}
	if s.ClusterUID == "" || len(s.Nodes) != len(s.Profile.Nodes) {
		return st, fmt.Errorf("profile and Kubernetes inventory must agree before initialization")
	}
	if (len(s.Nodes)+c.MaxWorkers)*st.Memory > c.MaxTotalMemoryMiB {
		return st, fmt.Errorf("base plus maximum elastic VMs exceeds maxTotalMemoryMiB")
	}
	for _, entry := range s.Profile.Nodes {
		name := fullName(c.Profile, entry.Name)
		n, ok := s.Nodes[name]
		if !ok || n.UID == "" || !nodeReady(n) {
			return st, fmt.Errorf("base VM %s is missing or not Ready", name)
		}
		st.Base[name] = string(n.UID)
		if !entry.ControlPlane {
			st.Template = *n.DeepCopy()
		}
	}
	if st.Template.Name == "" {
		return st, fmt.Errorf("initialize with at least one Ready base worker for accurate allocatable resources")
	}
	return st, nil
}

func fullName(profile, short string) string {
	if short == "" {
		return profile
	}
	return profile + "-" + short
}

func nextName(s Snapshot) (string, error) {
	if len(s.Profile.Nodes) == 0 {
		return "", fmt.Errorf("empty profile")
	}
	last := s.Profile.Nodes[len(s.Profile.Nodes)-1].Name
	id := 1
	var err error
	if last != "" {
		id, err = strconv.Atoi(strings.TrimPrefix(last, "m"))
	}
	if err != nil {
		return "", fmt.Errorf("unrecognized Minikube worker suffix %q", last)
	}
	return fmt.Sprintf("%s-m%02d", s.Profile.Name, id+1), nil
}

func (s State) target() int {
	n := 0
	for _, w := range s.Workers {
		if w.Phase != "deleting" {
			n++
		}
	}
	return n
}

func (p *provider) commit(st State) error {
	if err := atomicJSON(p.path, st); err != nil {
		return err
	}
	p.state = st
	return nil
}

func clone(st State) State {
	st.Workers = append([]Worker{}, st.Workers...)
	return st
}

func (p *provider) increase(delta int) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	st := clone(p.state)
	if st.Error != "" {
		return fmt.Errorf("provider paused: %s", st.Error)
	}
	if delta <= 0 || delta > st.Config.MaxWorkers-st.target() {
		return fmt.Errorf("increase exceeds worker bounds")
	}
	for range delta {
		var id [16]byte
		if _, err := rand.Read(id[:]); err != nil {
			return err
		}
		st.Workers = append(st.Workers, Worker{ID: "minikube://" + st.Config.Profile + "/" + st.ClusterUID + "/" + hex.EncodeToString(id[:]), Phase: "queued"})
	}
	return p.commit(st)
}

func (p *provider) decrease(delta int) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	st := clone(p.state)
	if st.Error != "" || delta >= 0 || st.target()+delta < st.Config.MinWorkers {
		return fmt.Errorf("invalid target cancellation")
	}
	remaining := -delta
	kept := []Worker{}
	for _, w := range st.Workers {
		// Cancellation is only for work that has not started. Never delete a VM.
		if remaining > 0 && w.Phase == "queued" {
			remaining--
			continue
		}
		kept = append(kept, w)
	}
	if remaining != 0 {
		return fmt.Errorf("cannot cancel already provisioning or existing workers")
	}
	st.Workers = kept
	return p.commit(st)
}

func validateSnapshot(st State, s Snapshot) error {
	if st.ClusterUID != s.ClusterUID || st.Memory != s.Profile.Memory || st.CPUs != s.Profile.CPUs || st.Driver != s.Profile.Driver {
		return fmt.Errorf("cluster identity or VM sizing changed; refusing infrastructure mutations")
	}
	known := map[string]bool{}
	for name, uid := range st.Base {
		if string(s.Nodes[name].UID) != uid {
			return fmt.Errorf("protected base node %s changed or disappeared", name)
		}
		known[name] = true
	}
	for _, w := range st.Workers {
		if w.Name == "" {
			continue
		}
		known[w.Name] = true
		if w.Phase == "ready" {
			n, ok := s.Nodes[w.Name]
			if !ok || n.Spec.ProviderID != w.ID || n.Labels[poolLabel] != "elastic" {
				return fmt.Errorf("worker %s disappeared or its ownership changed", w.Name)
			}
		}
		if n, ok := s.Nodes[w.Name]; ok && w.UID != "" && string(n.UID) != w.UID {
			return fmt.Errorf("worker %s UID changed", w.Name)
		}
	}
	for _, n := range s.Profile.Nodes {
		if !known[fullName(st.Config.Profile, n.Name)] {
			return fmt.Errorf("unmanaged VM appeared; no automatic adoption")
		}
	}
	for name := range s.Nodes {
		if !known[name] {
			return fmt.Errorf("unmanaged Kubernetes node appeared")
		}
	}
	return nil
}

// reconcile executes one operation at a time, never inside a gRPC request.
func (p *provider) reconcile(ctx context.Context) error {
	p.mu.Lock()
	if p.state.Error != "" {
		p.mu.Unlock()
		return nil
	}
	st := clone(p.state)
	p.mu.Unlock()
	s, err := p.backend.Snapshot(ctx)
	if err != nil {
		return err
	}
	if err = validateSnapshot(st, s); err != nil {
		return err
	}
	if time.Since(st.LastChange) < time.Duration(st.Config.CooldownSeconds)*time.Second {
		return nil
	}

	// Finish removals before additions so pending deletion cannot deadlock a full pool.
	sort.SliceStable(st.Workers, func(i, j int) bool {
		return st.Workers[i].Phase == "deleting" && st.Workers[j].Phase != "deleting"
	})
	for _, w := range st.Workers {
		if w.Phase == "ready" {
			continue
		}
		if w.Phase == "creating" {
			return fmt.Errorf("interrupted creation for %s; inspect VM and journal before recovery", w.Name)
		}
		if w.Phase == "queued" {
			w.Name, err = nextName(s)
			if err != nil {
				return err
			}
			if _, ok := s.Nodes[w.Name]; ok {
				return fmt.Errorf("next worker name already exists")
			}
			w.Phase = "creating"
			if err = p.update(w); err != nil {
				return err
			}
			if err = p.backend.Add(ctx, w, st.Memory); err != nil {
				return err
			}
			s, err = p.backend.Snapshot(ctx)
			if err != nil {
				return err
			}
			n, ok := s.Nodes[w.Name]
			if !ok || n.UID == "" || !nodeReady(n) {
				return fmt.Errorf("new worker %s did not become Ready", w.Name)
			}
			w.UID = string(n.UID)
			if err = p.update(w); err != nil {
				return err
			}
			if err = p.backend.Mark(ctx, w); err != nil {
				return err
			}
			w.Phase = "ready"
			return p.update(w)
		}
		if w.Phase == "deleting" {
			if err = p.backend.Delete(ctx, w); err != nil {
				return err
			}
			p.mu.Lock()
			defer p.mu.Unlock()
			st = clone(p.state)
			for i, current := range st.Workers {
				if current.ID == w.ID {
					st.Workers = append(st.Workers[:i], st.Workers[i+1:]...)
					break
				}
			}
			st.LastChange = time.Now().UTC()
			return p.commit(st)
		}
	}
	return nil
}

func (p *provider) update(w Worker) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	st := clone(p.state)
	for i, old := range st.Workers {
		if old.ID == w.ID {
			st.Workers[i] = w
			st.LastChange = time.Now().UTC()
			return p.commit(st)
		}
	}
	return fmt.Errorf("worker intent was cancelled")
}

func loadState(path string, c Config) (State, error) {
	var s State
	data, err := os.ReadFile(path)
	if err != nil {
		return s, err
	}
	if err = json.Unmarshal(data, &s); err != nil {
		return s, err
	}
	if s.Version != 1 || !reflect.DeepEqual(c, s.Config) {
		return s, fmt.Errorf("configuration differs from journal; disable and explicitly migrate configuration before reuse")
	}
	return s, nil
}
