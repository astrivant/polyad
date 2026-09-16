# Graph boundaries

Polyad has three graph boundary types:

| Type | Purpose |
| --- | --- |
| `Graph` | Combine workloads, daemons, resources and nested boundaries within one Kubernetes cluster |
| `PolyGraph` | Compose graph boundaries locally or across registered clusters, including nested compositions |
| `ReplicaGroup` | Scale copies of a reusable definition through the Kubernetes scale API |

All three participate in ownership, live GraphRule evaluation and recursive
status. StatefulSet and Deployment execution remain choices on Daemon definitions.

Cross-cluster placement, Istio transport and shared read-only observers are
independent optional extensions. PolyGraphs manage remote child Graph intent;
destination operators execute it and enforce local GraphRules. Observers share
state without participating in execution. See [multicluster configuration](multicluster.md).

## Repeated execution

Use a persistent Graph containing a finite Graph definition with an activation
policy. Producer requests or `onDeadline: Activate` timers start independent
instances. Queue mode serializes requests; parallel mode bounds concurrent runs.
See [the repeated graph example](../examples/repeated-graph.yaml) and
[activation policies](activation.md).

The application owns iteration counts, stop conditions and durable state.
`minIntervalSeconds` measures admission spacing; it does not reproduce a wait
measured from the previous completion. Local Python applications can run ordinary
Graphs from application control flow, persisting their own iteration cursor and
propagating pause and cancellation to active work.

## Placement and storage

Use regular Graph or PolyGraph `placement` for interruptible capacity. Users
choose appropriate node labels, tolerations, recovery behavior and storage;
placement does not impose a storage prohibition. Finite jobs use `Workload`,
including on spot nodes; persistent services use `Daemon`. See
[storage configuration](workload-storage.md) and the
[spot workload example](../examples/spot-workload.yaml).
