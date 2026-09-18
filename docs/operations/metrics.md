# Scheduler metrics API

Polyad exposes queue pressure, tracked objects and graph hierarchy observations
through a dedicated, optional HTTP service. It uses the
[Prometheus Python client](https://prometheus.github.io/client_python/)
for `/metrics`; `/v1/metrics` provides the corresponding JSON snapshot, including
parent/root identities and graph status. `/openapi.json` describes the HTTP API.

## Table of contents

- [Enable and scrape](#enable-and-scrape)
- [Metric inventory and scope](#metric-inventory-and-scope)
- [Graph diagnostics for benchmarks](#graph-diagnostics-for-benchmarks)
- [Freshness and failures](#freshness-and-failures)
- [Operator scaling with KEDA](#operator-scaling-with-keda)
- [Central reports across clusters](#central-reports-across-clusters)

## Enable and scrape

```yaml
metrics:
  enabled: true
  graphLabels: false
```

The Helm chart creates `<release>-polyad-metrics`, a ClusterIP Service on port
8092. Its routes use the operator's [shared Flask application and Waitress runtime](../apis/composition-api.md),
with endpoint-specific authentication and reserved worker capacity alongside event
subscriptions. Kopf health probes retain their own server. Outside Helm, set `POLYAD_METRICS_ENABLED=true`; optionally set
`POLYAD_METRICS_GRAPH_LABELS=true`.

Enable `metrics.authentication.enabled` to require a dedicated bearer token on
every metrics route, including OpenAPI. The chart can create KEDA
`TriggerAuthentication` and ESO `ExternalSecret` resources; see
[metrics authentication and external credentials](authentication.md).
Authentication is disabled by default. Treat object names and hierarchy
membership as internal operational data. If NetworkPolicies are
enabled, allow monitoring clients with `networkPolicy.metricsPeers`. If the
operator uses Istio, authorize the scraper's mTLS identity through
`mesh.operator.metricsPrincipals`. Both controls apply when both are enabled.
The chart does not expose metrics through its external API gateways.

Scrape **each operator Pod**, using the Service's discovered endpoints rather
than one load-balanced Service address. For an existing Prometheus Operator,
this optional resource selects the metrics Service (adjust release, namespace
and metadata labels to match your Prometheus installation):

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: polyad
  namespace: monitoring
spec:
  namespaceSelector:
    matchNames: [polyad]
  selector:
    matchLabels:
      app.kubernetes.io/name: polyad
      app.kubernetes.io/instance: polyad
      app.kubernetes.io/component: metrics
  endpoints:
    - port: metrics
      path: /metrics
      interval: 15s
```

The [ServiceMonitor CRD](https://prometheus-operator.dev/docs/api-reference/api/#monitoring.coreos.com/v1.ServiceMonitor)
and Prometheus are supplied by your monitoring stack,
not by this chart. A meshed operator also requires a compatible mTLS scrape
configuration.

With bearer authentication enabled, add `authorization` to the ServiceMonitor
endpoint, referencing a Secret **in the ServiceMonitor's namespace**:

```yaml
authorization:
  type: Bearer
  credentials:
    name: polyad-metrics
    key: token
```

If Prometheus is in another namespace, use ESO there to populate a Secret from
the same provider entry. The chart does not copy credentials across namespaces.

## Metric inventory and scope

Every Prometheus series includes `namespace` and `replica`. Additional labels
come from fixed categories; workload labels, tags, request IDs and UIDs are not
copied into Prometheus labels.

All emitted families are gauges. The table inventories the current exporter;
enabling a backend does not create its infrastructure merely because metrics are
enabled. `metrics.graphLabels` only gates the per-object families listed below.
See the [typed tuning reference](../../charts/polyad/values-tuning.reference.yaml)
for values and the signals to watch when changing them.

| Metric | Meaning and additional labels |
| --- | --- |
| `polyad_snapshot_timestamp_seconds` | Unix publication timestamp; emitted for every snapshot |
| `polyad_operator_interval_seconds` | Effective process pause by `loop`: rescan, consume, metrics or backlog; configured interval, not measured duration |
| `polyad_inbound_updates` | Shared stream backlog by `shard` and `state`: `queued` or `unacknowledged` |
| `polyad_inbound_sample_fresh` | Whether the local shared-queue sample is actionable |
| `polyad_inbound_sample_age_seconds` | Age of the last local shared-queue sample; absent until one exists |
| `polyad_inventory_sample_fresh` | Whether a complete local namespace inventory is current |
| `polyad_inventory_sample_age_seconds` | Age measured from the start of the last complete local inventory scan; absent until one exists |
| `polyad_refresh_queue_entries` | Replica-local waiting reconciliation keys; excludes the active attempt |
| `polyad_kubernetes_writes_queued` | Concrete API writes waiting for dispatch by `writer` |
| `polyad_kubernetes_writes_in_flight` | Dispatched writes awaiting transport completion by `writer` |
| `polyad_kubernetes_writes_oldest_queued_seconds` | Age of the oldest waiting write by `writer` |
| `polyad_kubernetes_writes_oldest_in_flight_seconds` | Age of the oldest outstanding transport by `writer` |
| `polyad_tracked_objects` | Namespace CR inventory by `kind` and `role`: definition or instance |
| `polyad_tracked_objects_total_count` | Total namespace CR inventory, including explicit zero for an empty scan |
| `polyad_shard_objects` | CR inventory by root-family `shard`, `kind` and lifecycle `phase` |
| `polyad_observed_resources` | Sum of direct owned resources in current-generation graph status, by `kind` |
| `polyad_graph_status_observations` | Instance graphs with `current` or `unknown` status observations |
| `polyad_owned_shards` / `polyad_leader` | Local shard assignment count and planner leadership flag |
| `polyad_connection_pool_in_use` / `polyad_connection_pool_limit` / `polyad_connection_pool_waiting` | Local process pool checkouts, configured capacity and PostgreSQL waiters, labeled by fixed `pool` category; idle sockets excluded from demand |
| `polyad_component_connection_pool_in_use` / `polyad_component_connection_pool_limit` / `polyad_component_connection_pool_waiting` | Fresh pool totals across local processes by `component` and `pool`; includes split components, excludes remote workers; deduplicate scrape replicas |
| `polyad_component_connection_pressure` | Sum of each local component process's busiest pool fraction; fresh reports only, deduplicate scrape replicas; remote workers excluded from local scaling |
| `polyad_postgresql_connections` | Global primary connections from this operator state scope; available when optional PostgreSQL storage is enabled |
| `polyad_dragonfly_connections` / `polyad_dragonfly_sample_fresh` | Primary connected clients and sample freshness for bundled HA cache autoscaling; deduplicate metrics replicas |
| `polyad_postgresql_sample_fresh` / `polyad_postgresql_state_fresh` | Freshness of the connection sample and this process's persisted inventory |
| `polyad_component_requests_per_second` / `polyad_component_requests_in_flight` | Global HTTP arrival rate and open responses by component, including event streams |
| `polyad_component_sample_fresh` | Whether all recent component process reports are available |
| `polyad_component_reporting_replicas` | Fresh reporting processes by `component`; an observed count, not the desired Deployment replicas |
| `polyad_worker_sample_fresh` | Root-held heartbeat availability by `worker` process identity |
| `polyad_worker_writes_queued` / `polyad_worker_writes_in_flight` | Root-held worker write pressure by `worker`; absent for stale reports |
| `polyad_worker_refresh_queue_entries` | Worker-local waiting reconciliation keys by `worker`; excludes active attempts and is absent for stale reports |
| `polyad_cluster_inventory_sample_fresh` | Root-held remote inventory freshness by `cluster` |
| `polyad_cluster_inbound_sample_fresh` | Root-held remote backlog freshness by `cluster`, independently of inventory |
| `polyad_cluster_inbound_updates` | Remote backlog by `cluster`, `shard` and `state`; requires fresh inventory and backlog |
| `polyad_hierarchy_info` | Object membership in parent/root hierarchy; requires fresh inventory and `metrics.graphLabels` |
| `polyad_graph_status_current` | Whether observed graph metrics match spec generation; requires fresh inventory and `metrics.graphLabels` |
| `polyad_graph_direct_resources` | Current instance resources by `resource_kind`, plus hierarchy labels; requires `metrics.graphLabels` |
| `polyad_graph_shape` | Current topology by `dimension` (nodes, edgeCount, breadth, depth), plus hierarchy labels; requires `metrics.graphLabels` |
| `polyad_workload_signal` | Fresh local numeric signals by `kind`, `name`, `node`, `signal`; requires `metrics.graphLabels` |
| `polyad_cluster_workload_signal` | Fresh remote numeric signals with an additional `cluster` label; requires root reports and `metrics.graphLabels` |

PostgreSQL families require optional state storage; their connection count covers
state-database sessions with this control plane's application identity, not every
database connection or the separate authentication database. Dragonfly families
require the bundled HA pool connection sampler. Component demand and reporting
counts are omitted when aggregate reports are stale. Root worker/cluster families
exist only for reported workers and registered clusters. Root-held worker pressure
duplicates that worker's local gauges: use one view for aggregation.

The effective interval series describes the **metrics-serving process**. In split
deployments it describes the telemetry component. Each worker uses its own
configured intervals. The JSON snapshot exposes the same values in `tuning`. Readiness and
ownership cadence, Prometheus scrape intervals, HPA synchronization and trace
batching are separate controls.

The JSON snapshot also includes `workGraph`, with the effective
`operator.writeQueue` limits for that process. Root mode collects each reporting
operator's limits in `workers[identity].workGraph`; use only fresh worker reports.
These configuration values are separate from the backlog gauges and do not
represent measured active workers. See [work-graph configuration](../development/write-pipeline.md#configuration).

Writers are `workloads`, `coordination` and `apiIntake`. Queue entries
are refresh notifications: several can refer to the same object, and an
unacknowledged entry can also be undergoing reconciliation. **Do not add inbound,
local refresh and API write counts together as independent work.**

The metrics-serving inventory includes Graph, PolyGraph, ReplicaGroup, Rewrite,
Composition, Activation, TemporaryConnection, Workload, Daemon, Resource, Gate,
ShutdownPolicy and GraphRule CRs. Root mode also scans OperatorPool and RemoteScale.
Reusable definitions count separately from instances. Definition
references are not ownership links. Direct resources come from graph status
and include graph child CRs and
operator-owned helpers. Do not add these to the CR inventory or to recursive
subtree resource totals.

The JSON `inventory.objects` records include UID-fenced parent/root identities,
nesting depth, shard, phase, termination, generation freshness, direct resources,
execution, topology and recursive rollups. These are namespace-local controller
hierarchies. Missing parents, UID mismatches and cycles produce incomplete
hierarchies, not invented roots. Rewrite duties use their target graph's shard.
Non-graph definitions have no reconciliation shard.

For per-object Prometheus series, enable `metrics.graphLabels`. This adds
`polyad_hierarchy_info`, `polyad_graph_direct_resources`, `polyad_graph_shape`
and `polyad_graph_status_current`, labelled by kind/name, parent, root, role and
shard. Shape dimensions are declared node count, admission edge count, breadth
and depth. Object names increase time-series cardinality, particularly with
short-lived activation runs; leave this off when aggregate metrics suffice.
The JSON hierarchy remains available either way.

The scalar routes are available independently of `metrics.graphLabels`:

| Route | Source and required feature |
| --- | --- |
| `/v1/workloads/{kind}/{name}/{metric}` | Fresh controller workload or replica observations; `?node=` selects a logical node, `?cluster=` selects a root-held remote inventory |
| `/v1/components/executor/backlog` | Fresh local and registered remote inbound queues |
| `/v1/components/{component}/{metric}` | Fresh gateway/telemetry `requestsPerSecond` or `inFlight` reports |
| `/v1/components/{component}/connectionPressure` | Sum of fresh local process pool-pressure fractions for dense/bootstrap/gateway/executor/telemetry; KEDA uses a fractional per-replica target |
| `/v1/postgresql/connections` | Optional state database connection sampler |
| `/v1/dragonfly/connections` | Bundled HA Dragonfly primary connection sampler |

CPU and memory utilization for the operator HPA come from Kubernetes resource
metrics. Graph calculations and Soul searching observations are exported from
`status.structuralRules` and `status.throughput`, as described below. OpenTelemetry
also exports [traces and decision logs](tracing.md).

## Graph diagnostics for benchmarks

Enable the following on the **operator chart**. The Terraform and load-study
operator profiles already do this:

```yaml
metrics:
  enabled: true
  graphLabels: true
  graphSpectra: true
```

Every metrics-serving operator replica publishes the same families from its
cached namespace inventory. Roots also export their fresh remote inventories
with a `cluster` label; local observations use `cluster=""`. Other labels identify
`graph_namespace`, `kind`, `name`, parent and root. These cover **Graph, PolyGraph
and ReplicaGroup** boundaries. Separate replicas may briefly observe different
revisions: deduplicate equivalent observations with `max by (...)` so each graph
contributes once. Preserve `cluster`, graph identity and the
family's rule/relation/stage labels when doing so.

| Metric | Calculated values and inputs |
| --- | --- |
| `polyad_graph_topology` | All numeric topology fields by `view` (`declared` or `observed`) and `dimension`: node/kind/subgraph counts, admission edges/depth/breadth/layer widths/fan-in/fan-out/roots/leaves, connection components and condensation layers |
| `polyad_graph_execution` | Numeric execution and recursive rollup fields by `scope` and `dimension`; do not add direct counts to rollups |
| `polyad_graph_rule_current` | Whether the saved admission report matches graph generation plus current rule UID/generation; zero suppresses its measured-value series |
| `polyad_graph_rule_allowed` | Verdict of the most recent successful admission report, by `rule` and `relation`; rejected attempts remain decision logs, not newly admitted state |
| `polyad_graph_rule_measurement` | Every calculated structural dimension: vertices, edges, depth, breadth, fan-in/out, cycle rank, largest strong component, expanded nodes, nesting depth and exact Cheeger when available |
| `polyad_graph_rule_parameter` | Numeric configured size limits, hard Cheeger bounds and spectral bounds; unset bounds are omitted |
| `polyad_graph_rule_shape` | Calculated acyclic/connected/tree predicates, and planarity when requested |
| `polyad_graph_spectrum` | Adjacency radius, algebraic connectivity and largest Laplacian eigenvalue by `statistic` |
| `polyad_graph_eigenvalue` | Full sorted adjacency/Laplacian eigenvalues by `matrix` and zero-based `index`, when `graphSpectra` retained them |
| `polyad_graph_cheeger_input` | Effective `maxVertices`, `maxCuts`, `timeoutSeconds`, operator ceilings, projected vertices/edges and configured priority-cut/vertex counts, by `parameter` |
| `polyad_graph_cheeger_result` | `exact`, `constant` (exact only), witnessed `upperBound`, `evaluatedCuts`, `skippedPriorityCuts`, `durationSeconds` and witness `cutSize`, by `statistic` |
| `polyad_graph_cheeger_calculation_info` | Completion reason and projection; `Complete`, `MinimumViolated`, `VertexLimit`, `CutBudget` or `TimeBudget` |
| `polyad_graph_throughput` | Soul searching numeric observations, application Cheeger targets, proposed Cheeger, demand/throughput, traffic weights and capacity preparation values |
| `polyad_graph_throughput_info` | Soul searching `mode`, `phase`, demand signal and unit |

Cheeger families distinguish `source="rule"` (hard policy) from
`source="throughput"` (Soul searching), plus `stage="current"` or `"candidate"`
and candidate `layout`. The [two bounds guide](../graphs/cheeger-orchestration.md)
explains their different roles. An incomplete calculation **never** emits an
exact `constant`: its best observed cut only proves an upper bound. Vertex-limit
rejections may have no measured edges, cut or upper bound. Duration measures the
latest calculation's wall time.

These endpoints expose **already calculated** observations. HTTP reads do not
run eigensolvers or enumerate cuts, and `graphSpectra` does not turn spectral
rules on. To calculate spectra, configure `spectrum: {}` on a selected
[GraphRule](../graphs/graph-rules.md#spectral-bounds); this requests measurement
without adding a spectral bound. The load fixture's `load-envelope` rule includes
it. Spectral calculations retain the existing 256-vertex limit. Full eigenvalues
add two series per vertex per spectral rule and increase status/snapshot size;
disable `graphSpectra` when summary values suffice.

Admission measurements describe the last successfully checked live boundary;
`declared`/`observed` topology counts come from ordinary lifecycle observations.
They need not describe identical vertices while activation instances are changing.
A fresh inventory and matching generations do not make measurements an atomic
cluster snapshot. Stale/missing inventory, changed rule identity or graph
generation suppress applicable series until fresh data is available. Soul searching
carries its own evaluated generation; expired application samples are cleared by
its existing reconciliation policy. Older operator versions' reports without
these fences are withheld until recomputed.

`/v1/metrics` includes the complete precomputed reports under
`inventory.objects[].structuralRules`, plus `throughput` and `observedTopology`.
Reports include rule identities, selected parameters, witness vertex names and,
when retained, eigenvalue arrays. Detailed names and witness cuts stay in JSON,
not Prometheus labels. Array-valued numeric paths use zero-based indices; consult
JSON for the corresponding named traffic destinations. Remote copies appear under
`clusters[cluster].inventory.objects`. Existing metrics authentication and network
policies protect both endpoints.

For example, chart exact structural Cheeger values without combining replicas:

```promql
max by (cluster, graph_namespace, kind, name, rule, relation) (
  polyad_graph_cheeger_result{source="rule",stage="current",statistic="constant"}
)
```

The [benchmark dashboard](../../studies/load/README.md#monitoring-and-traces)
includes topology, Cheeger values/bounds, search work and spectra. Missing series
mean uncalculated, unavailable or stale; do not substitute zero for them.

## Freshness and failures

By default, the operator publishes snapshots every five seconds. HTTP reads use cached
bytes and never contact Kubernetes or Dragonfly. API write gauges are sampled,
so short bursts between samples may not appear.

The adapter [checks pending write conflicts](../development/mutations.md#queued-kubernetes-write-conflicts)
before dispatch. Rejected and cancelled requests leave the queued gauges when
their callers exit the queue; they never enter the in-flight gauges. A dispatched
request remains in flight until transport finishes, even after cancellation.
Use the structured decision logs to distinguish conflicts from API latency;
the gauges measure pressure, not conflict totals.

[Write admission](../development/write-pipeline.md#configuration) defaults to one
active validation/transport plus one waiter per adapter. Duplicate callers share
that entry. Active validation remains counted as queued until HTTP dispatch, so
`queued` can reach `maxPending + maxInFlight` with no in-flight mutation. Background dependency
GETs are reads, not additional write entries; their intervals and burst limits
are separate from KEDA metrics publication.

Shared queues are sampled independently every five seconds by default. A failed sample or
one older than fifteen seconds suppresses the actionable Prometheus backlog
series. Inventory updates replace the previous snapshot only after a complete
namespace scan; failed scans or samples older than thirty seconds (measured
from the start of the scan) suppress
inventory-derived Prometheus series. Lists across kinds are eventually
consistent and can reflect different observation times within the namespace.

Tune publication, backlog sampling and inventory rescans through
[`operator.tuning`](performance.md#worker-cadence). Changing the intervals does
not extend freshness deadlines or make unavailable demand count as zero.

`polyad_inbound_sample_fresh`, `polyad_inventory_sample_fresh` and their
`*_sample_age_seconds` companions expose these conditions. JSON retains the
last observations with `fresh: false`; consumers must check this flag.
Generation freshness is separate: `statusCurrent` means the status describes
the current spec generation, not that Kubernetes resources were just polled.
Descendant completeness is reported by each graph's rollup.

A replica with no published snapshot, publication stalled for fifteen seconds,
or replacement/draining signalled returns HTTP 503 for both data endpoints.
An exited metrics thread or publication task also fails the existing health
probe. No API/cache outage is represented as zero demand.

## Operator scaling with KEDA

Shared queue and inventory counts are observed by every replica. Deduplicate
replicas **before** summing shards. For example, namespace inbound demand:

```promql
sum(max by (namespace, shard, state) (
  polyad_inbound_updates{namespace="polyad"}
))
```

This conservatively takes the largest recent replica observation for each shard
and state; replica samples are not simultaneous. Scrape targets must keep the
exported namespace label, or adjust the selector to your relabeling convention.
For local write pressure, sum replicas directly:

```promql
sum(polyad_kubernetes_writes_queued{namespace="polyad"})
```

KEDA's Prometheus scaler expects a query returning one scalar/vector element.
When configuring it later, use the first query as a starting point, choose a
measured backlog threshold and set `ignoreNullValues: "false"` so missing samples
do not silently become zero demand. Use `ha: true` for operator autoscaling and
keep `minReplicaCount: 2` or higher to preserve its replica floor. Disable the chart's operator HPA before
letting a KEDA ScaledObject manage the same Deployment. The scheduler currently
has 32 logical shards; one graph family remains serialized, so adding replicas
cannot speed up a single busy family. Kubernetes API saturation can also worsen
with more writers. [KEDA Prometheus scaler](https://keda.sh/docs/2.20/scalers/prometheus/)

For workload scaling, [ReplicaGroup and the workload metrics endpoint](../graphs/replication.md)
provide a bounded Kubernetes scale target for services and entire graphs. Use
an existing KEDA installation or the [optional chart dependency](../deployment/local-services.md#install-keda-with-the-chart).

See [per-workload metric scopes and freshness](../graphs/replication.md#metric-scopes-and-freshness)
for scalar KEDA endpoints and `polyad_workload_signal` series.

## Central reports across clusters

[Distributed component deployments](../deployment/components.md) keep these APIs on the
telemetry Service, with the same URLs and authentication as dense deployments.
`/v1/components/{component}/{metric}` provides global queue or HTTP demand for
component KEDA targets. `/v1/postgresql/connections` provides the optional
[database scaler](../deployment/postgresql.md#scale-postgresql-with-operator-connection-counts)
with one global count. `/v1/dragonfly/connections` supplies primary client counts
for [bundled HA cache scaling](../deployment/dragonfly.md). These endpoints return HTTP 503 for
unavailable samples. All
collection runs in background loops; HTTP scrapes perform no database, cache
or Kubernetes I/O for measurement collection. Optional [named API keys](api-keys.md)
use shared-cache admission for each scrape. Deduplicate global values across HA
metrics replicas.

[Root mode](../deployment/root-control-plane.md) gathers cluster-qualified inventories, topology
streams and queue demand in root storage. Its metrics API accepts `?cluster=NAME`
for workload scalar queries. See [root reports and freshness](../deployment/root-control-plane.md#reports-disconnection-and-deletion)
and [root-local KEDA targets](../deployment/root-control-plane.md#keda-from-the-root) before scaling
remote workloads or execution replicas.
