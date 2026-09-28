package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	v1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	pb "polyad.local/minikube-autoscaler/internal/protos"
)

type fakeBackend struct {
	snapshot        Snapshot
	adds, deletes   int
	addError        error
	readyAfterReads int
}

func (f *fakeBackend) Snapshot(context.Context) (Snapshot, error) {
	if f.adds > 0 && f.readyAfterReads > 0 {
		f.readyAfterReads--
		n := f.snapshot.Nodes["polyad-m03"]
		n.Status.Conditions[0].Status = v1.ConditionFalse
		if f.readyAfterReads == 0 {
			n.Status.Conditions[0].Status = v1.ConditionTrue
		}
		f.snapshot.Nodes[n.Name] = n
	}
	return f.snapshot, nil
}
func (f *fakeBackend) Add(_ context.Context, w Worker, _ int) error {
	f.adds++
	if f.addError != nil {
		return f.addError
	}
	base := f.snapshot.Nodes["polyad-m02"]
	n := base.DeepCopy()
	n.Name = w.Name
	n.UID = types.UID("uid-" + w.Name)
	f.snapshot.Nodes[w.Name] = *n
	f.snapshot.Profile.Nodes = append(f.snapshot.Profile.Nodes, struct {
		Name         string
		ControlPlane bool
	}{strings.TrimPrefix(w.Name, "polyad-"), false})
	return nil
}
func (f *fakeBackend) Mark(_ context.Context, w Worker) error {
	n := f.snapshot.Nodes[w.Name]
	n.Spec.ProviderID = w.ID
	n.Labels[poolLabel] = "elastic"
	f.snapshot.Nodes[w.Name] = n
	return nil
}
func (f *fakeBackend) Delete(_ context.Context, w Worker) error {
	f.deletes++
	delete(f.snapshot.Nodes, w.Name)
	for i, n := range f.snapshot.Profile.Nodes {
		if fullName("polyad", n.Name) == w.Name {
			f.snapshot.Profile.Nodes = append(f.snapshot.Profile.Nodes[:i], f.snapshot.Profile.Nodes[i+1:]...)
			break
		}
	}
	return nil
}

func fixture(t *testing.T) (*provider, *fakeBackend) {
	t.Helper()
	c := Config{Profile: "polyad", Namespace: "polyad", Listen: "127.0.0.1:50051", MaxWorkers: 2, MaxTotalMemoryMiB: 16384, ProvisionTimeoutSeconds: 900, CooldownSeconds: 10}
	s := Snapshot{ClusterUID: "cluster-uid", Nodes: map[string]v1.Node{}}
	s.Profile = Profile{Name: "polyad", Memory: 4096, CPUs: 2, Driver: "qemu2"}
	s.Profile.Nodes = append(s.Profile.Nodes, struct {
		Name         string
		ControlPlane bool
	}{"", true}, struct {
		Name         string
		ControlPlane bool
	}{"m02", false})
	for _, name := range []string{"polyad", "polyad-m02"} {
		s.Nodes[name] = v1.Node{ObjectMeta: metav1.ObjectMeta{Name: name, UID: types.UID("uid-" + name), Labels: map[string]string{"kubernetes.io/os": "linux", "kubernetes.io/arch": "arm64"}},
			Status: v1.NodeStatus{Capacity: v1.ResourceList{v1.ResourceCPU: resource.MustParse("2"), v1.ResourceMemory: resource.MustParse("4Gi")},
				Allocatable: v1.ResourceList{v1.ResourceCPU: resource.MustParse("1900m"), v1.ResourceMemory: resource.MustParse("3500Mi"), v1.ResourcePods: resource.MustParse("110")},
				Conditions:  []v1.NodeCondition{{Type: v1.NodeReady, Status: v1.ConditionTrue}}}}
	}
	st, err := initialize(c, s)
	if err != nil {
		t.Fatal(err)
	}
	f := &fakeBackend{snapshot: s}
	p := &provider{state: st, path: filepath.Join(t.TempDir(), "state.json"), backend: f}
	if err = p.commit(st); err != nil {
		t.Fatal(err)
	}
	return p, f
}

