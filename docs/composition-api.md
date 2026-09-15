# Structural policies and composition requests

Polyad separates engineer-owned constraints from end-user composition intent.
A `GraphRule` is namespace-scoped. Its default `enforcement: Namespace` applies
to every executable boundary, including graphs created directly through Kubernetes.
`enforcement: Referenced` opts a rule into selection through `spec.rules` on a graph;
selected rules are inherited by descendants. All selected rules must pass.

Only policy administrators should have write access to `graphrules`. The operator's
Role grants read access. The HTTP API cannot create or modify rules. Its bearer
credential authorizes workload creation and audit reads in the operator namespace;
it is not a per-user identity or a tenant isolation mechanism. Apply normal workload
RBAC, Pod Security and resource quotas as well.

## Mathematical constraints

Each rule selects one directed relation: `admission` dependencies or `connections`.
Isolated vertices count. Duplicate edges collapse; self-loops count in directed
edge/degree measurements. Boundaries are measured independently, with explicit
recursive limits to prevent nesting from hiding total size.

| Field under `spec.limits` | Inclusive maximum |
| --- | --- |
| `nodes`, `edges` | Vertices and distinct directed edges in a boundary |
| `depth`, `breadth` | Number of layers and largest earliest topological layer in the SCC condensation DAG |
| `fanIn`, `fanOut` | Maximum directed in-degree and out-degree |
| `strongComponent` | Largest strongly connected component (SCC) |
| `cycleRank` | Independent undirected cycles, `m - n + c`, on the simple projection |
| `expandedNodes` | Recursive vertex occurrences, including graph vertices and each repeated subgraph instance |
| `nestingDepth` | Nested boundary levels, including this boundary as level one |

An SCC groups vertices that can reach one another. Collapsing each SCC yields an
acyclic condensation graph. Admission is already acyclic, so each of its SCCs has
one vertex. Breadth measures a layer, not a maximum antichain or guaranteed parallel
capacity. These measurements describe topology, not CPU time or algorithmic cost.

`shapes` may require `acyclic` on the directed relation, or `connected`, `tree`, and
`planar` on its simple undirected projection. That projection drops self-loops,
merges opposite edges and ignores weights. Thus a directed two-node cycle can have
undirected cycle rank zero; use `acyclic` or `strongComponent` to constrain directed
recurrence.

Spectral rules use this same undirected, unweighted projection. For adjacency
matrix `A` and degree matrix `D`, the combinatorial Laplacian is `L = D - A`:

| Field under `spec.spectrum` | Constraint |
| --- | --- |
| `maxRadius` | Maximum absolute eigenvalue of `A` |
| `minConnectivity` | Minimum second-smallest eigenvalue of `L`, or zero for fewer than two vertices |
| `maxLaplacian` | Maximum eigenvalue of `L` |

