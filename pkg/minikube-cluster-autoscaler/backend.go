package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"runtime"
	"strconv"
	"strings"
	"time"

	v1 "k8s.io/api/core/v1"
	"k8s.io/component-helpers/scheduling/corev1/nodeaffinity"
)

// Profile contains only the Minikube settings that constrain this worker group.
type Profile struct {
	Name    string
	Driver  string
	Network string
	Memory  int
	CPUs    int
	Nodes   []struct {
		Name         string
		ControlPlane bool
	}
	KubernetesConfig struct{ KubernetesVersion string }
}

type Snapshot struct {
	Profile    Profile
	ClusterUID string
	Nodes      map[string]v1.Node
}

// Backend keeps infrastructure commands replaceable by deterministic test doubles.
type Backend interface {
	Snapshot(context.Context) (Snapshot, error)
	Add(context.Context, Worker, int) error
	Mark(context.Context, Worker) error
	Delete(context.Context, Worker) error
}

type commands struct{ config Config }

func run(ctx context.Context, input []byte, env []string, executable string, args ...string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, executable, args...)
	cmd.Stdin = bytes.NewReader(input)
	cmd.Env = append(os.Environ(), env...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	data, err := cmd.Output()
	if err != nil {
		return nil, fmt.Errorf("%s failed: %w: %s", executable, err, stderr.String())
	}
	return data, nil
}

func (b commands) kube(ctx context.Context, input []byte, args ...string) ([]byte, error) {
	return run(ctx, input, nil, "kubectl", append([]string{"--context", b.config.Profile, "--request-timeout=20s"}, args...)...)
}

func (b commands) Snapshot(ctx context.Context) (Snapshot, error) {
	s := Snapshot{Nodes: map[string]v1.Node{}}
	data, err := run(ctx, nil, nil, "minikube", "--profile", b.config.Profile, "profile", "list", "--light", "--output=json")
	if err != nil {
		return s, err
	}
	var profiles struct {
		Valid []struct {
			Name   string
			Config Profile
		}
	}
	if err = json.Unmarshal(data, &profiles); err != nil {
		return s, err
	}
	for _, p := range profiles.Valid {
		if p.Name == b.config.Profile {
			s.Profile = p.Config
		}
	}
	if s.Profile.Name != b.config.Profile {
		return s, fmt.Errorf("selected VM profile is missing")
	}
	expected := "kvm2"
	if runtime.GOOS == "darwin" {
		expected = "qemu2"
	}
	if s.Profile.Driver != expected || (expected == "qemu2" && s.Profile.Network != "socket_vmnet") {
		return s, fmt.Errorf("requires native %s VMs and socket_vmnet on macOS", expected)
	}
	if !strings.HasPrefix(s.Profile.KubernetesConfig.KubernetesVersion, "v1.35.") {
		return s, fmt.Errorf("this addon pins Cluster Autoscaler 1.35; Kubernetes must be 1.35.x")
	}
	controlPlanes := 0
	for _, n := range s.Profile.Nodes {
		if n.ControlPlane {
			controlPlanes++
		}
	}
	if controlPlanes != 1 || s.Profile.Memory < 1 || s.Profile.CPUs < 1 {
		return s, fmt.Errorf("requires exactly one control plane and positive VM sizing")
	}
	data, err = b.kube(ctx, nil, "get", "namespace", "kube-system", "-o", "json")
	if err != nil {
		return s, err
	}
	var namespace v1.Namespace
	if err = json.Unmarshal(data, &namespace); err != nil {
		return s, err
	}
	s.ClusterUID = string(namespace.UID)
	data, err = b.kube(ctx, nil, "get", "nodes", "-o", "json")
	if err != nil {
		return s, err
	}
	var nodes v1.NodeList
	if err = json.Unmarshal(data, &nodes); err != nil {
		return s, err
	}
	for _, n := range nodes.Items {
		s.Nodes[n.Name] = n
	}
	return s, nil
}

func (b commands) Add(ctx context.Context, _ Worker, memory int) error {
	// Minikube owns naming and VM metadata; never call QEMU or virsh directly.
	_, err := run(ctx, nil, []string{"MINIKUBE_MEMORY=" + strconv.Itoa(memory), "MINIKUBE_NATIVE_SSH=true"}, "minikube",
		"--profile", b.config.Profile, "node", "add", "--worker", "--control-plane=false")
	return err
}

