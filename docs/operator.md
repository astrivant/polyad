# Graph based workload scheduling on Kubernetes

Polyad schedules container workloads through graphs, dependencies, placement
constraints and admission gates. Storage and application recovery are explicit
workload responsibilities. A graph's `mode: persistent` describes a long-running
lifecycle; it does not enable persistent storage or checkpointing.

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
| `PolyGraph`, `GraphNode` | `PolyGraph` CR; nodes reference other graph types and descendant status aggregates at the root |
| `Placement` on `Topology` / `Graph` | Shared node-group or labeled resource-slice placement, inherited by descendant pods |
| `Persistence` | Workload/Daemon `spec.persistence` with an explicit StorageClass and existing PVC |
| `DelayGate` | `Gate` CR with `spec.delaySeconds`; deadlines belong to graph instances |
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

Definitions (`Workload`, `Daemon`, `Ephemeral`, `Resource`, `Gate`, `ShutdownPolicy`) do not launch work by themselves. A Graph references them by name in the same namespace. Nested Graph/PolyGraph/EphemeralGraph/Feedback definitions must set `templateOnly: true`; the parent instantiates an owned copy with that flag cleared. Recursive references and nesting deeper than 32 are rejected. A finite parent must reference finite children if it expects completion.

`connections` record topology; applications configure transport. Container/resource string fields can use `${nodes.NAME.name}` to refer to the generated Kubernetes resource name of another node, for example a PVC's `claimName` or a Service hostname. Names stay stable across resource replacement. Resources with probes, finalizers or readiness requirements should be dependencies before consumers start. A Service still needs an explicit selector; applications can use their own pod labels in templates.

## Scheduling a graph onto a resource slice

Every `Graph` can set `spec.placement`; placement is independent of finite, persistent, or ephemeral lifetime. [Complete example](../examples/graph-placement.yaml):

```yaml
spec:
  placement:
    enforce: true
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

Placement defaults to `enforce: true`. The compiler propagates it through nested
graphs and feedback epochs into every Job and daemon Deployment pod template.
Workload definitions and pod templates can narrow enforced selectors and required
affinity, combine preferences and add tolerations. Conflicting exact selectors
are rejected before admitting the revision; incompatible affinity predicates can
leave pods unschedulable. Pod affinity/anti-affinity is preserved. A pod `nodeName`
is rejected inside placed graphs because it bypasses scheduling.

Set `enforce: false` on a graph's placement to make it a default. An explicit,
more specific graph, workload or pod placement then replaces that default in
full. For example, a workload may select `pool: gpu` beneath a default `pool: cpu`.
Omitting a child placement retains the defaults. An enforced ancestor cannot be
relaxed by a child setting `enforce: false`. General placement does not change a
graph's lifecycle type or remove inherited ephemeral storage restrictions.

Tolerations on CRDs define fields that Polyad reads; Kubernetes does not schedule
custom resources themselves. The guard is in the compiler: it merges and validates
the inherited placement before creating native pod templates, including required
tolerations. Tolerations allow matching taints; selectors and required affinity
choose eligible nodes. They do not reserve nodes, ensure scheduling, or prevent
node failure. Polyad's compilation policy is not an admission webhook policing
arbitrary edits by other Kubernetes clients.

Operator placement is separate: set Helm `operator.nodeSelector` and `operator.tolerations` to run
operator replicas on a control node group. Workload graph placement has no effect
on the operator pods.

This is a **shared placement boundary**, not gang scheduling or an atomic capacity reservation. Pods can occupy different nodes in the selected slice, and different allowed zones; setting one zone label targets one zone. Kubernetes still checks each pod's CPU, memory, GPU requests and constraints individually. A graph may be partially admitted while waiting for capacity. Co-starting an entire graph or choosing one common pool dynamically from several alternatives would require a separate group-admission/reservation mechanism. PVC/resource selection remains in Resource manifests (`storageClassName`, volume selectors, etc.); node placement applies to pod execution.

For advance capacity requests, see [capacity planning](capacity.md).

## Ephemeral execution

“Ephemeral” means compute can disappear before completion. Graph intent and observations remain durable in Kubernetes. It does **not** mean the CR deletes itself after running or checkpoints are stored on a disposable disk.

`Ephemeral` executes as a Job on explicit spot placement. `EphemeralGraph`
propagates placement and its storage prohibition through all nested boundaries,
including PolyGraphs and Feedback epochs. Persistent PVC mounts, PVC resource
nodes, persistence declarations and storage classes are rejected before leaf
workloads are admitted. Label and toleration values are provider-specific.

Kubernetes replaces failed/evicted Job pods within the declared `backoffLimit`.
After retry exhaustion the graph reports failure. Recreated compute starts the
container entrypoint again; applications must tolerate repeated side effects.
Ephemeral workloads should be disposable or reconstruct their inputs. Persistent
workloads belong on non-spot capacity in regular graphs.

## Workload persistence

Workload and Daemon definitions can opt into an existing PVC:

```yaml
spec:
  persistence:
    enabled: true
    storageClass: durable-ssd
    claimName: application-data
    mountPath: /var/lib/application
  template:
    spec:
      containers:
        - name: worker
          image: your-worker-image
