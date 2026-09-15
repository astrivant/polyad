# Persistent graphs and Kubernetes execution

## Daemons change the graph's contract

A job produces a terminal result. A daemon maintains a capability over time. Its success condition is a temporal invariant, such as “accept requests while healthy,” with an explicit stop condition. A persistent graph is therefore a supervised system rather than a computation with an expected return value.

Use two relations over the same vertices:

- **Admission edges** require `started`, `ready`, or `completed`. These must form a DAG. A completion edge from a daemon is invalid.
- **Data-flow connections** describe communication. These may cycle, including self-loops. They do not block admission or create network transport automatically.

For a data-flow graph, contract each strongly connected component into one vertex. The resulting condensation graph is a DAG: each cyclic component can become a supervised boundary with its own capacity, readiness and shutdown contracts. Polyad currently requires you to declare these boundaries; it does not infer or deploy SCCs automatically.

There are two useful interpretations of a cycle:

1. **Concurrent stream processing:** start all participants, then exchange messages. The cycle needs buffering, backpressure, and usually an initial token or independent producer. All participants waiting for each other's readiness is a startup deadlock; keep that relation acyclic.
2. **Recurrence:** run a finite graph in epochs, advancing durable state between iterations. `Feedback` implements this interpretation, with optional finite `rounds` or indefinite recurrence.

For a recurrence `x[t+1] = F(x[t], input[t])`, a fixed point satisfies `F(x*, input*) = x*`. Existence does not imply convergence. A contraction provides convergence; a linear autonomous recurrence converges to zero when its matrix has spectral radius below one. Stream stability instead depends on arrival/service rates, queues, and feedback gain. These mathematical properties are application contracts, not conclusions the operator can draw from connectivity alone.

Useful temporal properties include “a stopped boundary never admits new work,” “an admitted request eventually receives service,” and “ownership is released only after cleanup.” Readiness may become false again; completion is a terminal observation for a particular execution. Daemon scheduling therefore needs reservations and fairness, not an artificial infinite duration fed into shortest-remaining-work scheduling.

## API and Python abstractions

All CRDs are namespaced under `polyad.astrivant.com/v1alpha1` and shipped in `charts/polyad/crds`. These are alpha APIs.

| Python abstraction | Kubernetes representation |
| --- | --- |
| `Work`, `Workload`, command `Operation` | `Workload` definition plus a graph `Node`; execution uses a Job and explicit container command |
| Persistent node | `Daemon` definition, executed as a Deployment with application probes |
| `Ephemeral` | Spot-placed, restartable Job definition |
| `Graph`, `Topology` | `Graph` CR; nodes reference definitions and can contain nested boundaries |
| `Placement` on `Topology` / `Graph` | Shared node-group or labeled resource-slice placement, inherited by descendant pods |
| `EphemeralGraph` | Graph with required placement for interruptible capacity |
| `Feedback` | `Feedback` CR with a finite graph body and optional round limit |
| `Gate` | `Gate` CR containing the library's Boolean expression; signals are `NODE.ready`, `NODE.started`, `NODE.completed`, `NODE.failed` |
| `ShutdownContract` | `ShutdownPolicy` CR with runtime limit and pod termination grace period |
| `Finalizer` | Operator `polyad.astrivant.com/drain` finalizer and application/controller-owned Kubernetes finalizers on descendants |
| `Rewrite`, `RewriteRegistry` | One-shot `Rewrite` CR naming a graph, expected generation, and full replacement topology |
| Resource ownership | `Resource` definition for Service, ConfigMap, or PVC |
| `Control`, `Outcome`, `Estimate`, `Statistics`, scheduling policies | Runtime values/contracts, not independent cluster objects; Kubernetes execution reports lifecycle in graph status |

Existing local `Scheduler`, `Graph`, and bounded `Feedback` retain their cooperative Python behavior. The Kubernetes controller is a separate execution backend. Python callables, factories, `ProcessOwner` objects, checkpoint callbacks and arbitrary finalizer functions are not serialized or executed from CRs. Package application code into containers. Container probes, signal handlers and external checkpoint storage provide the remote lifecycle contract. Admission is deterministic inventory order with `slots` reservations per boundary; Kubernetes schedules actual CPU/memory requests. Local shortest-remaining/FIFO/BFS/DFS policies are not remotely executed by this backend.

The new descriptors can also build the graph spec from Python:

```python
from polyad.graph import Ephemeral, EphemeralGraph, Placement
from polyad.graph.topology import converter

boundary = EphemeralGraph(
    nodes=(Ephemeral(name="worker", ref="spot-worker"),),
    placement=Placement(nodeSelector={"polyad.astrivant.com/capacity": "spot"}),
)
cr = {
    "apiVersion": "polyad.astrivant.com/v1alpha1",
    "kind": "EphemeralGraph",
    "metadata": {"name": "spot-pipeline"},
    "spec": converter.unstructure(boundary),
}
```