func TestBoundsAndCancellation(t *testing.T) {
	p, f := fixture(t)
	for _, delta := range []int{0, -1, 3} {
		if p.increase(delta) == nil {
			t.Errorf("accepted increase %d", delta)
		}
	}
	if err := p.increase(2); err != nil {
		t.Fatal(err)
	}
	if p.increase(1) == nil {
		t.Fatal("accepted over maximum")
	}
	if p.decrease(0) == nil || p.decrease(1) == nil || p.decrease(-3) == nil {
		t.Fatal("accepted invalid cancellation")
	}
	if err := p.decrease(-1); err != nil {
		t.Fatal(err)
	}
	if err := p.reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	if f.adds != 1 || p.state.Workers[0].Phase != "ready" {
		t.Fatal(p.state.Workers)
	}
	if p.decrease(-1) == nil || f.deletes != 0 {
		t.Fatal("target cancellation removed a live worker")
	}
	loaded, err := loadState(p.path, p.state.Config)
	if err != nil || loaded.target() != 1 {
		t.Fatalf("journal: %+v %v", loaded, err)
	}
}

func TestReadinessAndMemoryGuards(t *testing.T) {
	p, f := fixture(t)
	c := p.state.Config
	c.MaxTotalMemoryMiB = 12000
	if _, err := initialize(c, f.snapshot); err == nil {
		t.Fatal("accepted insufficient VM memory ceiling")
	}
	f.snapshot.Profile.Memory = 8192
	if err := validateSnapshot(p.state, f.snapshot); err == nil {
		t.Fatal("accepted resized VMs")
	}
	f.snapshot.Profile.Memory = 4096
	n := f.snapshot.Nodes["polyad"]
	n.UID = "replacement"
	f.snapshot.Nodes["polyad"] = n
	if err := validateSnapshot(p.state, f.snapshot); err == nil {
		t.Fatal("accepted replaced base node")
	}
}

func TestNoAdoptionOrBlindProvisionRetry(t *testing.T) {
	p, f := fixture(t)
	if err := p.increase(1); err != nil {
		t.Fatal(err)
	}
	f.addError = fmt.Errorf("SSH timeout")
	if err := p.reconcile(context.Background()); err == nil {
		t.Fatal("failure hidden")
	}
	p.state.LastChange = time.Time{}
	if err := p.reconcile(context.Background()); err == nil || f.adds != 1 {
		t.Fatal("retried ambiguous creation")
	}
	f.snapshot.Nodes["foreign"] = v1.Node{}
	if err := validateSnapshot(p.state, f.snapshot); err == nil {
		t.Fatal("adopted external node")
	}
}

