package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	pb "polyad.local/minikube-autoscaler/internal/protos"
)

func main() {
	if err := execute(); err != nil {
		log.Fatal(err)
	}
}

func execute() error {
	mode := flag.String("mode", "check", "init, bridge, serve, check, bridge-check, maintenance-check, or resume")
	config := flag.String("config", "", "explicit addon JSON configuration")
	dir := flag.String("state-dir", "", "private state directory; required")
	flag.Parse()
	if *dir == "" || !filepath.IsAbs(*dir) {
		return fmt.Errorf("state-dir must be an absolute path")
	}
	c, err := loadConfig(*config)
	if err != nil {
		return err
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	switch *mode {
	case "init":
		return initializeAddon(ctx, *dir, c)
	case "bridge":
		return serveBridge(ctx, *dir, c)
	case "serve":
		return serveProvider(ctx, *dir, c)
	case "bridge-check":
		client, err := newRemote(filepath.Join(*dir, "provider"), c)
		if err != nil {
			return err
		}
		host, _, _ := net.SplitHostPort(c.Listen)
		client.url = "https://" + net.JoinHostPort(host, "50052")
		check, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		_, err = client.Snapshot(check)
		return err
	case "maintenance-check":
		lock, err := lockFile(filepath.Join(*dir, "host/bridge.lock"))
		if err != nil {
			return fmt.Errorf("stop the native bridge before cluster lifecycle changes: %w", err)
		}
		defer lock.Close()
		return nil
	case "check":
		tls, err := clientTLS(filepath.Join(*dir, "client/tls"), "autoscaler-client")
		if err != nil {
			return err
		}
		conn, err := grpc.NewClient(c.Listen, grpc.WithTransportCredentials(credentials.NewTLS(tls)))
		if err != nil {
			return err
		}
		defer conn.Close()
		ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
		defer cancel()
		client := pb.NewCloudProviderClient(conn)
		if _, err = client.Refresh(ctx, &pb.RefreshRequest{}, grpc.WaitForReady(true)); err != nil {
			return err
		}
		groups, err := client.NodeGroups(ctx, &pb.NodeGroupsRequest{})
		if err != nil {
			return err
		}
		if len(groups.NodeGroups) != 1 || groups.NodeGroups[0].Id != groupID {
			return fmt.Errorf("unexpected provider groups")
		}
		fmt.Println("Authenticated provider ready; base VMs are excluded from its worker group")
		return nil
	case "resume":
		// Only retry operations whose identities were already durably captured.
		hostLock, err := lockFile(filepath.Join(*dir, "host/bridge.lock"))
		if err != nil {
			return fmt.Errorf("stop the native bridge before recovery: %w", err)
		}
		defer hostLock.Close()
		lock, err := lockFile(filepath.Join(*dir, "provider/provider.lock"))
		if err != nil {
			return err
		}
		defer lock.Close()
		hostData, err := os.ReadFile(filepath.Join(*dir, "host/bridge.json"))
		if err != nil {
			return err
		}
		var hostState bridgeState
		if err = json.Unmarshal(hostData, &hostState); err != nil {
			return err
		}
		path := filepath.Join(*dir, "provider/state.json")
		st, err := loadState(path, c)
		if err != nil {
			return err
		}
		b := commands{c}
		s, err := b.Snapshot(ctx)
		if err != nil {
			return err
		}
		if err = validateSnapshot(st, s); err != nil {
			return err
		}
		for i, w := range st.Workers {
			if w.Phase == "creating" {
				// The host's independent journal is proof of ownership, not a name match.
				owned := hostState.Owned[w.ID]
				if w.UID == "" && owned.Name == w.Name && owned.UID != "" && string(s.Nodes[w.Name].UID) == owned.UID {
					w.UID = owned.UID
					st.Workers[i].UID = owned.UID
				}
				if w.UID == "" || !nodeReady(s.Nodes[w.Name]) {
					return fmt.Errorf("ambiguous creation %s; inspect host journal, do not adopt by name", w.Name)
				}
				if err = b.Mark(ctx, w); err != nil {
					return err
				}
				st.Workers[i].Phase = "ready"
				w.Phase = "ready"
				hostState.Owned[w.ID] = w
			}
		}
		if err = atomicJSON(filepath.Join(*dir, "host/bridge.json"), hostState); err != nil {
			return err
		}
		st.Error = ""
		return atomicJSON(path, st)
	default:
		return fmt.Errorf("unknown mode %q", *mode)
	}
}

func initializeAddon(ctx context.Context, dir string, c Config) error {
	if err := os.MkdirAll(dir, 0700); err != nil {
		return err
	}
	lock, err := lockFile(filepath.Join(dir, "init.lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	path := filepath.Join(dir, "provider/state.json")
	if _, err = os.Stat(path); err == nil {
		return fmt.Errorf("already initialized; existing ownership must not be recaptured")
	}
	b := commands{c}
	s, err := b.Snapshot(ctx)
	if err != nil {
		return err
	}
	st, err := initialize(c, s)
	if err != nil {
		return err
	}
	host, _, _ := net.SplitHostPort(c.Listen)
	if err = issuePKI(dir, host); err != nil {
		return err
	}
	if err = atomicJSON(filepath.Join(dir, "config.json"), c); err != nil {
		return err
	}
	if err = atomicJSON(filepath.Join(dir, "provider/config.json"), c); err != nil {
		return err
	}
	if err = atomicJSON(filepath.Join(dir, "host/bridge.json"), bridgeState{Base: st, Owned: map[string]Worker{}}); err != nil {
		return err
	}
	return atomicJSON(path, st)
}

func serveBridge(ctx context.Context, dir string, c Config) error {
	lock, err := lockFile(filepath.Join(dir, "host/bridge.lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	path := filepath.Join(dir, "host/bridge.json")
	data, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	var state bridgeState
	if err = json.Unmarshal(data, &state); err != nil {
		return err
	}
	if state.Base.Config != c {
		return fmt.Errorf("bridge configuration differs from its initialization")
	}
	tls, err := serverTLS(filepath.Join(dir, "host/tls"), "bridge", "bridge-client")
	if err != nil {
		return err
	}
	// Docker Desktop reaches the host through host.docker.internal. Mutual TLS
	// remains mandatory on this listener, which is never mounted into the container.
	server := &http.Server{Addr: ":50052", TLSConfig: tls, ReadHeaderTimeout: 5 * time.Second,
		Handler: &bridge{state: state, path: path, backend: commands{c}}}
	go func() {
		<-ctx.Done()
		shutdown, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		_ = server.Shutdown(shutdown)
	}()
	log.Print("Native VM bridge listening on :50052 with mandatory provider-client authentication")
	err = server.ListenAndServeTLS("", "")
	if err == http.ErrServerClosed {
		return nil
	}
	return err
}

func serveProvider(ctx context.Context, dir string, c Config) error {
	lock, err := lockFile(filepath.Join(dir, "provider.lock"))
	if err != nil {
		return err
	}
	defer lock.Close()
	st, err := loadState(filepath.Join(dir, "state.json"), c)
	if err != nil {
		return err
	}
	b, err := newRemote(dir, c)
	if err != nil {
		return err
	}
	check, cancel := context.WithTimeout(ctx, 30*time.Second)
	snapshot, err := b.Snapshot(check)
	cancel()
	if err != nil {
		return err
	}
	if err = validateSnapshot(st, snapshot); err != nil {
		return err
	}
	p := &provider{state: st, path: filepath.Join(dir, "state.json"), backend: b}
	tls, err := serverTLS(filepath.Join(dir, "tls"), "provider", "autoscaler-client")
	if err != nil {
		return err
	}
	server := grpc.NewServer(grpc.Creds(credentials.NewTLS(tls)))
	pb.RegisterCloudProviderServer(server, &rpcServer{p: p})
	listener, err := net.Listen("tcp", ":50051")
	if err != nil {
		return err
	}
	defer listener.Close()
	go func() { <-ctx.Done(); server.Stop() }()
	go func() {
		ticker := time.NewTicker(5 * time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
			}
			p.mu.Lock()
			missing := c.MinWorkers - p.state.target()
			paused := p.state.Error != ""
			p.mu.Unlock()
			if missing > 0 && !paused {
				if err := p.increase(missing); err != nil {
					log.Print(err)
				}
			}
			op, cancel := context.WithTimeout(ctx, time.Duration(c.ProvisionTimeoutSeconds)*time.Second)
			err := p.reconcile(op)
			cancel()
			if err != nil && ctx.Err() == nil {
				p.mu.Lock()
				st := clone(p.state)
				st.Error = err.Error()
				persist := p.commit(st)
				p.mu.Unlock()
				log.Printf("Provider paused: %v; journal: %v", err, persist)
			}
		}
	}()
	log.Print("Containerized Minikube provider serving authenticated gRPC on :50051")
	err = server.Serve(listener)
	if ctx.Err() != nil {
		return nil
	}
	return err
}
