# Getting started

<!-- toc:start -->
**Table of contents**

- [Install](#install)
- [Python library](#python-library)
- [Choose an execution model](#choose-an-execution-model)
  - [Cooperative execution and persistence](#cooperative-execution-and-persistence)
  - [Quick start: local work](#quick-start-local-work)
  - [Quick start: Kubernetes](#quick-start-kubernetes)
- [Examples](#examples)
- [Development](#development)
<!-- toc:end -->

[Documentation](../README.md)

Run the commands below from the repository root. Choose the Kubernetes
operator for container workloads, or the local scheduler for Python work.

## Install

Supports Python 3.13 and 3.14. From this checkout, install the Python package with:

```sh
python -m pip install .
```

For local development and the plotting example, install the development group:

```sh
poetry install --with dev
```

Matplotlib is a development dependency. The Kubernetes backend also needs a
cluster, Helm, and an operator image available to that cluster. The
[Helm chart](../../charts/polyad/README.md) installs CRDs, namespace-scoped RBAC, probes,
operator replicas, and a shared Dragonfly queue with optional HA.<sup>[\[1\]](../deployment/operator.md#replicas-shared-queues-and-autoscaling)</sup>
Tool versions are pinned in
[`.tool-versions`](../../.tool-versions).

## Python library

Python applications can define and compose graphs using the library's objects.
Mypy can check their types, and cattrs can convert graph definitions to and from
dictionaries.<sup>[\[2\]](../development/toolchain.md#python-types-and-serialization)</sup>

## Choose an execution model

| Model | Use it for | Execution and observations |
| --- | --- | --- |
| Local Python scheduler | Cooperative workloads with checkpoints, runtime estimates and graph rewrites | Python workers report progress; the scheduler records events and can export diagrams and plots.<sup>[\[3\]](../../pkg/polyad/scheduling/README.md#scheduling-and-feedback)</sup><sup>[\[4\]](../../pkg/polyad/scheduling/README.md#logs-and-diagrams)</sup> |
| Kubernetes operator | Container workloads, persistent services, spot execution and graphs of graphs | Jobs, Deployments, StatefulSets and nested CRs report lifecycle and graph metrics; replicas coordinate ownership and API writes.<sup>[\[5\]](../deployment/operator.md#graph-instance-status)</sup><sup>[\[1\]](../deployment/operator.md#replicas-shared-queues-and-autoscaling)</sup> |

The local scheduler supports estimated-duration, FIFO, breadth-first and
depth-first ordering.<sup>[\[3\]](../../pkg/polyad/scheduling/README.md#scheduling-and-feedback)</sup><sup>[\[6\]](../../pkg/polyad/scheduling/README.md#graph-traversal-ordering)</sup>
Kubernetes admission follows
declared dependencies, gates and per-boundary slot reservations; Kubernetes places
the resulting Pods.<sup>[\[7\]](../deployment/operator.md#api-and-python-abstractions)</sup><sup>[\[8\]](../deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>
Both models keep graph boundaries
responsible for shutdown and cleanup.<sup>[\[9\]](../../pkg/polyad/scheduling/README.md#shutdown-conditions-and-finalizers)</sup><sup>[\[10\]](../deployment/operator.md#reconciliation-and-shutdown)</sup>

### Cooperative execution and persistence

Workloads must cooperate with pause and shutdown requests.<sup>[\[11\]](../../pkg/polyad/scheduling/README.md#cooperative-execution)</sup><sup>[\[9\]](../../pkg/polyad/scheduling/README.md#shutdown-conditions-and-finalizers)</sup>
Restarting with saved state requires application support and suitable storage;
Polyad does not automatically checkpoint or resume arbitrary containers.<sup>[\[12\]](../deployment/operator.md#workload-persistence)</sup>

On Kubernetes, `persistence.enabled: true` requires an explicit storage class and PVC.
Users choose capacity compatible with their storage and recovery requirements.
Spot placement is available on ordinary Workloads and graphs.<sup>[\[12\]](../deployment/operator.md#workload-persistence)</sup><sup>[\[13\]](../deployment/operator.md#interruptible-execution)</sup>

### Quick start: local work

```sh
poetry install --with dev
poetry run python examples/heartbeat.py
```

The [heartbeat example](../../examples/heartbeat.py) reports a one-second heartbeat while the graph grows into a
fork–join pipeline. The command prints the directory containing its plots and
event journal.<sup>[\[4\]](../../pkg/polyad/scheduling/README.md#logs-and-diagrams)</sup>

### Quick start: Kubernetes

Build and publish an image to a registry your cluster can pull from. Replace
`YOUR_REGISTRY` in these commands. Use the [Buildx setup instructions](../deployment/containers.md#production)
to prepare a builder for AMD64 and ARM64:

```sh
docker buildx build -f services/operator/Dockerfile --target production --platform linux/amd64,linux/arm64 \
  -t YOUR_REGISTRY/polyad:dev --push .
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm dependency build charts/polyad
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set operator.image.repository=YOUR_REGISTRY/polyad --set operator.image.tag=dev --wait
kubectl apply -n polyad -f examples/finite.yaml
kubectl get graphs -n polyad -o wide
```

The Python process starts Kopf on its own thread.<sup>[\[10\]](../deployment/operator.md#reconciliation-and-shutdown)</sup>
Operator replicas
share work notifications through Dragonfly and coordinate graph ownership with
Kubernetes Leases.<sup>[\[1\]](../deployment/operator.md#replicas-shared-queues-and-autoscaling)</sup>
See [deployment and lifecycle checks](../deployment/operator.md#build-install-and-exercise).

## Examples

| Example | What it demonstrates |
| --- | --- |
| [Finite pipeline](../../examples/finite.yaml) | Admit a second Job after the first completes |
| [Resources and gates](../../examples/resources-and-gates.yaml) | Create configuration, resolve generated names and gate dependent work |
| [Storage and delay](../../examples/storage-and-delay.yaml) | Require an explicit StorageClass and PVC, then delay workload admission |
| [Persistent service](../../examples/persistent.yaml) | Run a daemon with startup, readiness and liveness probes |
| [Repeated execution](../../examples/repeated-graph.yaml) | Activate a finite Graph from a persistent Graph using a timer |
| [Spot work](../../examples/spot-workload.yaml) | Run ordinary Workloads with explicit spot placement |
| [Advance capacity](../../examples/capacity.yaml) | Prewarm capacity for downstream work while preparation runs |
| [Graph composition](../../examples/polygraph.yaml) | Compose nested graph types and inspect root status rollups |
| [Traffic balancing](../../examples/traffic-balancing.yaml) | Split incoming requests between two graph replicas using optional Istio routing |

Apply examples after installing the operator. Spot examples require node labels
and tolerations that match your cluster; update their placement before applying.<sup>[\[13\]](../deployment/operator.md#interruptible-execution)</sup><sup>[\[8\]](../deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>

## Development

See the [development toolchain](../development/toolchain.md) for environment setup,
formatting, parallel tests and CI checks.