Definitions (`Workload`, `Daemon`, `Ephemeral`, `Resource`, `Gate`, `ShutdownPolicy`) do not launch work by themselves. A Graph references them by name in the same namespace. Nested Graph/EphemeralGraph/Feedback definitions must set `templateOnly: true`; the parent instantiates an owned copy with that flag cleared. Recursive references and nesting deeper than 32 are rejected. A finite parent must reference finite children if it expects completion.

`connections` record topology; applications configure transport. Container/resource string fields can use `${nodes.NAME.name}` to refer to the generated Kubernetes resource name of another node, for example a PVC's `claimName` or a Service hostname. Names stay stable across resource replacement. Resources with probes, finalizers or readiness requirements should be dependencies before consumers start. A Service still needs an explicit selector; applications can use their own pod labels in templates.

## Scheduling a graph onto a resource slice

Every `Graph` can set `spec.placement`; placement is independent of finite, persistent, or ephemeral lifetime. [Complete example](../examples/graph-placement.yaml):

```yaml
spec:
  placement:
    nodeSelector:
      workloads.example.com/pool: batch
    tolerations:
      - key: workloads.example.com/dedicated
        operator: Equal
        value: batch
        effect: NoSchedule
  nodes:
    - name: worker
      kind: Workload
      ref: hello
```

Use Kubernetes node labels for node groups, tenant partitions, GPU pools, zones, disk tiers, or other tagged slices. Cloud tags must first be reflected in Kubernetes labels. `nodeAffinity` supports required label expressions and weighted preferences. Tolerations permit the graph's pods to use matching tainted nodes; selectors and required affinity determine the target slice. See [Kubernetes node placement](https://kubernetes.io/docs/concepts/scheduling-eviction/assign-pod-node/) and [taints and tolerations](https://kubernetes.io/docs/concepts/scheduling-eviction/taint-and-toleration/).

The operator propagates placement through nested graphs and feedback epochs into Jobs and daemon Deployments. Workload/Daemon/Ephemeral definitions and pod templates can add constraints. Selectors must agree on shared keys. Required affinities are intersected (including the Cartesian product of OR alternatives), preferred affinities are combined, and tolerations are deduplicated. Existing pod affinity/anti-affinity is preserved. Contradictory affinity predicates leave pods unschedulable; conflicting exact selectors are rejected before admitting the revision. A pod `nodeName` is rejected under graph placement because it bypasses scheduling. General placement does not turn a graph into an ephemeral graph.

This is a **shared placement boundary**, not gang scheduling or an atomic capacity reservation. Pods can occupy different nodes in the selected slice, and different allowed zones; setting one zone label targets one zone. Kubernetes still checks each pod's CPU, memory, GPU requests and constraints individually. A graph may be partially admitted while waiting for capacity. Co-starting an entire graph or choosing one common pool dynamically from several alternatives would require a separate group-admission/reservation mechanism. PVC/resource selection remains in Resource manifests (`storageClassName`, volume selectors, etc.); node placement applies to pod execution.

## Ephemeral execution

“Ephemeral” means compute can disappear before completion. Graph intent and observations remain durable in Kubernetes. It does **not** mean the CR deletes itself after running or checkpoints are stored on a disposable disk.

`Ephemeral` executes as a Job on explicit spot node selectors/tolerations. `EphemeralGraph` propagates placement into descendant Jobs and Deployments, including nested graphs and feedback bodies. Node-specific placement intersects inherited selectors and affinity, and adds tolerations; conflicting selector values are rejected. Resources such as PVCs remain independent of spot pod lifetime. Label and toleration values are provider-specific; no provider is assumed.

Kubernetes replaces failed/evicted Job pods within the declared `backoffLimit`. An interrupted pod is not a completed node. After retry exhaustion the graph reports failure and retains evidence for inspection. Daemon Deployments replace lost pods indefinitely. Recreated compute starts the container entrypoint again: durable checkpoint/resume and idempotent external effects belong to the application. Kubernetes Jobs do not provide exactly-once effects. Avoid `emptyDir` and node-local volumes for checkpoints that must survive node loss. CR/node deletion and replacement intentionally drains owned PVCs too; use externally managed PVCs when data must outlive a boundary.

## Resource compiler objects

`polyad.operator.compiler.asts` defines attrs objects for all ten Polyad CR kinds
and the Job, Deployment, Service, ConfigMap, PersistentVolumeClaim and Lease kinds the
operator manages. Metadata, owner references, Job/Deployment specs, status patches
and deletion preconditions have dedicated types. A shared resource registry supplies
API versions, plural names and graph-boundary membership.

