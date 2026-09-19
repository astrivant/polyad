# Polyad Helm chart

Deploy Polyad's graph orchestration control plane with a single operator replica,
HA replicas or separate gateway, executor and telemetry components. Configure
multicluster coordination, constrained autoscaling, demand-driven adaptation,
discovery and event APIs, and the shared Dragonfly queue. Optional integrations
provide PostgreSQL state storage, KEDA, Istio networking and telemetry collection.
See the [operator guide](../../docs/deployment/operator.md) for graph semantics,
replica coordination, health metrics and installation examples.

## Table of contents

- [Installation](#installation)
- [Reference values](#reference-values)
- [Template layout](#template-layout)
- [Parameters](#parameters)
  - [Polyad resource templates](#polyad-resource-templates)
  - [Deployment profiles](#deployment-profiles)
  - [Helm-installed downstream operator workers](#helm-installed-downstream-operator-workers)
  - [OpenTelemetry traces and logs](#opentelemetry-traces-and-logs)
  - [Operator and shared queue parameters](#operator-and-shared-queue-parameters)
  - [Optional PostgreSQL state storage](#optional-postgresql-state-storage)
  - [Optional HA component deployment architecture](#optional-ha-component-deployment-architecture)
  - [Upstream Dragonfly operator dependency](#upstream-dragonfly-operator-dependency)
  - [Composition API](#composition-api)
  - [Temporary connections](#temporary-connections)
  - [Event subscriptions](#event-subscriptions)
  - [Scheduler metrics](#scheduler-metrics)
  - [Named operator API credentials](#named-operator-api-credentials)
  - [KEDA installation, observation and credentials](#keda-installation-observation-and-credentials)
  - [Optional External Secrets Operator resources](#optional-external-secrets-operator-resources)
  - [Operator endpoint and cache isolation](#operator-endpoint-and-cache-isolation)
  - [Optional Istio integration](#optional-istio-integration)
  - [Shared Istio namespace](#shared-istio-namespace)
  - [Cross-cluster PolyGraph management](#cross-cluster-polygraph-management)
  - [Optional shared graph observers](#optional-shared-graph-observers)
  - [Advance graph capacity](#advance-graph-capacity)
  - [Root control plane](#root-control-plane)
  - [Telemetry collectors](#telemetry-collectors)

## Installation

Install and upgrade output lists configured public routes, enabled internal API
endpoints and local port-forward commands. View it again with
`helm get notes RELEASE -n NAMESPACE`. Existing Gateway listeners and addresses
are discovered using the commands in the notes; credentials are never printed.

Use Helm `operator.nodeSelector` and `operator.tolerations` to select the operator's node group.
Graph CR placement controls workload pods independently; enforced graph placement
is copied into their native templates. The Python `polyad.cache` package connects
replicas using `POLYAD_CACHE_URL`, including external Redis-compatible endpoints.

Optional [Alloy and Prometheus Agent collectors](../../docs/operations/telemetry-agents.md)
discover exporters from enabled chart components. Use the
[all-signals reference](references/values-telemetry.reference.yaml) or
[metrics-only reference](references/values-prometheus-agent.reference.yaml) with your existing backends.

```sh
helm dependency build charts/polyad
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set operator.image.repository=YOUR_REGISTRY/polyad --set operator.image.tag=YOUR_TAG
```

The chart installs the [upstream Dragonfly operator](https://github.com/dragonflydb/dragonfly-operator)
Helm dependency, pinned in `Chart.lock`, and a managed Dragonfly cache. Defaults
are one Polyad replica (`ha: false`), two leader-elected Dragonfly controller replicas, and one
Dragonfly data instance with five-minute PVC snapshots and eviction disabled.

Install KEDA separately or set `keda.install=true` to enable the pinned upstream
dependency. In root mode, KEDA and every enabled local service join the
[root operator Graph](../../docs/deployment/local-services.md). The
[KEDA reference values](references/values-keda.reference.yaml) describe bundled and existing
installations. With `ha=true`, bundled KEDA also enables
[CPU/memory autoscaling for its metrics server and webhooks](../../docs/deployment/local-services.md#autoscale-bundled-keda-in-ha-mode),
with configurable bounds and stabilization under `keda.autoscaling`.
Enable cache HA with an initial primary and one replica:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set dragonfly.ha.enabled=true
```

HA enables [KEDA cache scaling](../../docs/deployment/dragonfly.md) between two and five
instances by default, using connection counts from Polyad's metrics endpoint.
These are failover copies; additional instances do not increase primary write
capacity. Set `dragonfly.autoscaling.enabled=false` for a fixed count without KEDA.

HA instances require distinct nodes by default. Set `dragonfly.ha.replicas=3` to
start with one primary and two replicas, or change `dragonfly.ha.topologyKey` to
`topology.kubernetes.io/zone` to require separate zones. The cluster needs enough
eligible nodes/zones and a storage provisioner. The managed `<release>-queue`
Service routes writes to the primary across failover. Replication is asynchronous;
API rescans reconstruct lost queue hints after a failure.

For external Dragonfly, set `dragonfly.enabled=false` and supply
`dragonfly.existingSecret` (a Secret with a `url` key) or `dragonfly.externalUrl`.
This disables both the managed instance and the controller dependency. Install
only one bundled Dragonfly controller release per cluster; other Polyad releases
can share its primary endpoint with namespace-isolated queues.

Polyad's [separately versioned resource chart](../polyad-crds/README.md) installs
the API definitions and can render named resource instances. Configure its maps
under `polyadResources`, for example `polyadResources.graphs.pipeline.spec`.
Every field accepts `tpl` references to other instances, and CRD defaults are
populated automatically. Its [per-kind reference values](../polyad-crds/README.md#reference-values-by-kind)
contain fully commented examples with required, optional and defaulted fields.

Polyad ships the pinned Dragonfly CRD in that dependency's `crds/` so Helm registers it before
creating the instance on a fresh installation. Keep
`dragonflyOperator.crds.install=false` to avoid duplicate CRD ownership. Helm
retains CRDs and does not upgrade them automatically; see [upstream provenance
and upgrade instructions](UPSTREAM.md).
Apply `crds/dragonflypools.yaml` before enabling cache autoscaling on an existing
release; Helm installs this new scaling API automatically on fresh installations.

**Migration from the former bundled StatefulSet:** the managed cache uses a new
`<release>-queue` name and fresh volumes. Existing queue snapshots are not
imported. Kubernetes remains authoritative and rescans repopulate notifications;
expect a reconciliation pause while the new cache starts. Review and remove the
old `data-<release>-dragonfly-0` PVC separately when it is no longer needed.

See the [networking guide](../../docs/deployment/networking.md) for scoped graph isolation,
optional Istio installation and endpoint authorization, event subscribers, and
Secret-driven health replacement. Both networking integrations are disabled by default.

Use [named API keys](../../docs/operations/api-keys.md) to separate service and operator
credentials, with inbound/outbound/bidirectional permissions and per-key rate
and concurrency limits shared across HA replicas. The
[authentication reference](references/values-authentication.reference.yaml) includes all
three directions and a dedicated KEDA lane.

Cross-cluster placement (`federation.enabled`), sidecar mesh transport
(`mesh.multicluster.enabled`) and shared read-only replicas (`observer.enabled`)
are separate optional extensions. See [the multicluster guide](../../docs/deployment/multicluster.md)
for registered credentials, east-west gateways, cluster-local rule scope and
complete values examples.
The [gateway listener settings](../../docs/deployment/multicluster.md#configurable-gateway-listener)
explain configurable names, hosts and ports, required TLS behavior, and matching
discovery labels and remote traffic grants.

With ESO enabled, `externalSecrets.reloadOnChange=true` adds Stakater Reloader
match/search annotations for generated Secrets and their operator/observer
consumers. Graph Daemons opt in with `spec.reloadOnSecretChange: true`.
See [Secret rotation](../../docs/operations/authentication.md#restart-consumers-after-rotation)
for the required Reloader installation and controller update behavior.

Operator deployment settings are grouped under `operator`. When upgrading existing
values files, nest replicas, placement, autoscaling, image, resources and shutdown
grace settings under that key (for example, `image.tag` becomes `operator.image.tag`).

## Reference values

[Atlas discovery values](references/values-discovery.reference.yaml) demonstrate inherited
operator modes, named-key home graphs and cross-cluster service negotiation.

The commented [`references/values-*.reference.yaml`](references/README.md) files
highlight settings for each profile and optional extension. Copy and adapt the
files you need, then pass them with `--values`; Helm does not load them
automatically. [`values.yaml`](values.yaml)
remains the complete default configuration. Set the single top-level `ha` flag
to false (default) or true. Each reference uses a generated partial editor schema
with canonical field types; Helm validates all requirements after merging defaults.

Each `@param` comment declares the accepted type, such as `[string]`, `[boolean]`,
`[integer]` or `[number]`. Collections use `[array]` or `[object]`, and `nullable`
allows `null`. The parameter tables below show these types alongside the actual
default values. Schema checks keep annotations in all shipped values files in sync.

| Reference file | Configuration and placement | Guide |
| --- | --- | --- |
| [`values-singular.reference.yaml`](references/values-singular.reference.yaml) | One dense operator and a persistent cache in the release cluster | [Singular](../../docs/deployment/deployment-profiles.md#one-dense-operator) |
| [`values-ha.reference.yaml`](references/values-ha.reference.yaml) | Replicated dense operators; also opts into cache HA | [HA](../../docs/deployment/deployment-profiles.md#ha-in-one-cluster) |
| [`values-connection-pools.reference.yaml`](references/values-connection-pools.reference.yaml) | Per-process Redis/PostgreSQL limits and KEDA scaling from active pool demand | [Connection pools](../../docs/operations/performance.md#connection-pools-and-keda) |
| [`values-keda.reference.yaml`](references/values-keda.reference.yaml) | Optional bundled KEDA installation and observation in the root operator Graph | [KEDA installation](../../docs/deployment/local-services.md#install-keda-with-the-chart) |
| [`values-components.reference.yaml`](references/values-components.reference.yaml) | HA bootstrap plus a self-managed gateway/executor/telemetry Graph and KEDA scaling in the release cluster | [Components](../../docs/deployment/components.md) |
| [`values-federation.reference.yaml`](references/values-federation.reference.yaml) | Remote cluster registrations for PolyGraph placement; destinations have independent execution operators | [Federation](../../docs/deployment/multicluster.md#placement-and-ownership) |
| [`values-root-control-plane.reference.yaml`](references/values-root-control-plane.reference.yaml) | HA management release that installs and controls remote execution pools | [Root control plane](../../docs/deployment/root-control-plane.md) |
| [`values-worker.reference.yaml`](references/values-worker.reference.yaml) | Downstream Helm-owned executors attached to a root, with explicit root or local scaling authority | [Helm workers](../../docs/deployment/helm-workers.md) |
| [`values-multicluster.reference.yaml`](references/values-multicluster.reference.yaml) | Istio transport, peer gateways and local network identity; adapt separately per cluster | [Multicluster networking](../../docs/deployment/multicluster.md#istio-across-different-networks) |
| [`values-istio-bundled.reference.yaml`](references/values-istio-bundled.reference.yaml) | Release-owned, pinned Istio base and control plane with native operator sidecars | [Bundled Istio](../../docs/deployment/istio-features.md#deployment-strategies) |
| [`values-istio-existing.reference.yaml`](references/values-istio-existing.reference.yaml) | Platform-owned Istio with Gateway API ingress and no control-plane lifecycle ownership | [Existing Istio](../../docs/deployment/istio-features.md#deployment-strategies) |
| [`values-istio-features.reference.yaml`](references/values-istio-features.reference.yaml) | Proxy telemetry, Sidecar scoping, policy observation and explicit gateway egress | [Optional features](../../docs/deployment/istio-features.md#feature-reference-values) |
| [`values-istio-traffic.reference.yaml`](references/values-istio-traffic.reference.yaml) | Locality routing, endpoint ejection and long-lived event connection balancing | [Traffic strategies](../../docs/deployment/istio-features.md#traffic-strategy-reference-values) |
| [`values-observer.reference.yaml`](references/values-observer.reference.yaml) | Read-only observers alongside this release's operator | [Observers](../../docs/deployment/multicluster.md#optional-shared-observers) |
| [`values-postgresql.reference.yaml`](references/values-postgresql.reference.yaml) | Optional persistent state, database HA and connection-driven KEDA scaling in the release cluster | [PostgreSQL](../../docs/deployment/postgresql.md) |
| [`values-postgresql-encryption.reference.yaml`](references/values-postgresql-encryption.reference.yaml) | Encrypted volumes for managed state and authentication databases; GKE Cloud KMS example and existing StorageClass alternative | [Encryption at rest](../../docs/deployment/postgresql.md#encryption-at-rest) |
| [`values-postgresql-record-encryption.reference.yaml`](references/values-postgresql-record-encryption.reference.yaml) | Optional encryption before database writes using an existing public/private key Secret | [Record encryption](../../docs/deployment/record-encryption.md) |
| [`values-authentication.reference.yaml`](references/values-authentication.reference.yaml) | Scoped service/operator keys, workload Secret assignments and optional dedicated authentication storage | [API keys](../../docs/operations/api-keys.md) |
| [`values-connections.reference.yaml`](references/values-connections.reference.yaml) | Service consent events and separate connection/reconciliation pulse budgets | [Temporary connections](../../docs/apis/temporary-connections.md) |
| [`values-websockets.reference.yaml`](references/values-websockets.reference.yaml) | Optional WebSocket subscriptions sharing the events Service, authorization and subscriber limits with SSE | [WebSocket subscriptions](../../docs/workloads/workload-events.md#websocket-subscriptions) |
| [`values-event-rebalancing.reference.yaml`](references/values-event-rebalancing.reference.yaml) | Rolling copulses, ready endpoint discovery, Istio routing and preStop draining | [Event connection rebalancing](../../docs/operations/event-rebalancing.md) |
| [`values-events.reference.yaml`](references/values-events.reference.yaml) | Event byte budgets, replay batch size, polling, retention and subscriber capacity | [Event contract and tuning](../../docs/apis/event-contract.md) |
| [`values-demo.reference.yaml`](references/values-demo.reference.yaml) | Public demonstration endpoints without authentication or HTTP quotas | [Demo mode](../../docs/operations/api-keys.md#demonstrations-without-authentication) |
| [`values-tracing.reference.yaml`](references/values-tracing.reference.yaml) | OTLP/HTTP traces and independent decision logs, parent-based trace sampling and optional exporter credentials | [OpenTelemetry traces and logs](../../docs/operations/tracing.md) |
| [`values-tuning.reference.yaml`](references/values-tuning.reference.yaml) | Work-graph worker and planner limits, validation cadence, polling intervals, Cheeger computation ceilings and metrics | [Write-pipeline configuration](../../docs/development/write-pipeline.md#configuration), [performance tuning](../../docs/operations/performance.md) |
| [`values-soul-searching.reference.yaml`](references/values-soul-searching.reference.yaml) | Capacity preparation within fixed operator ceilings; per-graph profiles define demand, targets and lookahead | [Approved load profiles](../../docs/graphs/load-profiles.md) |

Each file can render with chart defaults. Installation also requires the
infrastructure and Secrets called out in its comments. Replace example cluster
names, addresses, CIDRs and Secret references with your environment's values.
Observer values add observers to an operator release; they do not create an
observer-only release.

For example, combine HA, split components and optional PostgreSQL:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --values charts/polyad/references/values-ha.reference.yaml \
  --values charts/polyad/references/values-components.reference.yaml \
  --values charts/polyad/references/values-postgresql.reference.yaml
```

Later files override earlier values; lists such as `federation.clusters`,
`rootControlPlane.pools` and mesh peers are replaced, not appended. Use one base
profile, then extensions, then your environment overrides. Keep
`global.multiCluster.clusterName` consistent across the selected files. See
[combination examples](../../docs/deployment/deployment-profiles.md#combine-reference-values)
for federation and a split root installation.

## Template layout

Set `ha: false` (default) for singular or `ha: true` for HA.
`operator.replicaCount: null` resolves to one or two respectively. Root-scaled
Helm workers instead omit replicas, leaving their count to the root OperatorPool.
See [deployment profiles](../../docs/deployment/deployment-profiles.md) for install commands,
replica floors and the management/workload cluster placement table.

Templates are grouped by the deployment architecture they support:

| Directory | Purpose | Enabled by |
| --- | --- | --- |
| [`templates/singular/`](templates/singular) | One combined operator replica | `ha: false` (default) |
| [`templates/ha/`](templates/ha) | Replicated dense operator and optional root-owned execution pool declarations | `ha: true` |
| [`templates/ha/distributed/`](templates/ha/distributed) | Bootstrap Deployment and the gateway, executor and telemetry Graph, including component scaling | HA with `architecture.mode: Distributed` |
| [`templates/worker/`](templates/worker) | Administrator-installed executor Deployment with explicit root attachment | `worker.enabled`; replaces the ordinary singular or HA Deployment |
| [`templates/multicluster/`](templates/multicluster) | Federation and root-control-plane validation, east-west mesh resources and optional read-only observers | `federation.enabled`, `rootControlPlane.enabled`, `mesh.multicluster.enabled` and `observer.enabled`, independently of deployment mode |
| [`templates/shared/`](templates/shared) | Shared Deployment definition, Services, access controls, credentials, storage, ingress and autoscaling support | Both modes, with each optional feature controlled by its existing values |

Dense and Distributed use the same `polyad.operatorDeployment` named template in
[`shared/_deployment.tpl`](templates/shared/_deployment.tpl). Distributed components
also reuse its Pod template so image, credentials, placement and security settings
stay consistent. The shared CPU/memory HPA targets the dense operator or the distributed
bootstrap; component KEDA resources live with the distributed Graph.

Set `operator.autoscaling.enabled=true` to enable the HPA. CPU utilization is
always included; set `operator.autoscaling.targetMemoryUtilizationPercentage=80`
to add memory utilization at 80% of requested memory. The memory target defaults
to `null` (disabled), and enabling it requires `operator.resources.requests.memory`.
See [autoscaling configuration](../../docs/operations/performance.md#autoscaling-response)
for metric behavior, prerequisites and an example with both targets. Add
`operator.autoscaling.connections.enabled=true` for connection pressure; KEDA then
owns the same target's HPA and retains CPU/memory settings. See
[connection-pool tuning](../../docs/operations/performance.md#connection-pools-and-keda).

Federation, mesh and observers remain optional. Root-managed execution requires
HA, using either Dense or Distributed mode. Remote execution Deployments
are created by the root operator from Helm-declared or separately managed
OperatorPools. `NOTES.txt` remains at the template root, and install-time CRDs remain in
`crds/`. Directory placement organizes the source; values select the rendered
resources.

See [Dense and Distributed deployments](../../docs/deployment/components.md) and
[the root control plane](../../docs/deployment/root-control-plane.md) for architecture details.

## Parameters

### Polyad resource templates

| Name                      | Description                                                                                                                                    | Value  |
| ------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `polyadResources.enabled` | **Type: boolean.** Include the independently versioned CRD and resource chart; false requires administrators to install definitions separately | `true` |

### Deployment profiles

| Name | Description                                                                                                                                 | Value   |
| ---- | ------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `ha` | **Type: boolean.** Boolean. Run at least two operator replicas and permit split components or remote workers; false runs one dense operator | `false` |

### Helm-installed downstream operator workers

| Name                      | Description                                                                                                                                        | Value   |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `worker.enabled`          | **Type: boolean.** Install an execution replica connected to an existing root; disables root election and requires external root/cache credentials | `false` |
| `worker.rootNamespace`    | **Type: string.** Namespace of the root's queues, leases and OperatorPool; independent of this Helm release namespace                              | `""` |
| `worker.rootClusterName`  | **Type: string.** Root federation identity; global.multiCluster.clusterName identifies this worker's hosting cluster instead                       | `""` |
| `worker.rootDeployment`   | **Type: string.** Name of the existing root Deployment, used to identify the attachment authority                                                  | `""` |
| `worker.rootGraph`        | **Type: string.** Name of the root's reserved PolyGraph; normally ROOT_RELEASE-atlas                                                               | `""` |
| `worker.poolName`         | **Type: string.** Name of the root-namespace OperatorPool that may attach this Deployment                                                          | `""` |
| `worker.scalingAuthority` | **Type: string.** Root lets the matching OperatorPool/KEDA control replicas; Local keeps replicas and optional HPA under this Helm release         | `Root` |

### OpenTelemetry traces and logs

| Name                         | Description                                                                                                                                           | Value                                  |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `tracing.enabled`            | **Type: boolean.** Export operator and observer traces over OTLP/HTTP; disabled creates no exporter                                                   | `false` |
| `tracing.endpoint`           | **Type: string.** Full HTTP/protobuf trace URL, including /v1/traces; use a Collector reachable from every execution cluster                          | `http://otel-collector:4318/v1/traces` |
| `tracing.serviceName`        | **Type: string.** Service identity in the trace backend; resource attributes can identify cluster and environment                                     | `polyad-operator` |
| `tracing.samplingRatio`      | **Type: number.** Fraction of new root traces to sample (number, 0-1); child spans honor their parent's sampling decision                             | `1` |
| `tracing.timeoutSeconds`     | **Type: integer.** Export request timeout in seconds; exports are batched off the reconciliation path                                                 | `10` |
| `tracing.resourceAttributes` | **Type: string.** Comma-separated OpenTelemetry resource attributes, for example deployment.environment.name=production                               | `""` |
| `tracing.headersSecret`      | **Type: string.** Existing Secret with a headers key containing OTLP exporter headers; empty for collectors without authentication                    | `""` |
| `tracing.logs.enabled`       | **Type: boolean.** Export structured operator logs independently of trace enablement and sampling; console logging remains available                  | `false` |
| `tracing.logs.endpoint`      | **Type: string.** Full HTTP/protobuf log URL, including /v1/logs; shares service identity, timeout, resource attributes and headersSecret with traces | `http://otel-collector:4318/v1/logs` |

### Operator and shared queue parameters

| Name                                                                 | Description                                                                                                                                                                                                                              | Value                      |
| -------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `operator.serviceAccess.discovery`                                   | **Type: string.** Disabled, SameGraph, GraphTree, Cluster or Atlas; GraphTree confines discovery to related graphs in the home cluster                                                                                                   | `GraphTree` |
| `operator.serviceAccess.connections`                                 | **Type: string.** SameGraph by default; Atlas permits cross-cluster negotiation only when the receiving operator owns the common application boundary                                                                                    | `SameGraph` |
| `operator.serviceAccess.clusters`                                    | **Type: object.** Child cluster modes and parent links; each child can narrow but cannot widen its parent ceiling                                                                                                                        | `{}` |
| `operator.serviceAccess.trustDomains`                                | **Type: object.** Explicit Istio trust domain for each participating cluster; required for cross-cluster connections                                                                                                                     | `{}` |
| `operator.logLevel`                                                  | **Type: string.** Operator and observer Python logging verbosity; INFO for normal operation, DEBUG for reconciliation diagnostics (DEBUG, INFO, WARNING, ERROR or CRITICAL)                                                              | `INFO` |
| `operator.replicaCount`                                              | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha, and must remain null for Root-scaled workers                                                                                                      | `null` |
| `operator.nodeSelector`                                              | **Type: object.** Node labels selecting the operator node group, independent of workload graph placement                                                                                                                                 | `{}` |
| `operator.tolerations`                                               | **Type: array.** Taints tolerated by the operator replicas                                                                                                                                                                               | `[]` |
| `operator.autoscaling.enabled`                                       | **Type: boolean.** Enable operator HPA using CPU and optional memory utilization                                                                                                                                                         | `false` |
| `operator.autoscaling.minReplicas`                                   | **Type: integer.** Minimum operator replicas                                                                                                                                                                                             | `2` |
| `operator.autoscaling.maxReplicas`                                   | **Type: integer.** Maximum operator replicas, at least minReplicas and at most 32                                                                                                                                                        | `8` |
| `operator.autoscaling.targetCPUUtilizationPercentage`                | **Type: integer.** Target operator CPU utilization relative to requested CPU                                                                                                                                                             | `70` |
| `operator.autoscaling.targetMemoryUtilizationPercentage`             | **Type: integer or null.** Optional target memory utilization relative to requested memory (1-100); null disables the memory metric                                                                                                      | `null` |
| `operator.autoscaling.connections.enabled`                           | **Type: boolean.** Add KEDA connection-pressure scaling alongside CPU/memory for operator autoscaling, or to existing Distributed component scalers. Requires HA and KEDA.                                                               | `false` |
| `operator.autoscaling.connections.targetUtilizationPercentage`       | **Type: integer.** Target busy-plus-waiting connections as a percentage of configured pool capacity per process (1-100); idle retained sockets are excluded.                                                                             | `70` |
| `operator.autoscaling.connections.pollingInterval`                   | **Type: integer.** KEDA polling interval in seconds for the dense/bootstrap operator; HPA polling also follows the cluster sync period.                                                                                                  | `15` |
| `operator.autoscaling.behavior.scaleUp.stabilizationWindowSeconds`   | **Type: integer.** Scale-up recommendation window in seconds (0-3600)                                                                                                                                                                    | `0` |
| `operator.autoscaling.behavior.scaleUp.selectPolicy`                 | **Type: string.** Choose the largest or smallest permitted change, or disable scale-up (Max, Min, Disabled)                                                                                                                              | `Max` |
| `operator.autoscaling.behavior.scaleUp.policies`                     | **Type: array.** Rate limits (Pods or Percent); defaults to 100 percent or 4 Pods per 15 seconds; periodSeconds accepts 1-1800                                                                                                           | `[{"type": "Percent", "value": 100, "periodSeconds": 15}, {"type": "Pods", "value": 4, "periodSeconds": 15}]` |
| `operator.autoscaling.behavior.scaleDown.stabilizationWindowSeconds` | **Type: integer.** Scale-down recommendation window in seconds (0-3600)                                                                                                                                                                  | `300` |
| `operator.autoscaling.behavior.scaleDown.selectPolicy`               | **Type: string.** Choose the largest or smallest permitted change, or disable scale-down (Max, Min, Disabled)                                                                                                                            | `Max` |
| `operator.autoscaling.behavior.scaleDown.policies`                   | **Type: array.** Rate limits (Pods or Percent); defaults to 100 percent per 15 seconds; periodSeconds accepts 1-1800                                                                                                                     | `[{"type": "Percent", "value": 100, "periodSeconds": 15}]` |
| `operator.cheeger.maxVertices`                                       | **Type: integer.** Maximum vertices per Cheeger calculation (2-4096); 20 is the default, and raising it also requires enough cut/time budget                                                                                             | `20` |
| `operator.cheeger.maxCuts`                                           | **Type: integer.** Maximum distinct cuts per calculation (1-2147483647); 524287 covers 20 vertices, and incomplete searches block constrained actions                                                                                    | `524287` |
| `operator.cheeger.timeoutSeconds`                                    | **Type: number.** Cooperative time ceiling per calculation (0.001-300 seconds); checked between cut batches, never converts partial results into exact constants                                                                         | `5` |
| `operator.writeQueue.reconciliationCooldownSeconds`                  | **Type: number.** Shared per-resource cooldown window before new reconciliation decisions (0-300 seconds); zero disables pacing, expiry and connection consent remain live                                                               | `0` |
| `operator.writeQueue.reconciliationBurst`                            | **Type: integer.** New decisions per resource in one cooldown window (1-128); coalesced desired-state deliveries retry after deferral                                                                                                    | `1` |
| `operator.writeQueue.plannerParallelism`                             | **Type: integer.** Maximum mutation callbacks per approved planner batch (integer 1-32); 1 keeps batches serial, higher values require independent effects and sufficient writer/admission capacity; callers can lower this ceiling only | `1` |
| `operator.writeQueue.maxInFlight`                                    | **Type: integer.** Maximum concurrent writer slots per adapter (integer 1-32); 1 serializes transport, larger values allow only planner-approved independent writes to overlap; shared dependencies remain ordered                       | `1` |
| `operator.writeQueue.maxPending`                                     | **Type: integer.** Extra admitted writes beyond writer slots (integer 0-128); 1 keeps decisions fresh, larger bursts add reads and may return more drift conflicts; overflow retries from fresh state                                    | `1` |
| `operator.writeQueue.validationIntervalSeconds`                      | **Type: number.** Maximum pause between validator passes (0.01-60 seconds, no greater than validationWindowSeconds); watch notifications wake it sooner                                                                                  | `1` |
| `operator.writeQueue.validationWindowSeconds`                        | **Type: number.** Maximum age of a reusable dependency validation from its start (0.01-60 seconds); shorter windows add reads, longer windows tolerate more unobserved external drift                                                    | `5` |
| `operator.writeQueue.validationBurst`                                | **Type: integer.** Maximum pending candidates examined per validator pass (integer 1-128); later items use spare time before the oldest receipts expire                                                                                  | `8` |
| `operator.writeQueue.validationWorkers`                              | **Type: integer.** Maximum concurrent candidate validations per adapter (integer 1-32), shared by background and dispatch checks; raise only with Kubernetes read capacity available                                                     | `1` |
| `operator.writeQueue.reconciliationWorkers`                          | **Type: integer.** Maximum concurrent local reconciliation attempts and remote deliveries per cluster (integer 1-32); same-key follow-ups and graph-family leases remain serialized, separate families can prepare decisions together    | `1` |
| `operator.connections.cache.maxConnections`                          | **Type: integer.** Maximum connections per pool per process (1-4096); multiply by maximum replicas and pool instances when budgeting backend capacity.                                                                                   | `128` |
| `operator.connections.cache.connectTimeoutSeconds`                   | **Type: number.** Connection establishment timeout in seconds; shorter values fail faster during backend outages. Range: 0.1-60; fractional seconds allowed.                                                                             | `5` |
| `operator.connections.cache.socketTimeoutSeconds`                    | **Type: number.** Redis socket read/write timeout in seconds (0.1-60); requests fail closed and uncertain writes are not replayed.                                                                                                       | `5` |
| `operator.connections.lanes.maxConnections`                          | **Type: integer.** Maximum connections per pool per process (1-4096); multiply by maximum replicas and pool instances when budgeting backend capacity.                                                                                   | `32` |
| `operator.connections.lanes.connectTimeoutSeconds`                   | **Type: number.** Connection establishment timeout in seconds; shorter values fail faster during backend outages. Range: 0.1-60; fractional seconds allowed.                                                                             | `2` |
| `operator.connections.lanes.socketTimeoutSeconds`                    | **Type: number.** Redis socket read/write timeout in seconds (0.1-60); requests fail closed and uncertain writes are not replayed.                                                                                                       | `2` |
| `operator.connections.rateLimits.maxConnections`                     | **Type: integer.** Maximum connections per pool per process (1-4096); multiply by maximum replicas and pool instances when budgeting backend capacity.                                                                                   | `32` |
| `operator.connections.rateLimits.connectTimeoutSeconds`              | **Type: number.** Connection establishment timeout in seconds; shorter values fail faster during backend outages. Range: 0.1-60; fractional seconds allowed.                                                                             | `5` |
| `operator.connections.rateLimits.socketTimeoutSeconds`               | **Type: number.** Redis socket read/write timeout in seconds (0.1-60); requests fail closed and uncertain writes are not replayed.                                                                                                       | `5` |
| `operator.connections.state.minConnections`                          | **Type: integer.** Minimum PostgreSQL connections per pool (0-4096, no greater than maxConnections); zero opens lazily.                                                                                                                  | `0` |
| `operator.connections.state.maxConnections`                          | **Type: integer.** Maximum connections per pool per process (1-4096); multiply by maximum replicas and pool instances when budgeting backend capacity.                                                                                   | `2` |
| `operator.connections.state.poolTimeoutSeconds`                      | **Type: number.** PostgreSQL connection checkout timeout in seconds (0.1-60); includes waiting for a free pool slot.                                                                                                                     | `5` |
| `operator.connections.state.connectTimeoutSeconds`                   | **Type: integer.** Connection establishment timeout in seconds; shorter values fail faster during backend outages. Range: 1-60; whole seconds.                                                                                           | `5` |
| `operator.connections.state.statementTimeoutMilliseconds`            | **Type: integer.** PostgreSQL statement timeout in milliseconds (1-300000); bounds each database statement.                                                                                                                              | `5000` |
| `operator.connections.state.lockTimeoutMilliseconds`                 | **Type: integer.** PostgreSQL lock acquisition timeout in milliseconds (1-300000); bounds contention independently of checkout.                                                                                                          | `4000` |
| `operator.connections.authentication.minConnections`                 | **Type: integer.** Minimum PostgreSQL connections per pool (0-4096, no greater than maxConnections); zero opens lazily.                                                                                                                  | `0` |
| `operator.connections.authentication.maxConnections`                 | **Type: integer.** Maximum connections per pool per process (1-4096); multiply by maximum replicas and pool instances when budgeting backend capacity.                                                                                   | `2` |
| `operator.connections.authentication.poolTimeoutSeconds`             | **Type: number.** PostgreSQL connection checkout timeout in seconds (0.1-60); includes waiting for a free pool slot.                                                                                                                     | `5` |
| `operator.connections.authentication.connectTimeoutSeconds`          | **Type: integer.** Connection establishment timeout in seconds; shorter values fail faster during backend outages. Range: 1-60; whole seconds.                                                                                           | `5` |
| `operator.connections.authentication.statementTimeoutMilliseconds`   | **Type: integer.** PostgreSQL statement timeout in milliseconds (1-300000); bounds each database statement.                                                                                                                              | `5000` |
| `operator.connections.authentication.lockTimeoutMilliseconds`        | **Type: integer.** PostgreSQL lock acquisition timeout in milliseconds (1-300000); bounds contention independently of checkout.                                                                                                          | `4000` |
| `operator.connections.kubernetes.poolSize`                           | **Type: integer.** Retained HTTP connections per Kubernetes API adapter (1-4096); this is a keepalive cache, not a concurrent-request ceiling.                                                                                           | `32` |
| `operator.connections.kubernetes.connectTimeoutSeconds`              | **Type: number.** Connection establishment timeout in seconds; shorter values fail faster during backend outages. Range: 0.1-60; fractional seconds allowed.                                                                             | `5` |
| `operator.connections.kubernetes.readTimeoutSeconds`                 | **Type: number.** Kubernetes response timeout in seconds (0.1-60); timed-out mutations require fresh reconciliation.                                                                                                                     | `20` |
| `operator.tuning.rescanIntervalSeconds`                              | **Type: number.** Pause after local and root-managed remote inventory scans (1-15s); watch polyad_inventory_sample_age_seconds and polyad_cluster_inventory_sample_fresh; lower values add Kubernetes and optional state writes          | `5` |
| `operator.tuning.consumeIntervalSeconds`                             | **Type: number.** Pause after local and remote queue consumption passes (0.1-5s); watch polyad_inbound_updates and polyad_kubernetes_writes_queued before increasing dispatch pressure                                                   | `1` |
| `operator.tuning.metricsIntervalSeconds`                             | **Type: number.** Pause after cached metrics publication and component/worker reporting (1-5s); also controls enabled PostgreSQL and Dragonfly connection sampling, not Prometheus scrape frequency or trace export                      | `5` |
| `operator.tuning.backlogIntervalSeconds`                             | **Type: number.** Pause after local shared-queue samples (1-5s); watch polyad_inbound_sample_age_seconds; root-managed remote backlog is sampled by rescanIntervalSeconds                                                                | `5` |
| `operator.image.repository`                                          | **Type: string.** Operator image repository                                                                                                                                                                                              | `ghcr.io/astrivant/polyad` |
| `operator.image.tag`                                                 | **Type: string.** Operator image tag                                                                                                                                                                                                     | `0.0.1-alpha3` |
| `operator.image.pullPolicy`                                          | **Type: string.** Operator image pull policy                                                                                                                                                                                             | `IfNotPresent` |
| `operator.resources.requests.cpu`                                    | **Type: string.** Requested operator CPU; match limits.cpu for Guaranteed QoS and use this baseline for CPU autoscaling                                                                                                                  | `1` |
| `operator.resources.requests.memory`                                 | **Type: string.** Requested operator memory; match limits.memory for Guaranteed QoS and use this baseline for memory autoscaling                                                                                                         | `1Gi` |
| `operator.resources.limits.cpu`                                      | **Type: string.** CPU limit; match the request to retain Guaranteed QoS                                                                                                                                                                  | `1` |
| `operator.resources.limits.memory`                                   | **Type: string.** Operator memory limit; match the request when right-sizing from Grafana                                                                                                                                                | `1Gi` |
| `operator.terminationGracePeriodSeconds`                             | **Type: integer.** Time allowed for operator shutdown and outstanding API calls                                                                                                                                                          | `60` |

### Optional PostgreSQL state storage

| Name                                                | Description                                                                                                                                                             | Value                                                    |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `postgresql.enabled`                                | **Type: boolean.** Persist graph state and tracked parameters in PostgreSQL                                                                                             | `false` |
| `postgresql.managed`                                | **Type: boolean.** Create a CloudNativePG Cluster; install its operator first                                                                                           | `true` |
| `postgresql.existingSecret`                         | **Type: string.** External database Secret containing the connection DSN when managed is false                                                                          | `""` |
| `postgresql.secretKey`                              | **Type: string.** DSN key in the external database Secret                                                                                                               | `uri` |
| `postgresql.database`                               | **Type: string.** String. Application database for graph state and optional event history; set only when bootstrapping a new managed cluster                            | `polyad` |
| `postgresql.username`                               | **Type: string.** String. Dedicated application owner; PUBLIC database access is revoked and superuser access stays disabled                                            | `polyad` |
| `postgresql.events.enabled`                         | **Type: boolean.** Boolean. Archive approved application events in PostgreSQL; bounded live replay still uses Dragonfly                                                 | `true` |
| `postgresql.events.retentionDays`                   | **Type: integer.** Integer. Retain durable event history for this many days; graph snapshots retain current state independently                                         | `30` |
| `postgresql.scope`                                  | **Type: string.** State identity within the database; empty uses the release namespace and name                                                                         | `""` |
| `postgresql.image`                                  | **Type: string.** PostgreSQL image for the managed cluster                                                                                                              | `ghcr.io/cloudnative-pg/postgresql:18.3-standard-trixie` |
| `postgresql.maxConnections`                         | **Type: integer.** Maximum connections per managed database instance, including administration                                                                          | `100` |
| `postgresql.storage.size`                           | **Type: string.** Persistent storage per PostgreSQL instance                                                                                                            | `10Gi` |
| `postgresql.storage.storageClass`                   | **Type: string.** Storage class; empty uses the cluster default unless encryptionAtRest selects a class; conflicting explicit classes are rejected                      | `""` |
| `postgresql.encryptionAtRest.enabled`               | **Type: boolean.** Require the selected encrypted class for managed database PVCs; enabling this does not migrate existing volumes                                      | `false` |
| `postgresql.encryptionAtRest.provider`              | **Type: string.** ExistingStorageClass uses an administrator-verified encrypted class; GKE creates a Persistent Disk CSI class referencing a Cloud KMS key              | `ExistingStorageClass` |
| `postgresql.encryptionAtRest.storageClass`          | **Type: string.** Required existing class name for ExistingStorageClass; optional name for the new GKE class, otherwise generated from release namespace and name       | `""` |
| `postgresql.encryptionAtRest.kmsKeyName`            | **Type: string.** GKE symmetric Cloud KMS CryptoKey resource name in the cluster region; key material and IAM remain outside the chart                                  | `""` |
| `postgresql.encryptionAtRest.diskType`              | **Type: string.** GKE Persistent Disk CSI type such as pd-balanced, pd-ssd or hyperdisk-balanced; choose one supported by the node machine family and CSI driver        | `pd-balanced` |
| `postgresql.recordEncryption.enabled`               | **Type: boolean.** Encrypt new state, snapshot, event and authentication-policy payloads; all database writers must use the same policy; existing rows are not migrated | `false` |
| `postgresql.recordEncryption.existingSecret`        | **Type: string.** Existing release-namespace Secret containing PEM RSA keys; key material must stay out of Helm values                                                  | `""` |
| `postgresql.recordEncryption.publicKeyKey`          | **Type: string.** Secret data key holding a PEM RSA public key of at least 2048 bits; used to wrap a fresh encryption key for each record                               | `public.pem` |
| `postgresql.recordEncryption.privateKeyKey`         | **Type: string.** Optional matching PEM private-key entry; empty mounts only the public key, sufficient for database writes                                             | `""` |
| `postgresql.recordEncryption.privateKeyPasswordKey` | **Type: string.** Optional password entry for an encrypted private PEM; requires privateKeyKey; empty means an unencrypted PEM                                          | `""` |
| `postgresql.ha.enabled`                             | **Type: boolean.** Enable primary plus standby instances and synchronous replication                                                                                    | `false` |
| `postgresql.ha.instances`                           | **Type: integer.** Total instances when HA is enabled and autoscaling is disabled                                                                                       | `3` |
| `postgresql.ha.topologyKey`                         | **Type: string.** Failure-domain label for required database pod anti-affinity                                                                                          | `kubernetes.io/hostname` |
| `postgresql.resources.requests.cpu`                 | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.                                | `250m` |
| `postgresql.resources.requests.memory`              | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.                                | `512Mi` |
| `postgresql.resources.limits.memory`                | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.                                | `1Gi` |
| `postgresql.autoscaling.enabled`                    | **Type: boolean.** Create a KEDA ScaledObject for the managed Cluster using operator connection counts                                                                  | `false` |
| `postgresql.autoscaling.minInstances`               | **Type: integer.** Minimum instances; at least 3 with HA, never zero                                                                                                    | `3` |
| `postgresql.autoscaling.maxInstances`               | **Type: integer.** Maximum database instances                                                                                                                           | `6` |
| `postgresql.autoscaling.connectionsPerInstance`     | **Type: integer.** Operator connections per desired database instance; does not add primary write capacity                                                              | `20` |

### Optional HA component deployment architecture

| Name                                                   | Description                                                                                                                                                             | Value                                                 |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `architecture.mode`                                    | **Type: string.** Dense combines responsibilities; Distributed manages gateway, executor and telemetry ReplicaGroups inside a Graph; requires ha                        | `Dense` |
| `architecture.cheegerMinimum`                          | **Type: number.** Minimum edge expansion of the three-component Graph; the default chain has h=1                                                                        | `1` |
| `architecture.cheegerMaximum`                          | **Type: number or null.** Optional maximum edge expansion of the component Graph; null leaves it unbounded, 1 permits the chain but excludes a fully connected triangle | `null` |
| `architecture.expandedNodes`                           | **Type: integer.** Maximum recursive vertices in the self-managed Graph                                                                                                 | `27` |
| `architecture.autoscaling`                             | **Type: boolean.** Enable KEDA scaling of all component ReplicaGroup definitions                                                                                        | `false` |
| `architecture.components.gateway.replicas`             | **Type: integer.** Initial gateway copies                                                                                                                               | `2` |
| `architecture.components.gateway.minReplicas`          | **Type: integer.** Minimum gateway copies                                                                                                                               | `2` |
| `architecture.components.gateway.maxReplicas`          | **Type: integer.** Maximum gateway copies                                                                                                                               | `8` |
| `architecture.components.gateway.requestsPerSecond`    | **Type: number.** Target total HTTP requests per second per copy                                                                                                        | `50` |
| `architecture.components.gateway.concurrentRequests`   | **Type: integer.** Target open HTTP requests and event streams per copy                                                                                                 | `8` |
| `architecture.components.executor.replicas`            | **Type: integer.** Initial execution copies                                                                                                                             | `2` |
| `architecture.components.executor.minReplicas`         | **Type: integer.** Minimum execution copies                                                                                                                             | `2` |
| `architecture.components.executor.maxReplicas`         | **Type: integer.** Maximum execution copies                                                                                                                             | `8` |
| `architecture.components.executor.backlog`             | **Type: integer.** Target outstanding graph hints per execution copy across all managed clusters                                                                        | `8` |
| `architecture.components.telemetry.replicas`           | **Type: integer.** Initial metrics-serving copies                                                                                                                       | `2` |
| `architecture.components.telemetry.minReplicas`        | **Type: integer.** Minimum metrics-serving copies                                                                                                                       | `2` |
| `architecture.components.telemetry.maxReplicas`        | **Type: integer.** Maximum metrics-serving copies                                                                                                                       | `8` |
| `architecture.components.telemetry.requestsPerSecond`  | **Type: number.** Target total metrics requests per second per copy                                                                                                     | `50` |
| `architecture.components.telemetry.concurrentRequests` | **Type: integer.** Target concurrent metrics requests per copy                                                                                                          | `2` |
| `dragonfly.enabled`                                    | **Type: boolean.** Deploy Dragonfly through the upstream operator Helm dependency                                                                                       | `true` |
| `dragonfly.image`                                      | **Type: string.** Bundled Dragonfly image                                                                                                                               | `docker.dragonflydb.io/dragonflydb/dragonfly:v1.39.0` |
| `dragonfly.nodeSelector`                               | **Type: object.** Node labels selecting bundled cache Pods, independently of controller placement                                                                       | `{}` |
| `dragonfly.tolerations`                                | **Type: array.** Taints tolerated by bundled cache Pods; use with nodeSelector to require a dedicated pool                                                              | `[]` |
| `dragonfly.ha.enabled`                                 | **Type: boolean.** Enable primary/replica replication and automatic failover                                                                                            | `false` |
| `dragonfly.ha.replicas`                                | **Type: integer.** Initial Dragonfly instances in HA mode, including the primary; fixed when autoscaling is disabled                                                    | `2` |
| `dragonfly.ha.topologyKey`                             | **Type: string.** Place HA instances on distinct values of this node label                                                                                              | `kubernetes.io/hostname` |
| `dragonfly.autoscaling.enabled`                        | **Type: boolean.** Use KEDA for bundled HA Dragonfly and enable Polyad metrics; inactive outside bundled HA                                                             | `true` |
| `dragonfly.autoscaling.minReplicas`                    | **Type: integer.** Minimum total cache instances, including the primary; never below two                                                                                | `2` |
| `dragonfly.autoscaling.maxReplicas`                    | **Type: integer.** Maximum total cache instances; requires sufficient eligible nodes and storage                                                                        | `5` |
| `dragonfly.autoscaling.connectionsPerReplica`          | **Type: integer.** Primary connected clients per desired cache instance; scales failover copies, not write capacity                                                     | `50` |
| `dragonfly.externalUrl`                                | **Type: string.** External Redis-compatible URL when bundled Dragonfly is disabled                                                                                      | `redis://dragonfly:6379/0` |
| `dragonfly.existingSecret`                             | **Type: string.** Existing Secret containing a url key for Dragonfly, taking precedence over other connection settings                                                  | `""` |
| `dragonfly.persistence.enabled`                        | **Type: boolean.** Persist bundled Dragonfly snapshots on a PVC                                                                                                         | `true` |
| `dragonfly.persistence.size`                           | **Type: string.** Snapshot volume capacity                                                                                                                              | `1Gi` |
| `dragonfly.persistence.storageClass`                   | **Type: string.** Snapshot volume storage class; empty uses the cluster default                                                                                         | `""` |

### Upstream Dragonfly operator dependency

| Name                                    | Description                                                                                                        | Value                |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------ | -------------------- |
| `dragonflyOperator.nameOverride`        | **Type: string.** Upstream controller resource name                                                                | `dragonfly-operator` |
| `dragonflyOperator.replicaCount`        | **Type: integer.** Leader-elected Dragonfly controller replicas                                                    | `2` |
| `dragonflyOperator.crds.install`        | **Type: boolean.** Must remain false; Polyad installs the pinned upstream CRD from crds before rendering instances | `false` |
| `dragonflyOperator.manager.extraArgs`   | **Type: array.** Additional Dragonfly controller command arguments                                                 | `[]` |
| `dragonflyOperator.rbacProxy.extraArgs` | **Type: array.** Additional metrics proxy command arguments                                                        | `[]` |

### Composition API

| Name                              | Description                                                                                                                                        | Value        |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| `api.enabled`                     | **Type: boolean.** Serve authenticated composition requests on port 8090 through a ClusterIP Service                                               | `false` |
| `api.existingSecret`              | **Type: string.** Existing Secret with a token key authorizing namespace-scoped composition submissions and audit reads                            | `polyad-api` |
| `api.key`                         | **Type: string.** Inline composition token; creates a managed Secret and requires existingSecret to be empty                                       | `""` |
| `api.rateLimit.enabled`           | **Type: boolean.** Enforce shared Redis/Dragonfly composition request quotas on every logical shard                                                | `true` |
| `api.rateLimit.requestsPerMinute` | **Type: integer.** Combined submission and audit requests per minute per shard, shared by all replicas                                             | `60` |
| `api.gateway.enabled`             | **Type: boolean.** Expose the composition Service through a Gateway API v1 HTTPRoute; requires api.enabled                                         | `false` |
| `api.gateway.create`              | **Type: boolean.** Create a Gateway in this release namespace instead of attaching to an existing Gateway                                          | `false` |
| `api.gateway.name`                | **Type: string.** Existing Gateway name when create is false; created Gateways use the release API name                                            | `""` |
| `api.gateway.namespace`           | **Type: string.** Existing Gateway namespace; empty uses the release namespace                                                                     | `""` |
| `api.gateway.className`           | **Type: string.** Installed GatewayClass used when create is true                                                                                  | `""` |
| `api.gateway.sectionName`         | **Type: string.** Listener name to attach to or create                                                                                             | `http` |
| `api.gateway.hostnames`           | **Type: array.** DNS hostnames matched by the HTTPRoute; empty matches all listener hostnames                                                      | `[]` |
| `api.gateway.tlsSecret`           | **Type: string.** Existing TLS certificate Secret in the release namespace for a created HTTPS listener on port 443; empty creates HTTP on port 80 | `""` |

### Temporary connections

| Name                                 | Description                                                                                                                                               | Value     |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `connections.enabled`                | **Type: boolean.** Serve service-account authenticated temporary connections on port 8093                                                                 | `false` |
| `connections.scope`                  | **Type: string.** Allowed caller and target namespaces: Cluster, OperatorNamespace or Namespace                                                           | `Cluster` |
| `connections.namespace`              | **Type: string.** Allowed namespace when scope is Namespace; otherwise empty                                                                              | `""` |
| `connections.maxTtlSeconds`          | **Type: integer.** Maximum connection lifetime from receipt creation, capped at 86400 seconds                                                             | `3600` |
| `connections.retentionSeconds`       | **Type: integer.** Retain terminal receipts for retry identity and audit before cleanup                                                                   | `3600` |
| `connections.pulses.cooldownSeconds` | **Type: number.** HA-shared proposal and approval cooldown window (0-300 seconds); zero disables extra pacing, rejection and revocation bypass it         | `0` |
| `connections.pulses.burst`           | **Type: integer.** New proposals per graph and positive responses per graph endpoint in a cooldown window (1-128); identical retries consume no new pulse | `1` |

### Event subscriptions

| Name                                    | Description                                                                                                                                                                                                                                      | Value           |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------- |
| `events.enabled`                        | **Type: boolean.** Serve namespace graph observations through a separate events Service on port 8091; SSE is always available when enabled                                                                                                       | `false` |
| `events.websockets.enabled`             | **Type: boolean.** Enable /v1/events/ws alongside SSE; both transports share authentication, replay cursors and subscriber limits; requires events.enabled                                                                                       | `false` |
| `events.existingSecret`                 | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                                                                                                                                                       | `polyad-events` |
| `events.key`                            | **Type: string.** Inline subscriber token; creates a managed Secret and requires existingSecret to be empty                                                                                                                                      | `""` |
| `events.retention`                      | **Type: integer.** Maximum observations retained in the shared replay stream                                                                                                                                                                     | `10000` |
| `events.maxConnections`                 | **Type: integer.** Maximum simultaneous event subscribers per operator replica                                                                                                                                                                   | `16` |
| `events.maxEventBytes`                  | **Type: integer.** Maximum complete SSE record or WebSocket JSON frame in UTF-8 bytes (1024-16777216); oversized observations are rejected before replay/archive, and clients choose their own matching receive cap                              | `1048576` |
| `events.readBatchSize`                  | **Type: integer.** Maximum observations fetched per subscriber read (1-256); higher values catch up faster but increase memory and work per read                                                                                                 | `64` |
| `events.pollIntervalSeconds`            | **Type: number.** Empty-stream polling delay in seconds (0.05-5); lower values reduce idle delivery latency at the cost of more cache reads                                                                                                      | `1` |
| `events.rebalance.enabled`              | **Type: boolean.** Enable authenticated endpoint discovery, rolling reconnect controls and preStop draining; clients opt in with subscribe(rebalance=True)                                                                                       | `false` |
| `events.rebalance.routing`              | **Type: string.** Service keeps the configured authority and delegates routing to Kubernetes or Istio; Direct lets reachable in-cluster clients round-robin discovered Pod IPs while retaining Host and TLS identity                             | `Service` |
| `events.rebalance.refreshSeconds`       | **Type: number.** Refresh ready nonterminating event Pods every 1-60 seconds; discovery rejects membership older than three refresh intervals                                                                                                    | `5` |
| `events.rebalance.automatic`            | **Type: boolean.** Schedule a reconnect roll when the ready operator membership changes; manual Service annotation triggers remain available when false                                                                                          | `true` |
| `events.rebalance.batchPercent`         | **Type: integer.** Schedule at most this percentage of each replica's initial subscribers per batch (1-100, rounded up to one); this is a per-replica pacing limit                                                                               | `10` |
| `events.rebalance.intervalSeconds`      | **Type: number.** Separate ordinary batches by 0.1-30 seconds and jitter reconnections; termination compresses batches to fit drainSeconds                                                                                                       | `2` |
| `events.rebalance.cooldownSeconds`      | **Type: number.** Coalesce membership and manual triggers behind this per-replica cooldown (1-3600 seconds); termination drains take precedence                                                                                                  | `60` |
| `events.rebalance.drainSeconds`         | **Type: number.** Keep the process alive for 5-300 seconds after preStop closes subscription admission; terminationGracePeriodSeconds must also allow 35 seconds for shutdown plus another drainSeconds when the native Istio sidecar is enabled | `20` |
| `events.rebalance.maxConnectionSeconds` | **Type: number.** Optional connection age limit (0-86400 seconds); zero disables periodic rotation, otherwise use at least cooldownSeconds                                                                                                       | `0` |
| `events.istio.enabled`                  | **Type: boolean.** Render an events DestinationRule; requires mesh.operator.enabled, event rebalancing and Service routing                                                                                                                       | `false` |
| `events.istio.loadBalancer`             | **Type: string.** LEAST_REQUEST favors replicas with fewer outstanding requests; ROUND_ROBIN cycles through ready replicas when establishing new subscriptions                                                                                   | `LEAST_REQUEST` |
| `events.istio.warmupSeconds`            | **Type: number.** Gradually introduce new ready endpoints over this many seconds (1-300); affects new requests, never moves an existing WebSocket                                                                                                | `30` |

### Scheduler metrics

| Name                                    | Description                                                                                                                                                                     | Value            |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `metrics.enabled`                       | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement    | `false` |
| `metrics.graphLabels`                   | **Type: boolean.** Expose per-graph topology, policy, Cheeger, spectrum, throughput and workload-signal series; increases cardinality, does not gate JSON or KEDA scalar routes | `false` |
| `metrics.graphSpectra`                  | **Type: boolean.** Retain already-calculated eigenvalues for graph diagnostics; adds per-rule storage and time series when graphLabels is enabled.                              | `false` |
| `metrics.authentication.enabled`        | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                    | `false` |
| `metrics.authentication.existingSecret` | **Type: string.** Existing or ESO-managed Secret containing the metrics token                                                                                                   | `polyad-metrics` |
| `metrics.authentication.secretKey`      | **Type: string.** Key containing the metrics bearer token                                                                                                                       | `token` |
| `metrics.authentication.key`            | **Type: string.** Inline metrics token; requires existingSecret to be empty                                                                                                     | `""` |

### Named operator API credentials

| Name                                      | Description                                                                                                                                                                           | Value                   |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| `authentication.mode`                     | **Type: string.** String. Required enforces endpoint credentials; Disabled opts into an unauthenticated demo without HTTP request quotas                                              | `Required` |
| `authentication.backend`                  | **Type: string.** String. Builtin uses Polyad's Flask hooks; FlaskHTTPAuth loads the adapter included in official operator images                                                     | `Builtin` |
| `authentication.storage.enabled`          | **Type: boolean.** Boolean. Persist one-way key verifiers and policy records in PostgreSQL and enforce database revocation; raw tokens stay in Kubernetes Secrets                     | `false` |
| `authentication.storage.separateDatabase` | **Type: boolean.** Boolean. Use an isolated authentication database; false reuses the state database and its role, requiring postgresql.enabled                                       | `true` |
| `authentication.storage.managed`          | **Type: boolean.** Boolean. Provision a separate CloudNativePG Cluster; false uses the DSN from existingSecret                                                                        | `true` |
| `authentication.storage.existingSecret`   | **Type: string.** String. Externally managed authentication database connection Secret; used with separateDatabase=true and managed=false                                             | `""` |
| `authentication.storage.secretKey`        | **Type: string.** String. DSN key in the external authentication database Secret                                                                                                      | `uri` |
| `authentication.storage.database`         | **Type: string.** String. Database name for a new managed authentication cluster                                                                                                      | `polyad-authentication` |
| `authentication.storage.username`         | **Type: string.** String. Sole application login owning the managed authentication database; PostgreSQL administrators retain administrative access                                   | `polyad_authentication` |
| `authentication.storage.size`             | **Type: string.** String. Persistent storage per managed authentication database instance                                                                                             | `1Gi` |
| `authentication.storage.storageClass`     | **Type: string.** Authentication database storage class; empty uses the cluster default unless postgresql.encryptionAtRest selects a class; conflicting explicit classes are rejected | `""` |
| `authentication.services`                 | **Type: array.** Service API keys with direction, Secret reference, endpoint scopes, outbound baseUrl and individual rate/concurrency limits                                          | `[]` |
| `authentication.operators`                | **Type: array.** Peer operator API keys; Inbound, Outbound or Bidirectional, with HA-wide per-key lanes                                                                               | `[]` |

### KEDA installation, observation and credentials

| Name                                                               | Description                                                                                                                                                 | Value   |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `keda.install`                                                     | **Type: boolean.** Install the pinned upstream KEDA chart in this release namespace; leave false to use an existing cluster installation                    | `false` |
| `keda.autoscaling.enabled`                                         | **Type: boolean.** Autoscale enabled KEDA metrics-server and webhook components in HA mode; false keeps their upstream replica counts fixed                 | `true` |
| `keda.autoscaling.metricsServer.enabled`                           | **Type: boolean.** Create an HPA when the bundled metrics server is enabled                                                                                 | `true` |
| `keda.autoscaling.metricsServer.minReplicas`                       | **Type: integer.** Minimum metrics-server Pods; at least two for availability                                                                               | `2` |
| `keda.autoscaling.metricsServer.maxReplicas`                       | **Type: integer.** Maximum metrics-server Pods; at least minReplicas and at most 32                                                                         | `5` |
| `keda.autoscaling.metricsServer.targetCPUUtilizationPercentage`    | **Type: integer.** Target CPU utilization of the KEDA container relative to its CPU request                                                                 | `70` |
| `keda.autoscaling.metricsServer.targetMemoryUtilizationPercentage` | **Type: integer or null.** Target memory utilization of the KEDA container relative to its memory request; null disables memory scaling                     | `80` |
| `keda.autoscaling.webhooks.enabled`                                | **Type: boolean.** Create an HPA when bundled admission webhooks are enabled                                                                                | `true` |
| `keda.autoscaling.webhooks.minReplicas`                            | **Type: integer.** Minimum admission-webhook Pods; at least two for availability                                                                            | `2` |
| `keda.autoscaling.webhooks.maxReplicas`                            | **Type: integer.** Maximum admission-webhook Pods; at least minReplicas and at most 32                                                                      | `5` |
| `keda.autoscaling.webhooks.targetCPUUtilizationPercentage`         | **Type: integer.** Target CPU utilization of the KEDA container relative to its CPU request                                                                 | `70` |
| `keda.autoscaling.webhooks.targetMemoryUtilizationPercentage`      | **Type: integer or null.** Target memory utilization of the KEDA container relative to its memory request; null disables memory scaling                     | `80` |
| `keda.autoscaling.behavior.scaleUp.stabilizationWindowSeconds`     | **Type: integer.** Seconds of scale-up recommendations to retain; zero responds immediately                                                                 | `0` |
| `keda.autoscaling.behavior.scaleDown.stabilizationWindowSeconds`   | **Type: integer.** Seconds of scale-down recommendations to retain; 300 limits churn after a demand spike                                                   | `300` |
| `keda.observation.enabled`                                         | **Type: boolean.** Include KEDA workloads and Services in the reserved root Graph when root mode is enabled; disable if this installation does not use KEDA | `true` |
| `keda.observation.namespace`                                       | **Type: string.** Namespace of an existing KEDA installation; bundled KEDA always uses the release namespace                                                | `keda` |
| `keda.observation.deployments`                                     | **Type: array.** Existing KEDA Deployment names to observe; remove disabled components or replace customized names; ignored for bundled KEDA                | `["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]` |
| `keda.observation.services`                                        | **Type: array.** Existing KEDA Service names to observe; match the installed KEDA chart; ignored for bundled KEDA                                           | `["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]` |
| `keda.authentication.enabled`                                      | **Type: boolean.** Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics                                  | `false` |
| `keda.authentication.name`                                         | **Type: string.** TriggerAuthentication name; empty uses the release metrics name                                                                           | `""` |
| `kedaOperator.operator.replicaCount`                               | **Type: integer.** Fixed KEDA operator replicas; defaults to two even for singular Polyad and must stay at least two in HA mode                             | `2` |
| `kedaOperator.metricsServer.replicaCount`                          | **Type: integer.** Initial metrics-server Pods; the HPA owns subsequent counts when bundled HA autoscaling is enabled                                       | `2` |
| `kedaOperator.webhooks.enabled`                                    | **Type: boolean.** Enable admission validation for KEDA resources.                                                                                          | `true` |
| `kedaOperator.webhooks.replicaCount`                               | **Type: integer.** Initial admission-webhook Pods; the HPA owns subsequent counts when bundled HA autoscaling is enabled                                    | `2` |
| `kedaOperator.prometheus.operator.enabled`                         | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                               | `true` |
| `kedaOperator.prometheus.metricServer.enabled`                     | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                               | `true` |
| `kedaOperator.prometheus.webhooks.enabled`                         | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                               | `true` |

### Optional External Secrets Operator resources

| Name                                  | Description                                                                                                                                                                                                          | Value         |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `externalSecrets.enabled`             | **Type: boolean.** Generate ExternalSecret resources; requires an installed ESO operator and SecretStore                                                                                                             | `false` |
| `externalSecrets.reloadOnChange`      | **Type: boolean.** Opt into Stakater Reloader annotations for ESO Secrets, operator and observer Deployments, and Daemons with spec.reloadOnSecretChange; requires externalSecrets.enabled and an installed Reloader | `false` |
| `externalSecrets.refreshInterval`     | **Type: string.** ESO credential refresh interval                                                                                                                                                                    | `1h` |
| `externalSecrets.secretStoreRef.name` | **Type: string.** Existing SecretStore or ClusterSecretStore name                                                                                                                                                    | `""` |
| `externalSecrets.secretStoreRef.kind` | **Type: string.** Secret store reference kind                                                                                                                                                                        | `SecretStore` |
| `externalSecrets.secrets`             | **Type: array.** Secret mappings with name and data entries of secretKey and remoteRef; reference names via existingSecret settings                                                                                  | `[]` |

### Operator endpoint and cache isolation

| Name                             | Description                                                                                               | Value   |
| -------------------------------- | --------------------------------------------------------------------------------------------------------- | ------- |
| `networkPolicy.enabled`          | **Type: boolean.** Isolate operator pods; requires explicit Kubernetes API and cache egress rules         | `false` |
| `networkPolicy.apiServerCIDRs`   | **Type: array.** Kubernetes API endpoint CIDRs reachable through this cluster's CNI                       | `[]` |
| `networkPolicy.apiServerPort`    | **Type: integer.** Kubernetes API endpoint port after this cluster's service translation                  | `443` |
| `networkPolicy.compositionPeers` | **Type: array.** NetworkPolicy peers allowed to connect to the composition API                            | `[]` |
| `networkPolicy.eventPeers`       | **Type: array.** NetworkPolicy peers allowed to subscribe to events                                       | `[]` |
| `networkPolicy.metricsPeers`     | **Type: array.** Monitoring peers allowed to scrape the metrics API on port 8092                          | `[]` |
| `networkPolicy.healthPeers`      | **Type: array.** Optional monitoring peers allowed to read port 8080 health metrics                       | `[]` |
| `networkPolicy.extraEgress`      | **Type: array.** Additional NetworkPolicy egress rules, including any external cache or DNS configuration | `[]` |

### Optional Istio integration

| Name                                                              | Description                                                                                                                                        | Value                                                         |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `mesh.enabled`                                                    | **Type: boolean.** Allow graph HTTP and service-identity authorization and generate Istio security resources                                       | `false` |
| `mesh.install`                                                    | **Type: boolean.** Install the pinned upstream Istio base and istiod dependencies; requires mesh.enabled                                           | `false` |
| `mesh.multicluster.enabled`                                       | **Type: boolean.** Enable the existing or bundled sidecar mesh's multicluster configuration                                                        | `false` |
| `mesh.multicluster.eastWest.enabled`                              | **Type: boolean.** Install a dedicated east-west gateway for separate networks                                                                     | `false` |
| `mesh.multicluster.eastWest.portName`                             | **Type: string.** Istio Gateway listener name; protocol TLS and AUTO_PASSTHROUGH mode are required by this integration                             | `tls` |
| `mesh.multicluster.eastWest.hosts`                                | **Type: array.** Service SNI suffixes exposed through AUTO_PASSTHROUGH                                                                             | `["*.local"]` |
| `mesh.multicluster.peers`                                         | **Type: array.** Remote transport registrations; see docs/deployment/multicluster.md for same-network and gateway configurations                   | `[]` |
| `mesh.multicluster.routing.enabled`                               | **Type: boolean.** Add endpoint ejection and locality-aware routing to enabled Polyad Services                                                     | `false` |
| `mesh.multicluster.routing.mode`                                  | **Type: string.** LocalFirst uses Istio locality priorities; Failover adds ordered region failovers; Distributed applies explicit locality weights | `LocalFirst` |
| `mesh.multicluster.routing.failover`                              | **Type: array.** Ordered region mappings with from and to fields, used only in Failover mode                                                       | `[]` |
| `mesh.multicluster.routing.distribute`                            | **Type: array.** Locality mappings with from and a to weight map, used only in Distributed mode                                                    | `[]` |
| `mesh.multicluster.routing.consecutive5xxErrors`                  | **Type: integer.** Consecutive server errors before an endpoint is ejected                                                                         | `5` |
| `mesh.multicluster.routing.intervalSeconds`                       | **Type: integer.** Endpoint health analysis interval                                                                                               | `10` |
| `mesh.multicluster.routing.baseEjectionSeconds`                   | **Type: integer.** Minimum endpoint ejection duration                                                                                              | `30` |
| `mesh.multicluster.routing.maxEjectionPercent`                    | **Type: integer.** Maximum percentage of endpoints ejected from a Service                                                                          | `50` |
| `mesh.proxyResources.cpu`                                         | **Type: string.** CPU request and limit for the Istio proxy and init containers; tune alongside operator CPU from Grafana                          | `100m` |
| `mesh.proxyResources.memory`                                      | **Type: string.** Memory request and limit for the Istio proxy and init containers; matching pairs preserve Guaranteed QoS                         | `128Mi` |
| `mesh.operator.enabled`                                           | **Type: boolean.** Inject the operator pods and authorize their API and event ports with Istio                                                     | `false` |
| `mesh.operator.compositionPrincipals`                             | **Type: array.** Exact mTLS source identities allowed to use the composition endpoint                                                              | `[]` |
| `mesh.operator.metricsPrincipals`                                 | **Type: array.** Exact mTLS source identities allowed to read scheduler metrics                                                                    | `[]` |
| `mesh.operator.eventPrincipals`                                   | **Type: array.** Exact mTLS source identities allowed to subscribe to events                                                                       | `[]` |
| `mesh.operator.connectionPrincipals`                              | **Type: array.** Exact mTLS source identities allowed to request temporary connections                                                             | `[]` |
| `mesh.telemetry.enabled`                                          | **Type: boolean.** Render a workload-scoped Istio Telemetry resource for Polyad operator Pods                                                      | `false` |
| `mesh.telemetry.metricsProviders`                                 | **Type: array.** Istio extension providers receiving proxy metrics                                                                                 | `["prometheus"]` |
| `mesh.telemetry.accessLogging.enabled`                            | **Type: boolean.** Export filtered Envoy access logs                                                                                               | `true` |
| `mesh.telemetry.accessLogging.providers`                          | **Type: array.** Istio extension providers receiving access logs                                                                                   | `["envoy"]` |
| `mesh.telemetry.accessLogging.filter`                             | **Type: string.** CEL filter; the default records failures and requests slower than one second                                                     | `response.code >= 500 \|\| response.duration >= duration('1s')` |
| `mesh.telemetry.tracing.enabled`                                  | **Type: boolean.** Export proxy spans through configured Istio extension providers                                                                 | `false` |
| `mesh.telemetry.tracing.providers`                                | **Type: array.** Istio extension providers receiving proxy spans                                                                                   | `[]` |
| `mesh.telemetry.tracing.randomSamplingPercentage`                 | **Type: number.** Percentage of requests independently sampled by Envoy                                                                            | `1` |
| `mesh.sidecar.enabled`                                            | **Type: boolean.** Render a workload-scoped Istio Sidecar resource                                                                                 | `false` |
| `mesh.sidecar.egressHosts`                                        | **Type: array.** Namespace/host patterns visible to operator proxies, such as ./\* and istio-system/\*                                             | `["./*"]` |
| `mesh.authorization.audit.enabled`                                | **Type: boolean.** Audit matching requests without changing the allow decision                                                                     | `false` |
| `mesh.authorization.audit.paths`                                  | **Type: array.** Istio path patterns to audit                                                                                                      | `[]` |
| `mesh.authorization.audit.methods`                                | **Type: array.** HTTP methods to audit; empty matches every method                                                                                 | `[]` |
| `mesh.authorization.dryRunDeny.enabled`                           | **Type: boolean.** Evaluate a DENY policy in Istio dry-run mode without rejecting requests                                                         | `false` |
| `mesh.authorization.dryRunDeny.paths`                             | **Type: array.** Istio path patterns that would be denied                                                                                          | `[]` |
| `mesh.authorization.dryRunDeny.methods`                           | **Type: array.** HTTP methods that would be denied; empty matches every method                                                                     | `[]` |
| `mesh.egress.enabled`                                             | **Type: boolean.** Render namespace-scoped ServiceEntries for declared external destinations                                                       | `false` |
| `mesh.egress.destinations`                                        | **Type: array.** External destinations with unique name, DNS host, port and protocol                                                               | `[]` |
| `mesh.egress.gateway.enabled`                                     | **Type: boolean.** Install a dedicated Istio gateway and route declared destinations through it                                                    | `false` |
| `mesh.ingress.enabled`                                            | **Type: boolean.** Install the optional upstream Istio gateway dependency                                                                          | `false` |
| `mesh.ingress.hosts`                                              | **Type: array.** Hosts served by the Istio Gateway and VirtualService                                                                              | `[]` |
| `mesh.ingress.tlsSecret`                                          | **Type: string.** TLS credential Secret in the gateway namespace, required when exposing the APIs                                                  | `""` |
| `mesh.ingress.gatewayAPI.enabled`                                 | **Type: boolean.** Create a Gateway API Gateway and HTTPRoute without installing the legacy gateway dependency                                     | `false` |
| `mesh.ingress.gatewayAPI.className`                               | **Type: string.** GatewayClass used for the managed ingress                                                                                        | `istio` |
| `mesh.ingress.jwt.enabled`                                        | **Type: boolean.** Require a valid JWT at either managed ingress gateway                                                                           | `false` |
| `mesh.ingress.jwt.issuer`                                         | **Type: string.** Exact JWT issuer                                                                                                                 | `""` |
| `mesh.ingress.jwt.audiences`                                      | **Type: array.** Accepted JWT audience claims                                                                                                      | `[]` |
| `mesh.ingress.jwt.jwksUri`                                        | **Type: string.** Optional explicit JWKS endpoint; empty uses OpenID discovery                                                                     | `""` |
| `istioBase`                                                       | **Type: object.** Upstream Istio base chart overrides                                                                                              | `{}` |
| `istiod.env.ENABLE_NATIVE_SIDECARS`                               | **Type: string.** Settings for istiod.env.ENABLE_NATIVE_SIDECARS.                                                                                  | `true` |
| `istiod.meshConfig.enableAutoMtls`                                | **Type: boolean.** Settings for istiod.meshConfig.enableAutoMtls.                                                                                  | `true` |
| `istiod.meshConfig.defaultConfig.holdApplicationUntilProxyStarts` | **Type: boolean.** Settings for istiod.meshConfig.defaultConfig.holdApplicationUntilProxyStarts.                                                   | `true` |
| `istioIngress.labels.istio`                                       | **Type: string.** Settings for istioIngress.labels.istio.                                                                                          | `polyad-ingress` |
| `istioEastWestGateway.name`                                       | **Type: string.** Settings for istioEastWestGateway.name.                                                                                          | `polyad-eastwest` |
| `istioEastWestGateway.labels.istio`                               | **Type: string.** Settings for istioEastWestGateway.labels.istio.                                                                                  | `polyad-eastwest` |
| `istioEastWestGateway.labels.networking.istio.io/gatewayPort`     | **Type: string.** Advertised TLS Service port; update it together with networkGatewayPorts.tls.port.                                               | `15443` |
| `istioEastWestGateway.networkGateway`                             | **Type: string or null.** Null retains the pinned upstream Istio profile default.                                                                  | `""` |
| `istioEastWestGateway.networkGatewayPorts.status-port.port`       | **Type: integer.** Settings for istioEastWestGateway.networkGatewayPorts.status-port.port.                                                         | `15021` |
| `istioEastWestGateway.networkGatewayPorts.status-port.targetPort` | **Type: integer.** Settings for istioEastWestGateway.networkGatewayPorts.status-port.targetPort.                                                   | `15021` |
| `istioEastWestGateway.networkGatewayPorts.tls.port`               | **Type: integer.** Settings for istioEastWestGateway.networkGatewayPorts.tls.port.                                                                 | `15443` |
| `istioEastWestGateway.networkGatewayPorts.tls.targetPort`         | **Type: integer.** Settings for istioEastWestGateway.networkGatewayPorts.tls.targetPort.                                                           | `15443` |
| `istioEgress.name`                                                | **Type: string.** Stable Service name referenced by generated egress routes                                                                        | `polyad-egress` |
| `istioEgress.labels.istio`                                        | **Type: string.** Selector used by the generated Istio Gateway                                                                                     | `polyad-egress` |

### Shared Istio namespace

| Name                              | Description                                                                                                    | Value          |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------- | -------------- |
| `global.istioNamespace`           | **Type: string.** Istio control-plane namespace; must equal the release namespace when mesh.install is enabled | `istio-system` |
| `global.meshID`                   | **Type: string.** Shared mesh identity across participating clusters                                           | `""` |
| `global.network`                  | **Type: string.** Network containing this cluster; different networks use east-west gateways                   | `""` |
| `global.multiCluster.clusterName` | **Type: string.** Unique, stable cluster identity used by Istio and Polyad                                     | `""` |

### Cross-cluster PolyGraph management

| Name                  | Description                                                                                                    | Value   |
| --------------------- | -------------------------------------------------------------------------------------------------------------- | ------- |
| `federation.enabled`  | **Type: boolean.** Allow PolyGraph nodes to deploy Graphs and nested PolyGraphs in registered clusters         | `false` |
| `federation.clusters` | **Type: array.** Cluster name, namespace and kubeconfigSecret registrations; credentials are mounted read-only | `[]` |

### Optional shared graph observers

| Name                                 | Description                                                                                                                              | Value   |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `observer.enabled`                   | **Type: boolean.** Deploy shared read-only observers independently of execution operators                                                | `false` |
| `observer.replicaCount`              | **Type: integer.** Number of stateless read replicas in this cluster and namespace                                                       | `1` |
| `observer.existingSecret`            | **Type: string.** Existing Secret containing a dedicated read credential under token                                                     | `""` |
| `observer.mesh`                      | **Type: boolean.** Enable native sidecar injection, strict mTLS and source-identity authorization for observer Pods                      | `false` |
| `observer.principals`                | **Type: array.** Exact source principals allowed to read the observer when observer.mesh is enabled                                      | `[]` |
| `observer.peers`                     | **Type: array.** NetworkPolicy peers allowed to reach observer TCP 8094 when networkPolicy.enabled is set                                | `[]` |
| `observer.resources.requests.cpu`    | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `1` |
| `observer.resources.requests.memory` | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `1Gi` |
| `observer.resources.limits.cpu`      | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `1` |
| `observer.resources.limits.memory`   | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `1Gi` |

### Advance graph capacity

| Name                             | Description                                                                                                 | Value                                              |
| -------------------------------- | ----------------------------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `capacity.enabled`               | **Type: boolean.** Allow graph capacity plans to create autoscaler requests and inert placeholder Pods      | `false` |
| `capacity.provisioningClassName` | **Type: string.** Autoscaler class used when a graph does not select one                                    | `best-effort-atomic-scale-up.autoscaling.x-k8s.io` |
| `capacity.maxPods`               | **Type: integer.** Maximum forecast Pods per graph boundary, also bounded by each graph's maxPods           | `128` |
| `capacity.placeholderImage`      | **Type: string.** Inert image used to expose advance scheduling demand                                      | `registry.k8s.io/pause:3.10` |
| `capacity.priorityClass.create`  | **Type: boolean.** Install a release-scoped PriorityClass for placeholder Pods                              | `true` |
| `capacity.priorityClass.name`    | **Type: string.** Existing PriorityClass name, or an override for the generated name                        | `""` |
| `capacity.priorityClass.value`   | **Type: integer.** Placeholder priority; must meet the autoscaler's cutoff and be below workload priorities | `-5` |

### Root control plane

| Name                                 | Description                                                                                                                                                          | Value   |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `rootControlPlane.enabled`           | **Type: boolean.** Manage registered clusters and remote execution replicas through one root scheduler; requires ha                                                  | `false` |
| `rootControlPlane.pools`             | **Type: array.** Root-owned OperatorPools; set existingDeployment to attach a Helm-installed worker and scalingAuthority to Root or Local instead of provisioning it | `[]` |
| `rootControlPlane.kubeconfigSecret`  | **Type: string.** Existing root-namespace Secret with embedded, verified root kubeconfig under config, reachable from worker clusters                                | `""` |
| `rootControlPlane.meshPeers`         | **Type: array.** Complete workload-cluster mesh peer registry; the controller excludes its current execution cluster                                                 | `[]` |
| `rootControlPlane.endpoints.api`     | **Type: string.** Externally reachable root composition API URL advertised to workloads                                                                              | `""` |
| `rootControlPlane.endpoints.events`  | **Type: string.** Externally reachable root events URL advertised to workloads                                                                                       | `""` |
| `rootControlPlane.endpoints.metrics` | **Type: string.** Externally reachable root metrics URL advertised to workloads                                                                                      | `""` |

### Telemetry collectors

| Name                                           | Description                                                                                                                                                                                                    | Value                                   |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------- |
| `telemetry.enabled`                            | **Type: boolean.** Install a release-scoped collector; backends are supplied separately.                                                                                                                       | `false` |
| `telemetry.mode`                               | **Type: string.** Alloy collects metrics, container logs and traces; PrometheusAgent collects metrics only.                                                                                                    | `Alloy` |
| `telemetry.replicas`                           | **Type: integer.** Alloy distributes scrape and log targets across these replicas; PrometheusAgent requires one.                                                                                               | `1` |
| `telemetry.alloyImage`                         | **Type: string.** Pinned Grafana Alloy image.                                                                                                                                                                  | `docker.io/grafana/alloy:v1.19.2` |
| `telemetry.prometheusImage`                    | **Type: string.** Pinned Prometheus image, launched with --agent.                                                                                                                                              | `quay.io/prometheus/prometheus:v3.14.0` |
| `telemetry.clusterName`                        | **Type: string.** Stable cluster label attached to collected signals; empty uses global.multiCluster.clusterName or the release namespace.                                                                     | `""` |
| `telemetry.scrapeIntervalSeconds`              | **Type: integer.** Seconds between exporter scrapes.                                                                                                                                                           | `15` |
| `telemetry.scrapeTimeoutSeconds`               | **Type: integer.** Exporter timeout, no greater than the scrape interval.                                                                                                                                      | `10` |
| `telemetry.sampleLimit`                        | **Type: integer.** Maximum samples accepted per scrape; protects collectors from accidental cardinality growth.                                                                                                | `100000` |
| `telemetry.remoteWrite.url`                    | **Type: string.** Prometheus-compatible remote-write URL, including its write path.                                                                                                                            | `""` |
| `telemetry.remoteWrite.credentials.secretName` | **Type: string.** Existing Secret in the release namespace; never copied into a ConfigMap or cache.                                                                                                            | `""` |
| `telemetry.remoteWrite.credentials.secretKey`  | **Type: string.** Key containing the bearer token or basic-auth password.                                                                                                                                      | `token` |
| `telemetry.remoteWrite.credentials.username`   | **Type: string.** Basic-auth username; empty selects bearer authentication.                                                                                                                                    | `""` |
| `telemetry.logs.enabled`                       | **Type: boolean.** Alloy tails selected containers through the Kubernetes API without host mounts.                                                                                                             | `true` |
| `telemetry.logs.url`                           | **Type: string.** Loki push URL including /loki/api/v1/push.                                                                                                                                                   | `""` |
| `telemetry.logs.credentials.secretName`        | **Type: string.** Existing Secret in the release namespace; never copied into a ConfigMap or cache.                                                                                                            | `""` |
| `telemetry.logs.credentials.secretKey`         | **Type: string.** Key containing the bearer token or basic-auth password.                                                                                                                                      | `token` |
| `telemetry.logs.credentials.username`          | **Type: string.** Basic-auth username; empty selects bearer authentication.                                                                                                                                    | `""` |
| `telemetry.traces.enabled`                     | **Type: boolean.** Alloy accepts OTLP/gRPC and OTLP/HTTP traces on Pod IPs and forwards them over OTLP/HTTP.                                                                                                   | `true` |
| `telemetry.traces.endpoint`                    | **Type: string.** Destination OTLP/HTTP base URL, without /v1/traces.                                                                                                                                          | `""` |
| `telemetry.traces.routeOperator`               | **Type: boolean.** Route Helm-installed operator and observer traces through local Alloy; tracing.enabled must also be true. Use false for root-provisioned cross-cluster workers requiring a shared endpoint. | `false` |
| `telemetry.traces.credentials.secretName`      | **Type: string.** Existing Secret in the release namespace; never copied into a ConfigMap or cache.                                                                                                            | `""` |
| `telemetry.traces.credentials.secretKey`       | **Type: string.** Key containing the bearer token or basic-auth password.                                                                                                                                      | `token` |
| `telemetry.traces.credentials.username`        | **Type: string.** Basic-auth username; empty selects bearer authentication.                                                                                                                                    | `""` |
| `telemetry.operatorMetricsSecret.name`         | **Type: string.** Override with a named API-key Secret authorized for metrics; empty inherits metrics.authentication.                                                                                          | `""` |
| `telemetry.operatorMetricsSecret.key`          | **Type: string.** Secret key containing the metrics bearer token.                                                                                                                                              | `token` |
| `telemetry.storage.enabled`                    | **Type: boolean.** Keep the metric WAL on a per-replica PVC; false uses disposable emptyDir storage.                                                                                                           | `true` |
| `telemetry.storage.size`                       | **Type: string.** WAL volume size.                                                                                                                                                                             | `2Gi` |
| `telemetry.storage.storageClass`               | **Type: string.** StorageClass name; empty uses the cluster default.                                                                                                                                           | `""` |
| `telemetry.resources.requests.cpu`             | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.                                                                       | `100m` |
| `telemetry.resources.requests.memory`          | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.                                                                       | `128Mi` |
| `telemetry.resources.limits.memory`            | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.                                                                       | `512Mi` |
| `telemetry.nodeSelector`                       | **Type: object.** Collector placement; empty inherits operator.nodeSelector.                                                                                                                                   | `{}` |
| `telemetry.tolerations`                        | **Type: array.** Collector tolerations; empty inherits operator.tolerations.                                                                                                                                   | `[]` |
| `telemetry.extraTargets`                       | **Type: array.** Additional namespace-local exporters and log selectors, for separately installed components. Each namespace receives read-only discovery RBAC.                                                | `[]` |
| `telemetry.traceNamespaces`                    | **Type: array.** Additional namespaces allowed to send traces when networkPolicy.enabled; the release namespace is always allowed.                                                                             | `[]` |

<!-- The parameters table is maintained by the helm-readme-generator pre-commit hook. -->
