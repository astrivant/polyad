# Getting started

[Documentation](README.md)

Run the commands below from the repository root. Choose the Kubernetes
operator for container workloads, or the local scheduler for Python work.

## Install

Requires Python 3.13+. From this checkout, install the Python package with:

```sh
python -m pip install .
```

For local development and the plotting example, install the development group:

```sh
poetry install --with dev
```

Matplotlib is a development dependency. The Kubernetes backend also needs a
cluster, Helm, and an operator image available to that cluster. The
[Helm chart](../charts/polyad/README.md) installs CRDs, namespace-scoped RBAC, probes,
operator replicas, and a shared Dragonfly queue with optional HA.<sup>[\[1\]](operator.md#replicas-shared-queues-and-autoscaling)</sup>
Tool versions are pinned in
[`.tool-versions`](../.tool-versions).

## Python library

Python applications can define and compose graphs using the library's objects.
Mypy can check their types, and cattrs can convert graph definitions to and from
dictionaries.<sup>[\[2\]](toolchain.md#python-types-and-serialization)</sup>

## Choose an execution model

| Model | Use it for | Execution and observations |
| --- | --- | --- |
| Local Python scheduler | Cooperative workloads with checkpoints, runtime estimates and graph rewrites | Python workers report progress; the scheduler records events and can export diagrams and plots.<sup>[\[3\]](../pkg/polyad/balance/README.md#scheduling-and-feedback)</sup><sup>[\[4\]](../pkg/polyad/balance/README.md#logs-and-diagrams)</sup> |
| Kubernetes operator | Container workloads, persistent services, spot execution and graphs of graphs | Jobs, Deployments, StatefulSets and nested CRs report lifecycle and graph metrics; replicas coordinate ownership and API writes.<sup>[\[5\]](operator.md#graph-instance-status)</sup><sup>[\[1\]](operator.md#replicas-shared-queues-and-autoscaling)</sup> |

The local scheduler supports estimated-duration, FIFO, breadth-first and
depth-first ordering.<sup>[\[3\]](../pkg/polyad/balance/README.md#scheduling-and-feedback)</sup><sup>[\[6\]](../pkg/polyad/balance/README.md#graph-traversal-ordering)</sup>
Kubernetes admission follows
declared dependencies, gates and per-boundary slot reservations; Kubernetes places
the resulting Pods.<sup>[\[7\]](operator.md#api-and-python-abstractions)</sup><sup>[\[8\]](operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>
Both models keep graph boundaries
responsible for shutdown and cleanup.<sup>[\[9\]](../pkg/polyad/balance/README.md#shutdown-conditions-and-finalizers)</sup><sup>[\[10\]](operator.md#reconciliation-and-shutdown)</sup>

### Cooperative execution and persistence

Workloads must cooperate with pause and shutdown requests.<sup>[\[11\]](../pkg/polyad/balance/README.md#cooperative-execution)</sup><sup>[\[9\]](../pkg/polyad/balance/README.md#shutdown-conditions-and-finalizers)</sup>
Restarting with saved state requires application support and suitable storage;
Polyad does not automatically checkpoint or resume arbitrary containers.<sup>[\[12\]](operator.md#workload-persistence)</sup>

On Kubernetes, persistent workloads require an explicit storage class and PVC.
Use non-spot capacity: persistent storage declarations are invalid under
`Ephemeral` workloads. Ordinary graphs leave storage policy to their users.<sup>[\[12\]](operator.md#workload-persistence)</sup><sup>[\[13\]](operator.md#ephemeral-execution)</sup>

### Quick start: local work

```sh
poetry install --with dev
poetry run python examples/heartbeat.py
```

The [heartbeat example](../examples/heartbeat.py) reports a one-second heartbeat while the graph grows into a
fork–join pipeline. The command prints the directory containing its plots and
event journal.<sup>[\[4\]](../pkg/polyad/balance/README.md#logs-and-diagrams)</sup>

### Quick start: Kubernetes

Build and publish an image to a registry your cluster can pull from. Replace
`YOUR_REGISTRY` in these commands:

```sh
docker build --target production -t YOUR_REGISTRY/polyad:dev .
docker push YOUR_REGISTRY/polyad:dev
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm dependency build charts/polyad
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set operator.image.repository=YOUR_REGISTRY/polyad --set operator.image.tag=dev --wait
kubectl apply -n polyad -f examples/finite.yaml
kubectl get graphs -n polyad -o wide
```

The Python process starts Kopf on its own thread.<sup>[\[10\]](operator.md#reconciliation-and-shutdown)</sup>
Operator replicas
share work notifications through Dragonfly and coordinate graph ownership with
Kubernetes Leases.<sup>[\[1\]](operator.md#replicas-shared-queues-and-autoscaling)</sup>
See [deployment and lifecycle checks](operator.md#build-install-and-exercise).

## Examples

| Example | What it demonstrates |
| --- | --- |
| [Finite pipeline](../examples/finite.yaml) | Admit a second Job after the first completes |
| [Resources and gates](../examples/resources-and-gates.yaml) | Create configuration, resolve generated names and gate dependent work |
| [Storage and delay](../examples/storage-and-delay.yaml) | Require an explicit StorageClass and PVC, then delay workload admission |
| [Persistent service](../examples/persistent.yaml) | Run a daemon with startup, readiness and liveness probes |
| [Repeated execution](../examples/repeated-graph.yaml) | Activate a finite Graph from a persistent Graph using a timer |
| [Spot work](../examples/ephemeral.yaml) | Apply explicit spot placement to an ephemeral graph |
| [Advance capacity](../examples/capacity.yaml) | Prewarm capacity for downstream work while preparation runs |
| [Graph composition](../examples/polygraph.yaml) | Compose nested graph types and inspect root status rollups |

Apply examples after installing the operator. Spot examples require node labels
and tolerations that match your cluster; update their placement before applying.<sup>[\[13\]](operator.md#ephemeral-execution)</sup><sup>[\[8\]](operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>

## Development

See the [development toolchain](toolchain.md) for environment setup,
formatting, parallel tests and CI checks.