```

`storageClass` and `claimName` are required when enabled. The operator checks the
PVC in the same namespace and requires its `spec.storageClassName` to match.
Missing claims defer admission; a class mismatch marks the graph invalid.
Pending claims are allowed because `WaitForFirstConsumer` binds after a pod is
scheduled. The compiler mounts the claim in application and init containers.
Choose access modes and topology compatible with your replicas and node group.
For graph-owned claims using `WaitForFirstConsumer`, depend on `started`;
waiting for `ready` would require binding before the consuming pod exists.

Administrators provision StorageClasses; Polyad does not create cluster-scoped
StorageClass resources. External claims are not adopted or deleted by graph
cleanup. A claim created through a graph's own Resource node remains graph-owned
and is drained with that graph. Use an external claim when data must outlive it.
These workloads are not intended for spot instances. The compiler rejects
persistence beneath ephemeral boundaries, even when placement overrides are
allowed. StorageClass declarations in preserved native volume specs are also
checked. See [Kubernetes persistent volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/).

Storage preserves files; it does not preserve process memory or implement
checkpointing. Kubernetes suspension currently drains execution, and clearing
suspension starts containers again. Applications must handle signals, persist
consistent state, and load that state themselves. Local cooperative workloads
can checkpoint when their implementation supports it and a durable journal is
configured; this capability is not a general scheduler guarantee.

## Delay gates

```yaml
apiVersion: polyad.astrivant.com/v1alpha1
kind: Gate
metadata:
  name: cooldown
spec:
  delaySeconds: 30
```

Set `gate: cooldown` on a graph node. Its delay starts when the operator observes
its dependency conditions satisfied. The operator persists `status.delays` with
a `notBefore` deadline and rereads it before admitting the node. Other eligible
nodes continue; waiting consumes no worker or slot. Replicas and operator
restarts share that persisted deadline. Admission occurs on a later reconciliation
after the deadline, so it can be later than the requested delay.

A changed graph generation, gate revision, workload revision or dependency UID
restarts the timer. A false dependency observed before admission clears it.
Suspension/resume edits change the graph generation and restart pending delays.
Each gate selects exactly one of
`expression` or `delaySeconds`; delays range from zero to 315360000 seconds.

Locally, use `routes={"next": DelayGate(30)}` with `polyad.graph.DelayGate`.
The local scheduler uses a monotonic timer after dependencies complete. Local
timer progress is process-local and starts again when constructing a scheduler;
it is not an application checkpoint.

## Resource compiler objects

`polyad.compiler.asts` defines attrs objects for all ten Polyad CR kinds
and the Job, Deployment, Service, ConfigMap, PersistentVolumeClaim and Lease kinds the
operator manages. Metadata, owner references, Job/Deployment specs, status patches
and deletion preconditions have dedicated types. A shared resource registry supplies
API versions, plural names and graph-boundary membership.

`polyad.compiler.passes` contains the transformations applied to those models:

| Module | Responsibility |
| --- | --- |
| `composition` | Validate request identities and resolve references into resource definitions |
| `children` | Build owned resources with deterministic names and revision hashes |
| `audit` | Carry request and definition provenance into child manifests |
| `identity` | Inject graph, execution, Pod and operator endpoint context into workload containers |
| `storage` | Validate persistence and configure workload storage |
| `network` | Intersect inherited traffic rules and generate network and mesh policies |
| `capacity` | Identify upcoming work and compile capacity reservation templates |
| `mutations` | Check declared effects and shared bounds, and produce ordered execution batches |
| `schema` | Convert attrs models into structural OpenAPI schemas |

These passes are functions composed by the API and operator; they do not issue
Kubernetes requests. Resource models remain in `compiler.asts`, and kind metadata
remains in `compiler.registry`. Import transformations directly from
`polyad.compiler.passes` submodules.

The controller compiles graph intent with `compiler.passes.children.owned_child`, which
assigns deterministic names, desired hashes and ownership. The API adapter lowers
these objects to Kubernetes documents with cattrs immediately before sending them.
For example:

```python
from polyad.compiler.asts import Graph, JobSpec, ObjectMeta, PodTemplate, to_document
from polyad.compiler.passes.children import owned_child

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