The controller compiles graph intent with `compiler.children.owned_child`, which
assigns deterministic names, desired hashes and ownership. The API adapter lowers
these objects to Kubernetes documents with cattrs immediately before sending them.
For example:

```python
from polyad.operator.compiler.asts import Graph, JobSpec, ObjectMeta, PodTemplate, to_document
from polyad.operator.compiler.children import owned_child

parent = Graph(
    metadata=ObjectMeta(name="pipeline", namespace="default", uid="persisted-parent-uid"),
    spec={},
)
job = owned_child(parent, "task", "Job", JobSpec(
    backoffLimit=0,
    template=PodTemplate(spec={
        "restartPolicy": "Never",
        "containers": [{"name": "main", "image": "busybox:1.37", "command": ["true"]}],
    }),
))
document = to_document(job)
```

`from_document` selects a concrete type by kind and checks its API version.
Unknown native fields are retained in `extra` and flattened back into the document;
extensions cannot override modeled fields. Optional absent fields are omitted,
while explicit `False`, zero and empty mappings are preserved. ConfigMap data stays
at the document root. Pod settings and CR specs remain extensible dictionaries;
the existing graph library and CRD schemas validate graph semantics. These models
cover the operator's generated execution specs rather than the complete Kubernetes
schema. Desired-resource hashes retain their existing format across this refactor.

## Reconciliation and shutdown

Kopf runs on `OperatorThread`, which owns its asyncio event loop. The Python main thread owns process signals and forwards SIGTERM/SIGINT through a thread-safe stop event. Start with:

```sh
poetry run polyad-operator --namespace default
# or
python -m polyad.operator.runtime --namespace default
```

The Docker image starts this Python wrapper. It never invokes the Kopf CLI.

Dragonfly stores resource notifications in 32 shard streams and coalesces duplicate notifications for five seconds. Each replica processes its leased streams through one local FIFO consumer. A notification arriving during an active pass schedules another pass. Every pass rereads the CR, referenced definitions, and owned resources. API calls have finite transport timeouts; conflict, missing-dependency and transient-error retries return to the queue with refreshed state. Five-second timers recover missed child changes and changes to definitions. Raw Kopf event handlers publish work hints without writing Kopf progress annotations. Five-second API scans recover missed events and rebuild the shared queues after cache loss. Kubernetes desired state and status remain authoritative.

Deterministic names and owner UID checks make lost create acknowledgements recoverable. Status patches and rewrites include resource versions; deletes include UID and resource-version preconditions. Acknowledging DELETE does not prove disappearance. Replaced and removed children drain before replacements are admitted. A definition update replaces affected execution resources; completed Jobs can therefore run again. This is an explicit revision boundary, not transparent checkpoint migration.

`Rewrite` replaces the complete graph spec at `expectedGeneration`. The spec and rewrite receipt annotation are committed atomically; a retry checks the receipt before doing anything. Reference edits and Graph spec edits can also change desired topology directly. No arbitrary Python rewrite callbacks run inside the operator.

`spec.suspend: true` stops admission and drains the boundary; clearing it starts fresh execution. `ShutdownPolicy.afterSeconds` measures wall time from CR creation, including downtime. `graceSeconds` becomes pod `terminationGracePeriodSeconds`; application SIGTERM/preStop logic should checkpoint/drain inside that time. These are Kubernetes process contracts, not local `Control.pause` calls.

On CR deletion, the owning shard retains `polyad.astrivant.com/drain` while children exist. Foreground garbage collection waits for dependent Pods and their custom finalizers, including PVC protection. Custom finalizers must have an application/controller that removes them after durable cleanup; adding an unimplemented finalizer intentionally blocks deletion. Stopping the operator pod leaves workload CRs and finalizers intact so a replacement operator can resume ownership.

## Health

The operator's startup and liveness probes use Kopf's `/healthz` endpoint and the registered worker probe. Readiness also checks API/Lease renewal freshness and Dragonfly connectivity (`apiFresh` and `cacheFresh`). Periodic API scans and cache pings keep connectivity observations current even in an empty namespace. A slow or unavailable API can make the pod unready without triggering a restart loop. A stopped worker fails liveness. The health handler never treats an indefinitely running workload as a fault.

Daemon containers must supply startup, readiness and liveness probes. Graph admission observes current-generation Deployment ready/available replicas. Readiness edges control initial admission only: downstream work already admitted continues if upstream readiness later drops. End-to-end availability and recovery require application retry/backpressure contracts.

## Replicas, shared queues and autoscaling

