# Pod context and health binding

<!-- toc:start -->
**Table of contents**

- [Health checks](#health-checks)
- [Environment variables](#environment-variables)
- [Placement and lifecycle](#placement-and-lifecycle)
- [Deployment coverage](#deployment-coverage)
<!-- toc:end -->

Polyad's health listener binds to its primary Pod IP. Kubernetes supplies that IP
and the Pod's execution context through the Downward API, independently of trace
or log export. Operator Pods and graph-managed workloads receive the same context
selectors; each kubelet resolves them for its own containers.

## Health checks

The operator reads `POLYAD_POD_IP` from `status.podIP` and starts Kopf's existing
health listener at `http://POD_IP:8080/healthz`. IPv6 addresses are bracketed in
URLs. Wildcard addresses and overrides to a different address are rejected.
A Kubernetes process without the Pod-IP projection fails startup. Outside
Kubernetes, an unset Pod IP selects `127.0.0.1` for local development.

Startup and liveness HTTP probes omit `httpGet.host`, so Kubernetes targets the
Pod IP. Readiness queries that same address and also checks scheduler startup,
worker health, API/cache freshness, root attachment and connection draining. Probe
requests bypass environment-configured HTTP proxies. This uses the existing
Kopf server and creates no additional Flask application or server thread.

Inspect a replica using the same probe command:

```sh
kubectl -n polyad exec deployment/polyad-polyad -c operator -- \
  python -m polyad.operator.lifecycle.probes
kubectl -n polyad exec deployment/polyad-polyad -c operator -- \
  python -m polyad.operator.lifecycle.probes --ready
```

Health diagnostics use `exec` because a loopback-based port-forward does not reach
the Pod-only listener. API Services retain their existing listeners. Observers
continue using TCP probes against their existing observations listener.

For standalone processes, `--liveness` can select another port/path on the same
address. Pass the matching URL to `polyad.operator.lifecycle.probes --url URL`
when customizing a Docker health check.

## Environment variables

| Variable | Kubernetes source |
| --- | --- |
| `POLYAD_POD_NAME` | `metadata.name` |
| `POLYAD_POD_UID` | `metadata.uid` |
| `POLYAD_POD_NAMESPACE` | `metadata.namespace` |
| `POLYAD_POD_IP`, `POLYAD_POD_IPS` | `status.podIP`, `status.podIPs` |
| `POLYAD_HOST_IP`, `POLYAD_HOST_IPS` | `status.hostIP`, `status.hostIPs` |
| `POLYAD_KUBERNETES_NODE_NAME` | `spec.nodeName` |
| `POLYAD_SERVICE_ACCOUNT_NAME` | `spec.serviceAccountName` |
| `POLYAD_CPU_REQUEST_MILLICORES`, `POLYAD_CPU_LIMIT_MILLICORES` | Current container's `requests.cpu`, `limits.cpu`, divisor `1m` |
| `POLYAD_MEMORY_REQUEST_BYTES`, `POLYAD_MEMORY_LIMIT_BYTES` | Current container's `requests.memory`, `limits.memory`, divisor `1` |

Values are strings. Resource selectors refer to each container, including init
containers and native sidecars. Kubernetes may supply node allocatable resources
for an omitted CPU/memory limit; that value does not mean a container limit was
configured. Environment values are startup snapshots, including during in-place
resource resize. See the [Downward API field contract](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/).

Operators also receive `POLYAD_POD_CLUSTER` from the chart's local cluster name.
Root-provisioned workers use their destination cluster name. This describes where
the process runs; `POLYAD_CLUSTER_NAME` can identify the root it coordinates with.

## Placement and lifecycle

Node name and host addresses describe actual placement. Node selectors,
affinities and tolerations describe scheduling constraints and cannot establish
which node was selected. Region, zone and node labels are not automatically
projected through these environment variables. Applications needing them can use
an explicitly authorized Node lookup or administrator-projected Pod metadata.
No extra Node API permissions are granted for this context.

`POLYAD_NODE_NAME` remains the logical graph vertex. The distinct
`POLYAD_KUBERNETES_NODE_NAME` identifies the Kubernetes host and supplies the
standard `k8s.node.name` OpenTelemetry resource attribute for enabled logs/traces.

## Deployment coverage

The shared Helm helper covers singular and HA operators, distributed component
Daemon templates, observers and Helm-installed downstream workers. The root
reconciler applies selectors again when provisioning remote Deployment or
DaemonSet workers, so each worker resolves its own destination Pod/node state.

The compiler injects the same selectors into application containers, init
containers and native sidecars for Jobs, Deployments and StatefulSets. Context
names are reserved and override conflicting explicit entries; other environment
values and Secret references remain intact. These template changes follow normal
rollout rules. See [workload identities](../workloads/workload-environment.md) for
graph ancestry, endpoint discovery and execution context.
