package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// The bridge has its own journal outside the container's writable directory.
// Its protected base and VM ownership cannot be rewritten by provider requests.
type bridgeState struct {
	Base    State
	Owned   map[string]Worker
	Deleted map[string]Worker
}

type bridge struct {
	mu      sync.Mutex
	state   bridgeState
	path    string
	backend Backend
}

type bridgeRequest struct {
	Worker Worker
	Memory int
}

func (b *bridge) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Authentication is enforced by the TLS listener before any handler executes.
	if r.URL.Path == "/snapshot" && r.Method == http.MethodGet {
		s, err := b.backend.Snapshot(r.Context())
		if err != nil {
			http.Error(w, err.Error(), 503)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(s)
		return
	}
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", 405)
		return
	}
	var q bridgeRequest
	d := json.NewDecoder(http.MaxBytesReader(w, r.Body, 16384))
	d.DisallowUnknownFields()
	if err := d.Decode(&q); err != nil {
		http.Error(w, "invalid request", 400)
		return
	}
	b.mu.Lock()
	defer b.mu.Unlock()
	if err := b.mutate(r.Context(), r.URL.Path, q); err != nil {
		http.Error(w, err.Error(), 409)
		return
	}
	w.WriteHeader(http.StatusNoContent)
}

func (b *bridge) mutate(ctx context.Context, action string, q bridgeRequest) error {
	s, err := b.backend.Snapshot(ctx)
	if err != nil {
		return err
	}
	st := clone(b.state.Base)
	for _, worker := range b.state.Owned {
		st.Workers = append(st.Workers, worker)
	}
	if err = validateSnapshot(st, s); err != nil {
		return err
	}
	if _, protected := st.Base[q.Worker.Name]; protected {
		return fmt.Errorf("protected base VM")
	}
	old, exists := b.state.Owned[q.Worker.ID]
	switch action {
	case "/add":
		if exists {
			return fmt.Errorf("creation already recorded; inspect its outcome, never create twice")
		}
		prefix := "minikube://" + st.Config.Profile + "/" + st.ClusterUID + "/"
		if !strings.HasPrefix(q.Worker.ID, prefix) || len(strings.TrimPrefix(q.Worker.ID, prefix)) != 32 || q.Worker.Phase != "creating" || q.Worker.UID != "" || q.Memory != st.Memory || len(b.state.Owned) >= st.Config.MaxWorkers {
			return fmt.Errorf("invalid creation or worker limit reached")
		}
		expected, err := nextName(s)
		if err != nil || q.Worker.Name != expected {
			return fmt.Errorf("unexpected next Minikube worker name")
		}
		if (len(s.Profile.Nodes)+1)*st.Memory > st.Config.MaxTotalMemoryMiB {
			return fmt.Errorf("host VM memory ceiling reached")
		}
		b.state.Owned[q.Worker.ID] = q.Worker
		if err = atomicJSON(b.path, b.state); err != nil {
			return err
		}
		if err = b.backend.Add(ctx, q.Worker, q.Memory); err != nil {
			return err
		}

		// Native node-add can return before the kubelet's Ready transition.
		// Capture identity as soon as it registers, then wait within the caller's
		// deadline. Never interpret successful VM creation as permission to retry.
		for {
			s, err = b.backend.Snapshot(ctx)
			if err != nil {
				return err
			}
			if n, ok := s.Nodes[expected]; ok && n.UID != "" {
				if q.Worker.UID != "" && q.Worker.UID != string(n.UID) {
					return fmt.Errorf("new worker was replaced while awaiting readiness")
				}
				q.Worker.UID = string(n.UID)
				b.state.Owned[q.Worker.ID] = q.Worker
				if err = atomicJSON(b.path, b.state); err != nil {
					return err
				}
				if nodeReady(n) {
					return nil
				}
			}
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(2 * time.Second):
			}
		}
	case "/mark", "/delete":
		// A repeated completion must never delete a later VM reusing the same name.
		if done, ok := b.state.Deleted[q.Worker.ID]; action == "/delete" && ok && done.Name == q.Worker.Name && done.UID == q.Worker.UID {
			return nil
		}
		if !exists || old.Name != q.Worker.Name || old.UID == "" || old.UID != q.Worker.UID {
			return fmt.Errorf("worker is not in the bridge ownership journal")
		}
		if action == "/mark" {
			if err = b.backend.Mark(ctx, q.Worker); err != nil {
				return err
			}
			old.Phase = "ready"
			b.state.Owned[old.ID] = old
			return atomicJSON(b.path, b.state)
		}
		if len(b.state.Owned) <= st.Config.MinWorkers {
			return fmt.Errorf("minimum worker count reached")
		}
		// Persist deletion before touching Minikube. If the host exits after a
		// completed delete, absence in both inventories proves safe completion.
		old.Phase = "deleting"
		b.state.Owned[old.ID] = old
		if err = atomicJSON(b.path, b.state); err != nil {
			return err
		}
		_, nodeExists := s.Nodes[old.Name]
		vmExists := false
		for _, node := range s.Profile.Nodes {
			vmExists = vmExists || fullName(st.Config.Profile, node.Name) == old.Name
		}
		if nodeExists || vmExists {
			if err = b.backend.Delete(ctx, q.Worker); err != nil {
				return err
			}
		}
		if b.state.Deleted == nil {
			b.state.Deleted = map[string]Worker{}
		}
		b.state.Deleted[q.Worker.ID] = old
		delete(b.state.Owned, q.Worker.ID)
		return atomicJSON(b.path, b.state)
	default:
		return fmt.Errorf("unknown bridge operation")
	}
}

type remoteBackend struct {
	client *http.Client
	url    string
}

func (b remoteBackend) request(ctx context.Context, path string, value any, result any) error {
	method := http.MethodGet
	var body []byte
	if value != nil {
		method = http.MethodPost
		body, _ = json.Marshal(value)
	}
	q, err := http.NewRequestWithContext(ctx, method, b.url+path, bytes.NewReader(body))
	if err != nil {
		return err
	}
	response, err := b.client.Do(q)
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 300 {
		data, _ := io.ReadAll(io.LimitReader(response.Body, 4096))
		return fmt.Errorf("host bridge refused %s: %s", path, data)
	}
	if result != nil {
		return json.NewDecoder(io.LimitReader(response.Body, 8<<20)).Decode(result)
	}
	return nil
}

func (b remoteBackend) Snapshot(ctx context.Context) (Snapshot, error) {
	var s Snapshot
	err := b.request(ctx, "/snapshot", nil, &s)
	return s, err
}
func (b remoteBackend) Add(ctx context.Context, w Worker, memory int) error {
	return b.request(ctx, "/add", bridgeRequest{w, memory}, nil)
}
func (b remoteBackend) Mark(ctx context.Context, w Worker) error {
	return b.request(ctx, "/mark", bridgeRequest{Worker: w}, nil)
}
func (b remoteBackend) Delete(ctx context.Context, w Worker) error {
	return b.request(ctx, "/delete", bridgeRequest{Worker: w}, nil)
}

func newRemote(dir string, c Config) (remoteBackend, error) {
	tls, err := clientTLS(filepath.Join(dir, "tls"), "bridge-client")
	if err != nil {
		return remoteBackend{}, err
	}
	return remoteBackend{url: "https://host.docker.internal:50052", client: &http.Client{
		Transport: &http.Transport{TLSClientConfig: tls}, Timeout: time.Duration(c.ProvisionTimeoutSeconds) * time.Second,
	}}, nil
}