### Graph observation objects

`GraphMetrics` is the attrs tree behind `status.metrics`. Its children separate
the shape of a graph (`TopologyMetrics`), work in progress (`ExecutionMetrics`),
owned Kubernetes resources (`ResourceMetrics`), immediate graph children
(`SubgraphMetrics`) and the recursive summary (`RollupMetrics`). Nested counters
also have concrete types, so editors and Mypy can follow fields to their values.
Field names match the Kubernetes document, including `observedGeneration`.

```python
from polyad.compiler.asts import GraphMetrics, converter, to_document
from polyad.compiler.passes.schema import structural_schema
from polyad.graph import measure_topology
from polyad.graph.topology import topology

shape = measure_topology(topology({"nodes": []}))
metrics = GraphMetrics(observedGeneration=1, topology=shape)
document = to_document(metrics)
restored = converter.structure(document, GraphMetrics)
assert restored.topology is not None
assert restored.topology.admission.depth == 0
schema = structural_schema(GraphMetrics)
```

For an API response, decode `resource["status"]["metrics"]` in the same way.
Missing fields use Python defaults; this does **not** establish that an observation
is fresh or complete. The operator checks raw child generations and rollup field
presence before aggregating descendants. `observe_graph` in
`polyad.operator.graph_status` builds the full typed tree from a graph document
and its owned children. Existing `instance_metrics` and `topology_metrics` calls
still return dictionaries.

These models preserve unknown fields through the existing `extra` mechanism.
The metrics fields marked `emit_none` serialize explicit nulls to clear stale
observations in Kubernetes merge patches; other AST optional fields retain their
omission behavior. Attrs types provide the Python contract; generated CRD
constraints validate API writes. Python constructor defaults do not become
Kubernetes defaults, and unknown metrics fields remain subject to CRD pruning.

The models generate **the `status.metrics` schema** for Graph, PolyGraph,
EphemeralGraph and Feedback. Descriptions and numeric limits live in attrs field
metadata; nested objects, lists, literals and nullable fields come from their
annotations. CRD specs and other status fields are maintained separately.

After editing a metrics model, regenerate and check the chart:

```bash
bash scripts/project-python.sh scripts/generate-status-schemas.py
bash scripts/project-python.sh scripts/generate-status-schemas.py --check
```

The pre-commit hook and CI reject schema drift across all four CRDs. Generation
replaces only the metrics property, preserving unrelated manifest sections.
As with other CRD changes, apply updated CRDs before upgrading an existing Helm
release; Helm does not upgrade files under `crds/` automatically.

## Feedback epochs

A `Feedback` resource repeatedly creates an execution instance of a finite graph.
An epoch includes the whole graph, with its dependencies, gates and nested work.
For example, a sample-and-adjust graph can read current measurements, calculate
new settings and apply them; the next epoch observes the updated system.

The operator records completion in `status.epoch` and `status.lastEpochTime`,
then deletes the finished epoch's graph and waits for its resources to disappear.
Only one epoch executes at a time. A failed or unfinished epoch does not advance
the completion counter or start the next epoch.

