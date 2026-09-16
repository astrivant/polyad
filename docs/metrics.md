# Scheduler metrics API

Polyad exposes queue pressure, tracked objects and graph hierarchy observations
through a dedicated, optional HTTP service. It uses the
[Prometheus Python client](https://prometheus.github.io/client_python/)
for `/metrics`; `/v1/metrics` provides the corresponding JSON snapshot, including
parent/root identities and graph status. `/openapi.json` describes the HTTP API.

## Enable and scrape

```yaml
metrics:
  enabled: true
  graphLabels: false
```

The Helm chart creates `<release>-polyad-metrics`, a ClusterIP Service on port
8092. This listener runs in its own Python thread with bounded HTTP workers.
It is independent of composition intake, event subscriptions and Kopf health
probes. Outside Helm, set `POLYAD_METRICS_ENABLED=true`; optionally set
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

## Counts and scope

Every Prometheus series includes `namespace` and `replica`. Additional labels
come from fixed categories; workload labels, tags, request IDs and UIDs are not
copied into Prometheus labels.

| Metric | Meaning and additional labels |
| --- | --- |
| `polyad_inbound_updates` | Shared stream backlog by `shard` and `state`: `queued` or `unacknowledged` |
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

Writers are `workloads`, `coordination` and `compositionIntake`. Queue entries
are refresh notifications: several can refer to the same object, and an
unacknowledged entry can also be undergoing reconciliation. **Do not add inbound,
local refresh and API write counts together as independent work.**

The inventory includes Graph, PolyGraph, ReplicaGroup, Rewrite,
Composition, Activation, ReplicaGroup, Workload, Daemon, Resource, Gate, ShutdownPolicy and
GraphRule CRs. Reusable definitions count separately from instances. Definition
references are not ownership links. Direct resources come from graph status,
not a cluster-wide Pod or workload census; they include graph child CRs and
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

## Freshness and failures

By default, the operator publishes snapshots every five seconds. HTTP reads use cached
bytes and never contact Kubernetes or Dragonfly. API write gauges are sampled,
so short bursts between samples may not appear.

Shared queues are sampled independently every five seconds by default. A failed sample or
one older than fifteen seconds suppresses the actionable Prometheus backlog
series. Inventory updates replace the previous snapshot only after a complete
namespace scan; failed scans or samples older than thirty seconds (measured
from the start of the scan) suppress
inventory-derived Prometheus series. Lists across kinds are eventually
consistent, not a transactional snapshot of the whole namespace.

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
do not silently become zero demand. Keep at least one operator replica to
consume notifications and emit telemetry. Disable the chart's CPU HPA before
letting a KEDA ScaledObject manage the same Deployment. The scheduler currently
has 32 logical shards; one graph family remains serialized, so adding replicas
cannot speed up a single busy family. Kubernetes API saturation can also worsen
with more writers. [KEDA Prometheus scaler](https://keda.sh/docs/2.20/scalers/prometheus/)

For workload scaling, [ReplicaGroup and the workload metrics endpoint](replication.md)
provide a bounded Kubernetes scale target for services and entire graphs. KEDA
is installed separately.

See [per-workload metric scopes and freshness](replication.md#metric-scopes-and-freshness)
for scalar KEDA endpoints and `polyad_workload_signal` series.

## Central reports across clusters

[Root mode](root-control-plane.md) gathers cluster-qualified inventories, topology
streams and queue demand in root storage. Its metrics API accepts `?cluster=NAME`
for workload scalar queries. See [root reports and freshness](root-control-plane.md#reports-disconnection-and-deletion)
and [root-local KEDA targets](root-control-plane.md#keda-from-the-root) before scaling
remote workloads or execution replicas.