func (b commands) Mark(ctx context.Context, w Worker) error {
	// UID preconditions prevent assigning ownership to a replacement with the same name.
	patch, _ := json.Marshal([]map[string]any{
		{"op": "test", "path": "/metadata/uid", "value": w.UID},
		{"op": "add", "path": "/spec/providerID", "value": w.ID},
	})
	if _, err := b.kube(ctx, nil, "patch", "node", w.Name, "--type=json", "-p", string(patch)); err != nil {
		return err
	}
	if _, err := b.kube(ctx, nil, "label", "node", w.Name, poolLabel+"=elastic", "--overwrite"); err != nil {
		return err
	}
	_, err := b.kube(ctx, nil, "taint", "node", w.Name, elasticTaint+"=true:NoSchedule", "--overwrite")
	return err
}

func (b commands) Delete(ctx context.Context, w Worker) error {
	data, err := b.kube(ctx, nil, "get", "node", w.Name, "-o", "json")
	if err != nil {
		return err
	}
	var node v1.Node
	if err = json.Unmarshal(data, &node); err != nil {
		return err
	}
	if string(node.UID) != w.UID || node.Spec.ProviderID != w.ID || node.Labels[poolLabel] != "elastic" {
		return fmt.Errorf("refusing deletion: node ownership changed")
	}
	cordoned := node.Spec.Unschedulable
	for _, t := range node.Spec.Taints {
		if t.Key == "ToBeDeletedByClusterAutoscaler" && t.Effect == v1.TaintEffectNoSchedule {
			cordoned = true
		}
	}
	if !cordoned {
		return fmt.Errorf("refusing deletion: Cluster Autoscaler has not cordoned the worker")
	}

	// Minikube's delete internally force-drains without eviction. Require CA to
	// finish its PDB-aware drain first, including termination of ordinary Pods.
	data, err = b.kube(ctx, nil, "get", "pods", "--all-namespaces", "--field-selector", "spec.nodeName="+w.Name, "-o", "json")
	if err != nil {
		return err
	}
	var pods v1.PodList
	if err = json.Unmarshal(data, &pods); err != nil {
		return err
	}
	for _, pod := range pods.Items {
		daemon := false
		for _, owner := range pod.OwnerReferences {
			if owner.Kind == "DaemonSet" && owner.Controller != nil && *owner.Controller {
				daemon = true
			}
		}
		if !daemon {
			return fmt.Errorf("refusing deletion: Pod %s/%s remains", pod.Namespace, pod.Name)
		}
		for _, volume := range pod.Spec.Volumes {
			if volume.PersistentVolumeClaim != nil {
				return fmt.Errorf("refusing deletion: DaemonSet has persistent storage")
			}
		}
	}
	data, err = b.kube(ctx, nil, "get", "pv", "-o", "json")
	if err != nil {
		return err
	}
	var volumes v1.PersistentVolumeList
	if err = json.Unmarshal(data, &volumes); err != nil {
		return err
	}
	for _, pv := range volumes.Items {
		// Local PVs can outlive their Pods. Evaluate placement, not just live mounts.
		if volumeFits(pv, node) {
			return fmt.Errorf("refusing deletion: persistent volume %s can be tied to this worker", pv.Name)
		}
	}
	_, err = run(ctx, nil, nil, "minikube", "--profile", b.config.Profile, "node", "delete", w.Name)
	return err
}

func nodeReady(n v1.Node) bool {
	for _, c := range n.Status.Conditions {
		if c.Type == v1.NodeReady {
			return c.Status == v1.ConditionTrue
		}
	}
	return false
}

func volumeFits(pv v1.PersistentVolume, node v1.Node) bool {
	if pv.Spec.NodeAffinity == nil || pv.Spec.NodeAffinity.Required == nil {
		// An unscoped local volume is unsafe on every disposable worker.
		return pv.Spec.Local != nil || pv.Spec.HostPath != nil
	}
	selector, err := nodeaffinity.NewNodeSelector(pv.Spec.NodeAffinity.Required)
	return err != nil || selector.Match(&node)
}

func withTimeout() (context.Context, context.CancelFunc) {
	return context.WithTimeout(context.Background(), 45*time.Second)
}
