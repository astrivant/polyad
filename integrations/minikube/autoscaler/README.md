# Minikube Cluster Autoscaler addon

<!-- toc:start -->
**Table of contents**

- [Activate](#activate)
- [Limits and ownership](#limits-and-ownership)
- [Exercise growth and shrinkage](#exercise-growth-and-shrinkage)
- [Stop, restart and recover](#stop-restart-and-recover)
<!-- toc:end -->

This optional repository-local plugin uses upstream Kubernetes Cluster Autoscaler
1.35 and an `externalgrpc` provider. It scales **native Minikube workers**, not
Docker-based Kubernetes nodes. It is not a built-in `minikube addons enable` entry.

The provider runs in a restricted Docker container on the host. A small native
Go bridge runs beside Docker because a Linux Docker Desktop container cannot
directly operate macOS QEMU/HVF. Linux uses KVM2/libvirt. Both network links require
mutual TLS with separate client identities. The container never receives the
Docker socket, kubeconfig, SSH keys, Minikube disks or certificate-authority key.

## Activate

Start an existing three-node Minikube installation first. Install the repository's
Go toolchain, Docker, Helm, kubectl, Minikube and jq, then:

```bash
bash integrations/minikube/autoscaler/autoscaler.sh build
# Review config.example.json and use a private copy if its address or limits differ.
export POLYAD_MINIKUBE_AUTOSCALER_CONFIG="$PWD/integrations/minikube/autoscaler/config.example.json"
bash integrations/minikube/autoscaler/autoscaler.sh init
bash integrations/minikube/autoscaler/autoscaler.sh bridge
```

Leave the bridge running under your existing task/process supervisor. In another
terminal, from the same checkout:

```bash
bash integrations/minikube/autoscaler/autoscaler.sh enable
bash integrations/minikube/autoscaler/autoscaler.sh test
bash integrations/minikube/autoscaler/autoscaler.sh status
```

The example binds the provider to `192.168.105.1:50051`, the usual socket_vmnet
host gateway. Verify this address on your host; Linux users must supply their
libvirt bridge address. Port 50052 serves the authenticated native bridge and
must be reachable through Docker's `host.docker.internal`. Do not publish either
port on an untrusted interface. The addon currently requires Kubernetes 1.35.x.

## Limits and ownership

`maxWorkers` counts only new elastic workers, not the captured base nodes.
The example is zero to two elastic workers and `maxTotalMemoryMiB: 20480`.
With three 4096 MiB base VMs, this allows three to five VMs in total. The ceiling
excludes host and Docker overhead. Initialization rejects an inconsistent budget.
`provisionTimeoutSeconds` bounds each slow native operation; `cooldownSeconds`
spaces infrastructure changes. Provisioning runs serially outside gRPC requests.

Initialization records the cluster UID and base-node UIDs. It labels base nodes
and pins existing Deployments/StatefulSets in `polyad` and `kube-system` to them.
These placement changes remain after disabling the addon. Install unrelated
applications outside those infrastructure namespaces. New workers are labeled
`polyad.astrivant.com/minikube-pool=elastic` and tainted
`polyad.astrivant.com/elastic=true:NoSchedule`. Minikube registers before these
labels/taints are patched, so base placement is important during that brief window.

The provider never adopts arbitrary nodes. Cluster replacement, identity drift,
ambiguous VM creation or unsafe deletion pauses it. Deletion requires the
autoscaler's cordon and completed eviction of ordinary Pods, then independently
checks node identity and local PV placement. This is necessary because Minikube's
own deletion path can force-drain Pods without honoring PDBs.

## Exercise growth and shrinkage

```bash
kubectl --context polyad create -f integrations/minikube/autoscaler/demand.yaml
kubectl --context polyad -n polyad-elastic-demo rollout status deployment/demand --timeout=16m
kubectl --context polyad get nodes -L polyad.astrivant.com/minikube-pool
kubectl --context polyad -n polyad-elastic-demo scale deployment/demand --replicas=0
```

The isolated example requests enough resources to trigger one elastic VM from
zero. To test the configured ceiling, scale this Deployment to two replicas.
After scaling it to zero, allow at least the configured five-minute idle window
before expecting removal. Pod resource requests, placement and eviction rules
drive decisions, not CPU usage directly. KEDA controls Pod counts independently.
Do not put persistent application data on disposable test workers.

## Stop, restart and recover

```bash
bash integrations/minikube/autoscaler/autoscaler.sh disable
# Stop the foreground bridge with Ctrl-C.
```

Disabling removes only the autoscaler release and host provider container. It
retains VMs, journals, credentials and base placement. This is not a scale-to-zero
command. Let the autoscaler drain idle test workers before disabling if desired.

Before `minikube.sh start`, `recover` or `stop`, disable the addon and stop the
bridge. The integration preserves the current dynamic node count, not a fixed
three-node target. Deleting the cluster is refused while its ownership journal
exists; archive the stopped addon's private state only after resolving all worker
intents. Never reuse journals with a newly created cluster of the same name.

Inspect `.cache/minikube/autoscaler/<profile>/provider/state.json` on errors.
After repairing the reported cause, with both bridge and container stopped, use
`autoscaler.sh resume`, then restart the bridge and enable the addon. Recovery
never blindly retries ambiguous creation or adopts a VM by name alone. Certificate
validity is one year; this local prototype does not perform automatic key rotation.

Implementation: [Go package](../../../pkg/minikube-cluster-autoscaler/) and
[container](../../../services/minikube-cluster-autoscaler/).
