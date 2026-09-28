# Full local HA lab

<!-- toc:start -->
**Table of contents**

- [Enabled features](#enabled-features)
- [Resource ceiling and node autoscaling](#resource-ceiling-and-node-autoscaling)
- [Minikube kernel compatibility](#minikube-kernel-compatibility)
- [Access and credentials](#access-and-credentials)
<!-- toc:end -->

From an existing `integrations/minikube/minikube.sh start` installation:

```bash
bash integrations/minikube/minikube.sh full
bash integrations/minikube/full/full.sh test
```

This opt-in profile reuses the local operator image and persistent cache. It
installs a separate `polyad-lab` prerequisite release before upgrading `polyad`.
KEDA APIs are installed before its admission webhook and scaling targets.
Once selected, ordinary `minikube.sh enable` and `test` retain this profile.
Namespace `polyad` is intentional: service addresses and mesh identities in these
reference values are scoped to this namespace. This is not a general-purpose chart.

## Enabled features

| Component | Local configuration |
| --- | --- |
| Polyad | Dense HA, initially two replicas, KEDA range 2-3, local root PolyGraph |
| Dragonfly | Two replicated instances, connection-driven scaling up to three |
| PostgreSQL | CloudNativePG, three instances, synchronous HA, encrypted application records |
| APIs | Authenticated composition, events, WebSockets, discovery, metrics and temporary connections |
| Mesh | Istio control plane and ingress, mTLS, authorization, audit and dry-run policy experiments |
| Observers | Two read-only replicas with sidecar injection and dedicated authentication |
| Adaptation | VPA compatibility, capacity API, AdaptivePID Cheeger reduction with certificate-gap feedback |
| Telemetry | Two Alloy collectors plus local Prometheus, Loki and Tempo backends |

Istio supports these native VMs. Mesh and process HA do not make the single
Kubernetes control-plane VM highly available. The telemetry backends, VPA
controllers and local-path volumes are deliberately not HA. No Grafana is installed.
The pinned Minikube kindnet includes nftables-based NetworkPolicy enforcement,
including Dragonfly's operator-managed replication policy. The optional Polyad
API NetworkPolicies still require explicit administrator ingress CIDRs; this
profile does not invent trusted external networks. Mesh authorization is not a
substitute for NetworkPolicy. Remote federation, external secret providers and
external JWT identities still require real endpoints and administrator credentials.
Mutually exclusive modes, such as Gateway API versus legacy Istio ingress, are
not enabled simultaneously.

VPA is installed and workloads may opt in with their own bounded policies; this
does not automatically resize every workload. Avoid having VPA and CPU-utilization
HPA control the same resource request without an explicit coordination policy.
ProvisioningRequest support requires the optional Cluster Autoscaler addon to
process requests; merely installing its API is not a capacity guarantee.

## Resource ceiling and node autoscaling

Each VM uses 4 GiB. Three base VMs consume 12 GiB. The separate
[autoscaler addon](../autoscaler/README.md) example permits at most two additional
workers, giving a **20 GiB total VM ceiling**. This excludes Docker Desktop and
macOS overhead. Scaling Pods with KEDA does not itself change this VM ceiling.

Operator and observer Pods use the protected worker VMs, reserving
control-plane memory for the API server and etcd. A 4 GiB Minikube VM also spends
memory on its ISO's tmpfs root, so nominal RAM is not all available to workloads.
Collectors retain their existing node-bound PVC placement.

The HA upgrade does not silently enable host infrastructure mutation. Activate
the addon explicitly, then opt application workloads into its elastic pool.
Base VMs and infrastructure volumes are never candidates for addon deletion.

## Minikube kernel compatibility

The Minikube 1.39 VM ISO's Linux 6.6 kernel omits
`CONFIG_NETFILTER_NETLINK_GLUE_CT`. Its bundled kindnet can enforce policies,
but cannot record NFQUEUE's accepted-connection label. This can incorrectly
reject replies to permitted connections, including Dragonfly replication.

The lab's explicit `kindnetCompat.enabled` option installs a host-network
DaemonSet with only `NET_ADMIN`, no Kubernetes credentials and no host filesystem
mount. It maintains only the `polyad_kindnet_compat` nftables table. Hook priority
96 runs after kindnet's policy decision at 95 and before source NAT at 100,
recording label bit 28 on accepted new connections. Dropped packets never reach
the rule. It does not add an allow-all policy or disable enforcement. The pinned
image, hook priority and label are a compatibility contract, checked at runtime.

Normal Helm removal terminates the service and removes its table. Force-killing
a Pod may skip cleanup; inspect/remove only this named table before switching
CNIs. Disable the option when using a kernel/CNI that does not need it. This is a
local lab workaround, not a general-purpose production firewall component.

The live verification checks both an allowed cross-node replica connection and
a denied admin-port connection from an unrelated, non-mesh Pod. An Istio Pod's
successful TCP `connect()` alone does not prove upstream access: Envoy accepts
the local connection first.

## Access and credentials

Generated credentials live in Kubernetes Secrets and in private, ignored
`.cache/minikube/full/<profile>/` files. Existing Secrets are preserved; do not
commit this directory or paste its contents into logs. The self-signed
`polyad.local` ingress certificate lasts 30 days and must be renewed explicitly.

```bash
kubectl --context polyad -n polyad port-forward service/polyad 8443:443
# In another terminal, use https://polyad.local:8443 with DNS/hosts mapping to
# 127.0.0.1, the generated CA certificate, and the API's own bearer credential.
kubectl --context polyad -n polyad port-forward service/polyad-lab-prometheus 9090:9090
```

Do not expose the unauthenticated local telemetry backend ports to a public network.
The full test waits for database and replication readiness, checks the root,
and runs a fresh application Graph. A Helm release marked deployed is not enough.

Values are in [values.yaml](values.yaml); prerequisite settings are in
[chart/values.yaml](chart/values.yaml). Application feature semantics remain in
the canonical [chart references](../../../charts/polyad/references/).

The vendored ProvisioningRequest CRD comes from the official Kubernetes
[Cluster Autoscaler 1.35.0 source](https://github.com/kubernetes/autoscaler/blob/cluster-autoscaler-1.35.0/cluster-autoscaler/apis/config/crd/autoscaling.x-k8s.io_provisioningrequests.yaml).
Its Apache 2.0 license is retained in `chart/LICENSE.kubernetes`.