A directed DAG's adjacency eigenvalues are all zero, so directly applying those
eigenvalues would not distinguish pipeline shapes. The undirected projection does.
A disconnected projection has algebraic connectivity zero. NetworkX documents the
[graph matrix and spectral definitions](https://networkx.org/documentation/stable/reference/linalg.html);
Polyad computes symmetric spectra with NumPy's
[`eigvalsh`](https://numpy.org/doc/stable/reference/generated/numpy.linalg.eigvalsh.html).

Spectral comparisons allow `1e-9 * max(1, |measured|, |bound|)` numerical tolerance.
A spectral rule supports at most 256 vertices per boundary. Admission preflight is
bounded to 4,096 expanded node occurrences, 256 boundaries, 32 nesting levels and
32 GraphRules per namespace. Eigenvalue calculations run outside the operator's
event loop so lease renewal remains responsive. Feedback counts one epoch template;
these limits do not bound the total work of an indefinitely recurring graph.

```yaml
apiVersion: polyad.astrivant.com/v1alpha1
kind: GraphRule
metadata:
  name: namespace-budget
spec:
  enforcement: Namespace
  relation: admission
  limits:
    nodes: 64
    edges: 128
    depth: 12
    breadth: 16
    expandedNodes: 256
    nestingDepth: 8
  spectrum:
    maxRadius: 8
---
apiVersion: polyad.astrivant.com/v1alpha1
kind: GraphRule
metadata:
  name: connected-flow
spec:
  enforcement: Referenced
  relation: connections
  shapes: [connected]
  spectrum:
    minConnectivity: 0.1
```

Select the second rule using `spec.rules: [connected-flow]`. Namespace rules still
apply. Rules are refreshed before admission, including nested definitions and
rewrite targets. A violation reports phase `Invalid` with the rule and explanation;
successful graph observations include `status.structuralRules` with rule UIDs,
generations, measurements and compact spectral summaries. Python `graph_spectrum`
also returns the full sorted spectra.

Policy updates take effect on subsequent reconciliations. They block further
admission; they do not evict existing workloads. Kubernetes reads across different
objects are not an atomic snapshot. These are scheduler admission constraints,
not a validating webhook for arbitrary native Pods created outside Polyad.

## Enable the service

Create a Secret containing a `token` key in the operator namespace, then install
with `api.enabled=true` and `api.existingSecret=<secret-name>`. The chart exposes
port 8090 through `<release>-polyad-api`, a ClusterIP Service. Keep that endpoint
private; terminate TLS at your ingress or gateway if exposing it beyond the cluster.
The health endpoint remains Kopf's existing port 8080.

The Python process starts Kopf on its existing operator thread. Startup constructs
the Flask application with `APIBuilder`, then hosts it on a dedicated Waitress
thread. Waitress is a [production WSGI server supported by Flask](https://flask.palletsprojects.com/en/stable/deploying/waitress/).
There is no Flask development server and no additional command in the Dockerfile.

Build an application with custom synchronous adapters:

```python
from polyad.api import APIBuilder

app = (
    APIBuilder()
    .with_handlers(submit=submit_receipt, lookup=lookup_receipt)
    .with_bearer_token(namespace_token)
    .with_metadata("Team workload scheduler", "v1alpha1")
    .build()
)
```

`submit_receipt(request)` accepts an attrs `CompositionRequest` and returns a JSON
receipt. `lookup_receipt(request_id, audit)` returns a JSON result or `None`.
The builder is immutable: methods return a new configuration, and each `build()`
creates a separate application. Missing handlers or an empty token fail at build
time. `create_app(...)` is a convenience wrapper around the same builder.
In the operator, adapters bridge to `CompositionStore` on Kopf's event loop.

## OpenAPI schema

The service serves an OpenAPI 3.1 document at `GET /openapi.json`, generated with
[`apispec`](https://apispec.readthedocs.io/en/stable/quickstart.html). It uses the same
bearer authentication as the composition routes:

```sh
curl --fail-with-body -H "Authorization: Bearer $POLYAD_API_TOKEN" \
  http://localhost:8090/openapi.json
```

The schema describes ID-based composition nodes, dependencies and connections,
receipt/status/audit responses, error codes and bearer authentication. Native
workload definition payloads remain extensible objects validated by their CRDs
and the operator. Cross-reference resolution and graph policy checks are semantic
constraints beyond OpenAPI's structural request schema.

`APIBuilder.with_metadata(title, version)` sets the document's service identity.
Every built service exposes the endpoint; no additional Helm setting or process is
needed. The test suite checks OpenAPI validity, route coverage, and request/response
conformance using the development `openapi-spec-validator` dependency.

## Compose by ID

Submit [the nested composition example](../examples/composition.json):

```sh
curl --fail-with-body http://localhost:8090/v1/compositions \
  -H "Authorization: Bearer $POLYAD_API_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @examples/composition.json
```

A request contains `requestId`, `rootId`, and `objects`. Each object has its own
`id`, supported `kind`, and `spec`. The root must be a Graph, PolyGraph,
EphemeralGraph or Feedback. All definitions must be reachable from the root;
references between definitions cannot recurse.

Inside graph specifications, use:

- `nodes[].id` for an instance vertex and `nodes[].refId` for its definition.
- `requires[].nodeId` for another vertex in the same boundary.
- `connections[].sourceId` and `targetId` for data-flow edges.
- `nodes[].gateId` and `shutdownPolicyId` for supplied reusable definitions.
- `rules` for administrator-defined GraphRule names in the operator namespace.

A graph definition can be referenced by several vertices. Each reference gets an
independent execution instance and audit path. Graph placement, delays, persistence
and ephemeral restrictions retain their existing meanings. Feedback's `graph`
contains an epoch topology in the same ID-based format. IDs are lowercase DNS
labels of up to 63 characters; requests permit 1–128 definitions and a 1 MiB body.

## Durability, ordering and audit

HTTP `202` means Kubernetes accepted a durable `Composition` receipt, not that its
workloads passed admission or started. Invalid JSON returns `400`, invalid
compositions `422`, missing authentication `401`, and conflicting request ID reuse
`409`. A `503` or lost response has an uncertain outcome: retry the **same requestId
and content**. Reordering the definitions does not change their canonical digest.
Different work requires a new request ID. Idempotency lasts for the receipt's
lifetime; deleting and recreating it starts a new run with a new Kubernetes UID.

Receipt writes use a serialized Kubernetes adapter. Concurrent replicas converge
through deterministic names and Kubernetes create-if-absent semantics. Receipts,
their definitions and executable descendants share one family shard. Shared
Dragonfly queues and Kubernetes Leases govern all graph materialization. A worker
preflights rules, creates reusable templates, observes their acknowledgements on a
later pass, and only then creates the executable root. Finalization drains that
root before deleting templates.

The CR stores `spec.requestId` and `spec.document`, canonical JSON text representing
the request. CEL enforces immutability of both fields. This representation is
intentional: Kubernetes cannot inspect arbitrary preserved JSON fields in
[CRD CEL expressions](https://kubernetes.io/docs/tasks/extend-kubernetes/custom-resources/custom-resource-definitions/).
Python `receipt_spec(request)` produces this representation for direct manifest
creation; `request_name(request.requestId)` supplies the required deterministic
metadata name. API clients continue to send ordinary JSON objects.

Read current status and generated resource identities:

```sh
curl --fail-with-body -H "Authorization: Bearer $POLYAD_API_TOKEN" \
  http://localhost:8090/v1/compositions/example-analysis
curl --fail-with-body -H "Authorization: Bearer $POLYAD_API_TOKEN" \
  http://localhost:8090/v1/compositions/example-analysis/resources
```

The resource endpoint returns fresh names, namespaces, UIDs, owner references and
trace annotations. Results are bounded to 200 resources per kind and report
`truncated` when more exist. Request, object and node IDs link intent to manifests;
`composition-uid`, `definition-uid`, `definition-generation` and `node-path`
disambiguate receipt lifetimes, source revisions and repeated graph instances.
Jobs and Deployments copy provenance into Pod templates, so their Pods are
queryable through the same endpoint. This is live resource lineage, not an
append-only audit log: deleted resources require Kubernetes audit logging or an
external event archive for historical lookup.

Intake is bounded to 32 outstanding operations per replica. A timed-out write
retains its slot until acknowledgement; shutdown joins outstanding operations.
The existing Kopf probe includes intake writes in
`backlog.kubernetesWrites.compositionIntake`. A failed HTTP thread fails the
operator healthcheck.
