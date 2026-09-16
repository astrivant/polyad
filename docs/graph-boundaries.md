# Graph boundaries and migration

Polyad has three graph boundary types:

| Type | Purpose |
| --- | --- |
| `Graph` | Combine workloads, daemons, resources and nested boundaries |
| `PolyGraph` | Compose graph boundaries under shared placement, rules and lifecycle |
| `ReplicaGroup` | Scale copies of a reusable definition through the Kubernetes scale API |

All three participate in ownership, live GraphRule evaluation and recursive
status. StatefulSet and Deployment execution remain choices on Daemon definitions.

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
placement does not impose an inherited storage prohibition. The explicit
`Ephemeral` workload type remains available and retains its per-workload
placement and storage restrictions. See [storage configuration](workload-storage.md).

## Retired alpha types

`Feedback`, `EphemeralGraph`, and the local Python `FeedbackGraph` helper have
been removed. There are no compatibility aliases. Replace an EphemeralGraph
manifest with a Graph, retaining its placement. Replace Feedback's nested
`spec.graph` with a reusable Graph definition and select an activation policy
on that definition. Round counters and termination conditions move into the
application; they are not automatically migrated.

For an existing cluster, drain and delete instances of the retired kinds while
the previous operator still manages their finalizers, then upgrade the operator
and apply the revised CRDs and manifests. Helm does not automatically remove
previously installed CRDs from its `crds/` directory when chart files disappear.
Remove those old CRDs only after their instances and cleanup have finished.
Existing Python imports, network peer selectors, rewrite targets, composition
requests and replica templates must use the supported kinds above.