func TestDeletionMembershipMinimumAndDuplicates(t *testing.T) {
	p, f := fixture(t)
	r := &rpcServer{p: p}
	ctx := context.Background()
	if err := p.increase(1); err != nil {
		t.Fatal(err)
	}
	if err := p.reconcile(ctx); err != nil {
		t.Fatal(err)
	}
	w := p.state.Workers[0]
	for _, nodes := range [][]*pb.ExternalGrpcNode{
		{{Name: "polyad", ProviderID: w.ID}}, {{Name: w.Name, ProviderID: "foreign"}},
		{{Name: w.Name, ProviderID: w.ID}, {Name: w.Name, ProviderID: w.ID}},
	} {
		if _, err := r.NodeGroupDeleteNodes(ctx, &pb.NodeGroupDeleteNodesRequest{Id: groupID, Nodes: nodes}); err == nil {
			t.Fatal("accepted unsafe deletion")
		}
	}
	request := &pb.NodeGroupDeleteNodesRequest{Id: groupID, Nodes: []*pb.ExternalGrpcNode{{Name: w.Name, ProviderID: w.ID}}}
	p.state.Config.MinWorkers = 1
	if _, err := r.NodeGroupDeleteNodes(ctx, request); err == nil {
		t.Fatal("violated minimum")
	}
	p.state.Config.MinWorkers = 0
	for range 2 {
		if _, err := r.NodeGroupDeleteNodes(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	if p.state.target() != 0 {
		t.Fatal("target was not committed before RPC acknowledgement")
	}
	p.state.LastChange = time.Time{}
	if err := p.reconcile(ctx); err != nil || f.deletes != 1 {
		t.Fatalf("deletion: %v", err)
	}
}

func TestUpstreamWireProtocolAndTemplate(t *testing.T) {
	p, _ := fixture(t)
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	pb.RegisterCloudProviderServer(server, &rpcServer{p: p})
	go func() { _ = server.Serve(listener) }()
	defer server.Stop()
	conn, err := grpc.NewClient("passthrough:///test", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	client := pb.NewCloudProviderClient(conn)
	groups, err := client.NodeGroups(ctx, &pb.NodeGroupsRequest{})
	if err != nil || len(groups.NodeGroups) != 1 {
		t.Fatal(groups, err)
	}
	unknown, err := client.NodeGroupForNode(ctx, &pb.NodeGroupForNodeRequest{Node: &pb.ExternalGrpcNode{Name: "polyad"}})
	if err != nil || unknown.NodeGroup.Id != "" {
		t.Fatal("base node was exposed to CA")
	}
	response, err := client.NodeGroupTemplateNodeInfo(ctx, &pb.NodeGroupTemplateNodeInfoRequest{Id: groupID})
	if err != nil {
		t.Fatal(err)
	}
	var node v1.Node
	if err = node.Unmarshal(response.NodeBytes); err != nil {
		t.Fatal(err)
	}
	if node.Status.Allocatable.Cpu().MilliValue() != 1900 || node.Labels[poolLabel] != "elastic" || len(node.Spec.Taints) != 1 {
		t.Fatalf("bad template: %+v", node)
	}
	if node.UID != "" || len(node.Status.Addresses) != 0 {
		t.Fatal("template leaked base identity")
	}
}

func TestBridgeEnforcesIndependentOwnership(t *testing.T) {
	p, f := fixture(t)
	b := &bridge{state: bridgeState{Base: p.state, Owned: map[string]Worker{}}, path: filepath.Join(t.TempDir(), "bridge.json"), backend: f}
	ctx := context.Background()
	if err := b.mutate(ctx, "/delete", bridgeRequest{Worker: Worker{ID: "x", Name: "polyad"}}); err == nil {
		t.Fatal("deleted protected base")
	}
	w := Worker{ID: "minikube://polyad/cluster-uid/0123456789abcdef0123456789abcdef", Name: "polyad-m03", Phase: "creating"}
	q := bridgeRequest{Worker: w, Memory: 4096}
	if err := b.mutate(ctx, "/add", q); err != nil {
		t.Fatal(err)
	}
	if err := b.mutate(ctx, "/add", q); err == nil || f.adds != 1 {
		t.Fatal("duplicated VM creation")
	}
	w.UID = "wrong"
	if err := b.mutate(ctx, "/mark", bridgeRequest{Worker: w}); err == nil {
		t.Fatal("accepted forged identity")
	}
	w.UID = "uid-polyad-m03"
	if err := b.mutate(ctx, "/mark", bridgeRequest{Worker: w}); err != nil {
		t.Fatal(err)
	}
	if err := b.mutate(ctx, "/delete", bridgeRequest{Worker: w}); err != nil {
		t.Fatal(err)
	}
	if err := b.mutate(ctx, "/delete", bridgeRequest{Worker: w}); err != nil || f.deletes != 1 {
		t.Fatal("delete retry was not idempotent", err)
	}
}

func TestReadyWorkerOwnershipDrift(t *testing.T) {
	p, f := fixture(t)
	if err := p.increase(1); err != nil {
		t.Fatal(err)
	}
	if err := p.reconcile(context.Background()); err != nil {
		t.Fatal(err)
	}
	w := p.state.Workers[0]
	n := f.snapshot.Nodes[w.Name]
	n.Spec.ProviderID = "foreign"
	f.snapshot.Nodes[w.Name] = n
	if validateSnapshot(p.state, f.snapshot) == nil {
		t.Fatal("accepted changed provider identity")
	}
	delete(f.snapshot.Nodes, w.Name)
	if validateSnapshot(p.state, f.snapshot) == nil {
		t.Fatal("accepted vanished ready worker")
	}
}

func TestBridgeWaitsForKubeletAfterSuccessfulNodeAdd(t *testing.T) {
	p, f := fixture(t)
	f.readyAfterReads = 2
	b := &bridge{state: bridgeState{Base: p.state, Owned: map[string]Worker{}}, path: filepath.Join(t.TempDir(), "bridge.json"), backend: f}
	w := Worker{ID: "minikube://polyad/cluster-uid/0123456789abcdef0123456789abcdef", Name: "polyad-m03", Phase: "creating"}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := b.mutate(ctx, "/add", bridgeRequest{Worker: w, Memory: 4096}); err != nil {
		t.Fatal(err)
	}
	if f.adds != 1 || f.readyAfterReads != 0 || b.state.Owned[w.ID].UID != "uid-polyad-m03" {
		t.Fatal("node-add readiness lag caused duplicate creation or lost identity")
	}
}

func TestSyntheticUnregisteredNodeMapsToItsGroup(t *testing.T) {
	p, _ := fixture(t)
	if err := p.increase(1); err != nil {
		t.Fatal(err)
	}
	w := p.state.Workers[0]
	r := &rpcServer{p: p}
	result, err := r.NodeGroupForNode(context.Background(), &pb.NodeGroupForNodeRequest{Node: &pb.ExternalGrpcNode{Name: w.ID, ProviderID: w.ID}})
	if err != nil || result.NodeGroup.Id != groupID {
		t.Fatal("CA's synthetic pending VM lost its node group", err)
	}
}

func TestTLSRejectsAnonymousAndWrongRole(t *testing.T) {
	dir := t.TempDir()
	if err := issuePKI(dir, "127.0.0.1"); err != nil {
		t.Fatal(err)
	}
	tls, err := serverTLS(filepath.Join(dir, "host/tls"), "bridge", "bridge-client")
	if err != nil {
		t.Fatal(err)
	}
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(204) }))
	server.TLS = tls
	server.StartTLS()
	defer server.Close()
	for _, spec := range []struct {
		directory, role string
		allowed         bool
	}{
		{"provider", "bridge-client", true}, {"client", "autoscaler-client", false},
	} {
		clientConfig, err := clientTLS(filepath.Join(dir, spec.directory, "tls"), spec.role)
		if err != nil {
			t.Fatal(err)
		}
		client := &http.Client{Transport: &http.Transport{TLSClientConfig: clientConfig}, Timeout: time.Second}
		response, err := client.Get(server.URL)
		if response != nil {
			response.Body.Close()
		}
		if (err == nil) != spec.allowed {
			t.Fatalf("role %s accepted=%t: %v", spec.role, err == nil, err)
		}
	}
	if response, err := server.Client().Get(server.URL); err == nil {
		response.Body.Close()
		t.Fatal("anonymous client accepted")
	}
}