| Setting | Meaning |
| --- | --- |
| `spec.graph` | Finite graph template instantiated for every epoch |
| `spec.kind` | Epoch graph kind: Graph, PolyGraph or EphemeralGraph; defaults to Graph |
| `spec.rounds` | Maximum completed epochs; omit for indefinite recurrence, or set zero to finish without starting work |
| `spec.intervalSeconds` | Minimum wait from recorded epoch completion; defaults to one second. Cleanup and reconciliation can extend it |
| `spec.suspend` | Drain active execution and prevent new epochs; clearing it permits fresh execution |

Epoch counters and completion timestamps live in Kubernetes status, so an operator
restart does not reset the round limit. Each epoch starts fresh containers.
Applications must explicitly read and write any state or results needed by later
epochs. Use storage that outlives the epoch, such as an external PVC configured
through [workload persistence](#workload-persistence), when preserving files.

Feedback supplies repetition; the workloads implement any feedback algorithm.
There is no automatic result-to-input transfer or convergence test. See the
[two-epoch example](../examples/feedback.yaml) and
[local Feedback guide](../pkg/polyad/balance/README.md#feedback-graphs-and-daemon-like-work)
for the separate Python factory-based execution model.

## Reconciliation and shutdown

Kopf runs on `OperatorThread`, which owns its asyncio event loop. The Python main thread owns process signals and forwards SIGTERM/SIGINT through a thread-safe stop event. Start with:

```sh
poetry run polyad-operator --namespace default
# or
python -m polyad.operator.runtime --namespace default
```

The Docker image starts this Python wrapper. It never invokes the Kopf CLI.

Dragonfly stores resource notifications in 32 shard streams and coalesces duplicate notifications for five seconds. Each replica processes its leased streams through one local FIFO consumer. Notifications carry keys; each attempt reads the latest intent. Every pass rereads the CR, referenced definitions, and owned resources. API calls have finite transport timeouts; conflict, missing-dependency and transient-error retries return to the queue with refreshed state.

Raw Kopf event handlers publish work hints without writing Kopf progress annotations. Five-second API scans recover missed events and rebuild the shared queues after cache loss. Kubernetes desired state and status remain authoritative.

Deterministic names and owner UID checks make lost create acknowledgements recoverable. Status patches and rewrites include resource versions; deletes include UID and resource-version preconditions. Acknowledging DELETE does not prove disappearance. Replaced and removed children drain before replacements are admitted. A definition update replaces affected execution resources; completed Jobs can therefore run again. This is an explicit revision boundary, not transparent checkpoint migration.

`Rewrite` replaces the complete graph spec at `expectedGeneration`. The spec and rewrite receipt annotation are committed atomically; a retry checks the receipt before doing anything. Reference edits and Graph spec edits can also change desired topology directly. No arbitrary Python rewrite callbacks run inside the operator.

Rewrites use the [mutation-plan executor](mutations.md) to refresh target identity,
generation, resource version and deletion state before dispatch. The Python library
also supports bounded concurrent batches for operations with complete, disjoint
declared effects and compatible shared bounds. Topology replacements retain the
existing root-family serialization because their descendant effects are not fully modeled.

`spec.suspend: true` stops admission and drains the boundary; clearing it starts fresh execution. `ShutdownPolicy.afterSeconds` measures wall time from CR creation, including downtime. `graceSeconds` becomes pod `terminationGracePeriodSeconds`; application SIGTERM/preStop logic should checkpoint/drain inside that time. These are Kubernetes process contracts, not local `Control.pause` calls.

On CR deletion, the owning shard retains `polyad.astrivant.com/drain` while children exist. Foreground garbage collection waits for dependent Pods and their custom finalizers, including PVC protection. Custom finalizers must have an application/controller that removes them after durable cleanup; adding an unimplemented finalizer intentionally blocks deletion. Stopping the operator pod leaves workload CRs and finalizers intact so a replacement operator can resume ownership.

## Graph instance status

Executable `Graph`, `PolyGraph`, `EphemeralGraph`, and `Feedback` CRs publish lifecycle state
and `status.metrics`. Reusable `Workload`/`Daemon` definitions and `templateOnly`
graphs remain inert; their instances report through the owning graph. Inspect
an instance with:

```bash
kubectl get graphs,polygraphs,ephemeralgraphs,feedbacks -n polyad -o wide
kubectl get graph finite -n polyad -o jsonpath='{.status.metrics}'
```

The default columns show phase, desired node count and observed active nodes.
Wide output also includes breadth and depth.

| Field under `status.metrics` | Meaning |
| --- | --- |
| `topology` | Shape of the current desired graph; Feedback uses its epoch template |
| `observedTopology` | Induced graph of declared nodes with owned execution resources |
| `execution` | Observed, pending, active, ready, completed, failed and terminating node counts; reserved and available slots |
| `resources` | All directly owned resources by kind, including obsolete and terminating children |
| `subgraphs` | Immediate nested instance identities, phases, generation freshness, shape and execution summaries |
| `rollup` | Recursive descendant graph phases, leaf progress, resource counts and nesting depth |
| `observedGeneration` | Spec generation used for these measurements |
| `topologyError` | Validation failure when the desired shape cannot be measured |

NetworkX computes `topology.admission` from the acyclic admission relation.
`depth` counts node layers: an empty graph has depth zero, independent nodes
have depth one, and a chain of three nodes has depth three. `breadth` is the
largest layer, and `layerWidths` records each layer's size. A diamond has widths
`[1, 2, 1]`, breadth 2, depth 3, and `breadthDepth` 6. That product describes
shape; execution cost and available parallelism also depend on gates, resource
slots and lifecycle conditions. Edge counts deduplicate directed node pairs.
Root, leaf and maximum fan-in/fan-out counts expose branching and joins.

`topology.connections` measures cyclic data flow separately. It reports weak and
strong component counts, cyclic components (including self-loops), and the
largest strong component. Its `condensation` layers collapse each mutually
reachable component to one vertex, following NetworkX's
[condensation definition](https://networkx.org/documentation/stable/reference/algorithms/generated/networkx.algorithms.components.condensation.html).
Depth through a cycle is therefore never presented as a finite execution path.
The implementation does not enumerate all paths or cycles.

Measurements cover **one scheduling boundary**. A nested graph counts as one
node; its own CR reports its internal breadth, depth and progress. Nested
summaries include a `current` flag comparing both lifecycle and metric generations
with the child spec generation. Stale or terminating children expose identity
and phase but omit their metric summaries. This flag measures generation
freshness, not elapsed time. Feedback mirrors execution only from its current
epoch with current observations; between epochs or before observation, execution
and observed shape are absent or null. Completed epochs remain represented by `status.epoch`.

Observed inventory includes terminating or superseded execution until the API
confirms removal. Removed nodes disappear from topology while their resources
remain in cleanup counts. Lifecycle counters overlap: a completed Job may also
be ready. Active nodes are admitted resources without terminal state or deletion;
they are not a count of running Pods. Pending means no observed resource and
includes dependency, gate and capacity waits. Slot reservations follow the
scheduler's observed non-completed nodes, including failed or terminating ones.

Metrics refresh after successful and blocked reconciliations, including waiting
for definitions, replacement, suspension and finalizers. API reads form an
eventually consistent inventory; there is no atomic snapshot across Kubernetes
objects. Writes use the same ownership guard, ordered queue and resource-version
checks as lifecycle updates, and unchanged metrics do not cause another write.
Invalid shapes report `topologyError` and retain resource inventory. Lifecycle
`status.observedGeneration` and metric generation are explicit because they can
be persisted in separate ordered requests.

### Composing graph types with PolyGraph

`PolyGraph` is the explicit graph-of-graphs abstraction. Its nodes reference
`Graph`, `EphemeralGraph`, `Feedback`, or other `PolyGraph` templates. Each
reference creates a distinct owned instance, so using the same template twice
creates two independent executions. Ordinary `Graph` boundaries can mix these
graph references with workloads, daemons and resources.

```python
from polyad.graph import GraphNode, PolyGraph

application = PolyGraph(
    nodes=(
        GraphNode(name="batch", kind="Graph", ref="batch-template"),
        GraphNode(name="spot", kind="EphemeralGraph", ref="spot-template"),
        GraphNode(name="group", kind="PolyGraph", ref="group-template"),
    ),
)
```

Referenced definitions use `templateOnly: true`; their owned copies execute.
Root placement flows through every boundary. Admission gates, finite versus
persistent lifecycle rules, rewrites and foreground cleanup apply to PolyGraph
as they do to other graph boundaries. A finite parent cannot contain an
unbounded persistent child, and a completion dependency cannot wait on one.
Recursive template references are rejected; nesting retains the existing
32-level limit. There is no sharing of execution ownership between parents.

Every boundary, including an ordinary Graph, publishes `status.metrics.rollup`:

| Rollup field | Meaning |
| --- | --- |
| `graphCount`, `graphsByPhase` | Known descendant boundaries, including this boundary, and their lifecycle phases |
| `nestingDepth` | Known containment layers including this boundary as layer one; separate from dependency depth |
| `leafNodes`, `observedLeafNodes`, `pendingLeafNodes` | Declared and observed non-boundary work across known graph instances |
| `activeLeafNodes`, `readyLeafNodes`, `completedLeafNodes`, `failedLeafNodes`, `terminatingLeafNodes` | Descendant leaf lifecycle counts |
| `resourceCount`, `terminatingResources` | Known owned resources at all levels, counting each resource once and excluding the root CR |
| `observationsComplete`, `unobservedGraphs` | Whether all expected instances and subtree summaries were available at matching generations |

Each parent combines its direct leaf observations with each child's recursive
summary exactly once. An intermediate graph's node reservation is not counted
as another leaf workload. Slot capacities remain local scheduling budgets and
are not summed as physical cluster capacity. Feedback's template is not a
second execution: its epoch instance contributes the actual work. Counts
describe present inventory; historical epochs remain in the durable epoch
counter after their resources are removed.

Missing instances, older operators without rollups, stale generations and
terminating subgraphs mark the aggregate incomplete. Their unknown contents
are omitted, so totals are lower bounds until fresh summaries propagate.
`observationsComplete` describes the observations used; Kubernetes provides no
atomic snapshot of the whole hierarchy. Status events enqueue the immediate
parent through the shared queue, and periodic rescans repair missed events.
The entire ownership tree belongs to the root's shard. Terminal failures and
invalid child definitions propagate through lifecycle status to the root.

See [the mixed composition example](../examples/polygraph.yaml). It combines
a nested PolyGraph, spot execution, and a finite Feedback boundary:

```bash
kubectl apply -n polyad -f examples/polygraph.yaml
kubectl get polygraph composed -n polyad -o wide
kubectl get polygraph composed -n polyad -o jsonpath='{.status.metrics.rollup}'
```

The Python library exposes the same structural measurements with
`polyad.graph.topology_metrics(topology)` and accepts an optional set of present
node names to measure an observed subset.

## Debug logging

Set Helm `operator.logLevel: DEBUG` to trace reconciliation, admission decisions,
queue coalescing and delivery, shard leases, and API dispatch/completion timings.
The default is `INFO`. Supported levels are `DEBUG`, `INFO`, `WARNING`, `ERROR`
and `CRITICAL`.

```sh
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set operator.logLevel=DEBUG
kubectl logs -n polyad -l app.kubernetes.io/name=polyad -c operator --prefix=true --follow
```

Include your existing values file when upgrading a configured release. For a
local process, use `polyad-operator --log-level DEBUG` or
`POLYAD_LOG_LEVEL=DEBUG`; the command-line option takes precedence.

These settings affect Polyad loggers. They do not enable Kubernetes client,
HTTP transport or other dependency debug logging. Diagnostic messages include
resource identities, queue/shard context, delay reasons and elapsed seconds;
request/response bodies, gate facts, cache URLs and credentials are omitted.
Timestamps, module names and thread names identify each message. Set the level
back to `INFO` after troubleshooting to reduce volume.

## Health

The operator's startup and liveness probes use Kopf's `/healthz` endpoint and the registered worker probe. Readiness also checks API/Lease renewal freshness and Dragonfly connectivity (`apiFresh` and `cacheFresh`). Periodic API scans and cache pings keep connectivity observations current even in an empty namespace. A slow or unavailable API can make the pod unready without triggering a restart loop. A stopped worker fails liveness. The health handler never treats an indefinitely running workload as a fault.

Daemon containers must supply startup, readiness and liveness probes. Graph admission observes current-generation Deployment ready/available replicas. Readiness edges control initial admission only: downstream work already admitted continues if upstream readiness later drops. End-to-end availability and recovery require application retry/backpressure contracts.

The optional [metrics API](metrics.md) exposes Prometheus and JSON snapshots on
a separate listener, including queue pressure and graph hierarchy inventory.

### Backlog metrics

Kopf's `/healthz` response includes `scheduler.backlog`:

```json
{
  "inboundUpdates": {
    "scope": "namespace",
    "queued": 9,
    "unacknowledged": 3,
    "total": 12,
    "owned": {"queued": 3, "unacknowledged": 2, "total": 5},
    "sampleAgeSeconds": 1.2,
    "fresh": true
  },
  "kubernetesWrites": {
    "scope": "replica",
    "queued": 1,
    "inFlight": 1,
    "total": 2,
    "workloads": {
      "queued": 1, "inFlight": 0, "total": 1,
      "oldestQueuedSeconds": 2.5, "oldestInFlightSeconds": 0
    },
    "coordination": {
      "queued": 0, "inFlight": 1, "total": 1,
      "oldestQueuedSeconds": 0, "oldestInFlightSeconds": 0.4
    }
  }
}
```

Inbound counts cover all 32 Dragonfly streams in the namespace, including shards
without a current owner. `queued` counts unread entries; `unacknowledged` includes
active deliveries and entries awaiting retry or recovery. `total` is their sum.
`owned` restricts those same counts to this replica's currently owned shards.
These are coalesced reconciliation hints, not counts of raw watch events.
Namespace totals are shared observations: do not sum them across replicas.

A separate task samples every five seconds. Counts are consistent within each
shard; the namespace sum is an approximate snapshot across shards. Before the
first successful sample, counts and sample age are `null`. A failed sample retains
its previous values and sets `fresh: false`; samples also become stale after
15 seconds. Health probes read cached data and perform no cache or API requests.

Write gauges count concrete mutation requests in this process: `queued` means
waiting for the API adapter's ordered dispatch slot or its ownership check;
`inFlight` means dispatched and awaiting transport completion. Counts include
workload mutations and Lease coordination separately, with the age of the oldest
request in each stage. Reads are excluded. Cancellation retains an in-flight
request until its HTTP call finishes, and rejected or failed requests release
their gauges. Future mutations that have not been compiled yet, and retries
represented by inbound hints, are not counted as queued API writes. Sum write
gauges across replicas for aggregate process pressure; these do not measure the
Kubernetes API server's internal queue.

The existing `scheduler.pending` field remains the local reconciliation queue
length. Backlog growth or stale telemetry alone does not fail startup, readiness
or liveness; connectivity and worker health retain their existing checks. HPA
continues to use CPU utilization; these JSON metrics do not automatically wire a
queue-based autoscaling policy.

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

Dragonfly also caches duplicate notifications. The Helm chart depends on the
[upstream Dragonfly operator](https://github.com/dragonflydb/dragonfly-operator),
which manages a single data instance by default. Five-minute PVC snapshots and
disabled eviction preserve queue hints across routine restarts. Cache loss can
still discard hints; API rescans reconstruct them from Kubernetes state. Cache
unavailability pauses consumption and fails readiness without restarting healthy
Polyad processes.

Enable replication and automatic primary failover with:

```sh
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm dependency build charts/polyad
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set dragonfly.ha.enabled=true --set dragonfly.ha.replicas=2
```

The replica count includes the primary. HA instances run on distinct nodes by
default; `dragonfly.ha.topologyKey=topology.kubernetes.io/zone` instead requires
distinct zones. Provide enough eligible nodes/zones and persistent storage.
Replication-aware readiness and a disruption budget protect voluntary drains.
Two leader-elected Dragonfly controller replicas supervise the cache by default.
The managed `<release>-queue` Service follows the primary; clients discard a
connection that returns READONLY after demotion and reconnect on the next queue
pass. Asynchronous replication can lose recent notifications during failover;
the API rescan repairs those hints. This does not promise lossless failover.
See the [upstream replication and Service semantics](https://www.dragonflydb.io/docs/managing-dragonfly/operator/installation).

For an externally managed Dragonfly deployment, disable the bundled dependency
and supply a connection Secret (`url` key, supporting `rediss://` and credentials):

```sh
helm upgrade --install polyad charts/polyad --namespace polyad \
  --set dragonfly.enabled=false --set dragonfly.existingSecret=polyad-dragonfly
```

Install one bundled Dragonfly controller per cluster. Other Polyad releases may
share its primary endpoint; queue keys include the Polyad namespace. See the
[chart guide](../charts/polyad/README.md) for CRD upgrades and migration from the
previous hand-written StatefulSet.

Operator replicas use `polyad.cache.Cache` for bounded Redis/Dragonfly
connections. The shared queue uses that same pool for streams and duplicate
notification caches; reusable JSON cache entries require explicit TTLs and are
namespaced. Cache data is transient. Graph intent, status and delay deadlines
remain authoritative in Kubernetes and are never replaced with cached intent.
The runtime `redis` dependency supplies the client; the chart's Dragonfly
operator dependency supplies an optional server with HA.

For local development, set `POLYAD_CACHE_URL` (default
`redis://localhost:6379/0`). The legacy `POLYAD_DRAGONFLY_URL` remains a fallback.
Use `rediss://` for TLS and a Secret-backed URL for authentication. The chart
sets `POLYAD_CACHE_URL` from its managed endpoint, external URL or Secret.
Kubernetes credentials resolve from the pod's service
account in-cluster and fall back to the active kubeconfig outside the cluster.
All replicas serving a namespace must use the same Redis-compatible database. Install
one Polyad release per namespace; its replicas share the fixed coordination names.

Enable CPU-based HPA with:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad \
  --set operator.autoscaling.enabled=true --set operator.autoscaling.minReplicas=2 \
  --set operator.autoscaling.maxReplicas=8
```

HPA requires the cluster metrics API and operator CPU requests. Without HPA,
`operator.replicaCount` controls scale. New replicas receive shards automatically; scale-down
allows their leases to expire. At most 32 replicas can own useful shards. CPU HPA
does not directly measure queue backlog. The default cache is a single availability
dependency; enable Dragonfly HA to recover automatically from a primary failure.

Both HPA stabilization windows and scaling rate policies are configurable through
`operator.autoscaling.behavior`. Rescan, queue consumption and telemetry periods
are available under `operator.tuning`. See [performance tuning](performance.md)
for defaults, bounds and the equivalent KEDA configuration.

References: [Kubernetes Leases](https://kubernetes.io/docs/concepts/architecture/leases/),
[Dragonfly pending-message recovery](https://www.dragonflydb.io/docs/command-reference/stream/xautoclaim).

## Build, install and exercise

```sh
docker build -t polyad:dev .
# Push to your registry, or load into your local test cluster.
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm dependency build charts/polyad
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set operator.image.repository=YOUR_REGISTRY/polyad --set operator.image.tag=dev
kubectl -n polyad apply -f examples/finite.yaml
kubectl -n polyad get graphs -o yaml
kubectl -n polyad apply -f examples/persistent.yaml
kubectl -n polyad delete graph persistent
```

The default image name is a publication target, not an assertion that an image is published. Build and supply an image first. Helm installs CRDs from `crds/` but does not upgrade or remove them automatically; review and apply CRD schema changes separately before a chart upgrade. Removing the operator while graphs still have finalizers prevents their cleanup until the operator returns.

CI runs Python checks, uses `astrivant/hypothesis-helm` to property-test the Helm chart, and exercises real lifecycle behavior in a disposable kind cluster. The operator does not require hypothesis-helm at runtime.

References: [Kopf embedding](https://docs.kopf.dev/en/stable/embedding/), [Kopf health probes](https://docs.kopf.dev/en/stable/probing/), [Kubernetes finalizers](https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/).

## Structural policy and composition API

`GraphRule` applies namespace-wide or inherited referenced constraints to graph
structure, recursive size and spectra before admission. `Composition` records an
immutable ID-addressed request, materializes reusable definitions and a root through
the leased queues, and records generated manifest identities. The optional Flask
service uses `APIBuilder` and a dedicated Waitress thread inside the Python operator.
See [graph rules and constraint diagrams](graph-rules.md),
[composition request format and audit semantics](composition-requests.md), and
[composition service setup](composition-api.md).