The chart starts two operator replicas with rolling updates. All replicas watch the
namespace. A Kubernetes `coordination.k8s.io/v1` Lease elects one planner, which
uses rendezvous hashing to assign 32 fixed shards across live process identities.
Each shard has its own exclusive Lease. Parent graphs, nested boundaries and
rewrites of those graphs resolve to the same root shard, so related reconciliation
passes use one worker. Independent roots can run concurrently on other replicas.
Shard assignment balances graph keys, not measured graph execution cost; one large
graph does not become faster by adding replicas.

Membership and shard Leases renew every five seconds under healthy API conditions.
Lease acquisition and renewal use resource-version compare-and-swap. A successor
observes an unchanged Lease for 90 seconds before taking it; the old worker stops
starting writes with less than 35 seconds of locally measured ownership remaining.
Every workload write rereads its Lease. Replicas that cannot reach Dragonfly stop
renewing membership and shard ownership. HTTP retries are disabled, calls have
5-second connection/20-second read timeouts, and cancellation joins outstanding
HTTP calls. Reassignment stops renewals only after the active pass finishes.
Handoffs deliberately include an expiry delay; API problems can extend recovery.
Do not change the fixed shard count or run incompatible operator versions together.
When upgrading from the original singleton operator, stop its Deployment before
starting coordinated replicas: the old binary does not honor shard ownership.

Each Dragonfly stream has one consumer group. A new Lease holder reclaims pending
entries before reading new ones, and acknowledges a notification only after its
refreshed attempt. Pending graph conditions are retried by the next API scan.
Failed API writes leave their notification pending. Events carry resource keys,
never precomputed mutations, so an old notification cannot replay an old spec.
Finalizer changes, rewrites and status updates pass through the same guarded queue.
A stopped or disconnected replica cannot keep starting writes once its renewal
budget expires. Leases are cooperative coordination, not an atomic transaction
across Kubernetes objects: an arbitrarily delayed server-side write or a process
paused between its ownership check and request cannot be absolutely fenced by a
Lease alone. Resource versions, UID preconditions and deterministic child names
limit stale mutations; this is not an exactly-once execution guarantee.

Dragonfly also caches duplicate notifications. The bundled single-instance
StatefulSet uses a PVC and five-minute snapshots, with eviction disabled. Queue
entries survive operator restarts; a cache crash can lose entries since its last
snapshot. The API rescan rebuilds those hints. Cache unavailability pauses
consumption and fails readiness without restarting healthy operator processes.
For an externally managed HA Dragonfly deployment, disable the bundled instance
and supply a connection Secret (`url` key, supporting `rediss://` and credentials):

```sh
helm upgrade --install polyad charts/polyad --namespace polyad \
  --set dragonfly.enabled=false --set dragonfly.existingSecret=polyad-dragonfly
```

For local development, set `POLYAD_DRAGONFLY_URL` (default
`redis://localhost:6379/0`). Kubernetes credentials resolve from the pod's service
account in-cluster and fall back to the active kubeconfig outside the cluster.
All replicas serving a namespace must use the same Dragonfly database. Install
one Polyad release per namespace; its replicas share the fixed coordination names.

Enable CPU-based HPA with:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad \
  --set autoscaling.enabled=true --set autoscaling.minReplicas=2 \
  --set autoscaling.maxReplicas=8
```

HPA requires the cluster metrics API and operator CPU requests. Without HPA,
`replicaCount` controls scale. New replicas receive shards automatically; scale-down
allows their leases to expire. At most 32 replicas can own useful shards. CPU HPA
does not directly measure queue backlog. The default cache is a single availability
dependency; use an external HA deployment if cache downtime is unacceptable.

References: [Kubernetes Leases](https://kubernetes.io/docs/concepts/architecture/leases/),
[Dragonfly pending-message recovery](https://www.dragonflydb.io/docs/command-reference/stream/xautoclaim).

## Build, install and exercise

```sh
docker build -t polyad:dev .
# Push to your registry, or load into your local test cluster.
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set image.repository=YOUR_REGISTRY/polyad --set image.tag=dev
kubectl -n polyad apply -f examples/finite.yaml
kubectl -n polyad get graphs -o yaml
kubectl -n polyad apply -f examples/persistent.yaml
kubectl -n polyad delete graph persistent
```

The default image name is a publication target, not an assertion that an image is published. Build and supply an image first. Helm installs CRDs from `crds/` but does not upgrade or remove them automatically; review and apply CRD schema changes separately before a chart upgrade. Removing the operator while graphs still have finalizers prevents their cleanup until the operator returns.

CI runs Python checks, uses `astrivant/hypothesis-helm` to property-test the Helm chart, and exercises real lifecycle behavior in a disposable kind cluster. The operator does not require hypothesis-helm at runtime.

References: [Kopf embedding](https://docs.kopf.dev/en/stable/embedding/), [Kopf health probes](https://docs.kopf.dev/en/stable/probing/), [Kubernetes finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/).