func TestPersistentVolumeSafety(t *testing.T) {
	p, _ := fixture(t)
	n := template(p.state)
	local := v1.PersistentVolume{Spec: v1.PersistentVolumeSpec{PersistentVolumeSource: v1.PersistentVolumeSource{Local: &v1.LocalVolumeSource{Path: "/data"}}}}
	if !volumeFits(local, n) {
		t.Fatal("unscoped local storage allowed")
	}
	local.Spec.NodeAffinity = &v1.VolumeNodeAffinity{Required: &v1.NodeSelector{NodeSelectorTerms: []v1.NodeSelectorTerm{{MatchExpressions: []v1.NodeSelectorRequirement{
		{Key: "kubernetes.io/hostname", Operator: v1.NodeSelectorOpIn, Values: []string{n.Name}},
	}}}}}
	if !volumeFits(local, n) {
		t.Fatal("node-local PVC allowed")
	}
	local.Spec.NodeAffinity.Required.NodeSelectorTerms[0].MatchExpressions[0].Values = []string{"protected-base"}
	if volumeFits(local, n) {
		t.Fatal("unrelated base PVC blocks every worker")
	}
}

func TestConfigRejectsUnsafeInputsAndChanges(t *testing.T) {
	p, _ := fixture(t)
	path := filepath.Join(t.TempDir(), "config.json")
	for _, mutate := range []func(*Config){
		func(c *Config) { c.Profile = "../../other" }, func(c *Config) { c.Listen = "0.0.0.0:50051" },
		func(c *Config) { c.MinWorkers = -1 }, func(c *Config) { c.MaxWorkers = 17 }, func(c *Config) { c.CooldownSeconds = 0 },
	} {
		c := p.state.Config
		mutate(&c)
		data, _ := json.Marshal(c)
		if err := os.WriteFile(path, data, 0600); err != nil {
			t.Fatal(err)
		}
		if _, err := loadConfig(path); err == nil {
			t.Fatal("unsafe config accepted")
		}
	}
	c := p.state.Config
	c.MaxWorkers++
	if _, err := loadState(p.path, c); err == nil {
		t.Fatal("configuration silently changed")
	}
}
