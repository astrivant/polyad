# Polyad Helm chart

Deploy the Kubernetes workload scheduler and its shared Dragonfly queue. See the
[operator guide](../../docs/deployment/operator.md) for graph semantics, replica coordination,
health metrics, and installation examples.

## Table of contents

- [Installation](#installation)
- [Reference values](#reference-values)
- [Template layout](#template-layout)
- [Parameters](#parameters)
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

## Installation

Install and upgrade output lists configured public routes, enabled internal API
endpoints and local port-forward commands. View it again with
`helm get notes RELEASE -n NAMESPACE`. Existing Gateway listeners and addresses
are discovered using the commands in the notes; credentials are never printed.

Use Helm `operator.nodeSelector` and `operator.tolerations` to select the operator's node group.
Graph CR placement controls workload pods independently; enforced graph placement
is copied into their native templates. The Python `polyad.cache` package connects
replicas using `POLYAD_CACHE_URL`, including external Redis-compatible endpoints.

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
[KEDA reference values](values-keda.reference.yaml) describe bundled and existing
installations. Then enable cache HA with an initial primary and one replica:

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

Polyad ships the pinned Dragonfly CRD in `crds/` so Helm registers it before
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
[authentication reference](values-authentication.reference.yaml) includes all
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

The commented `values-*.reference.yaml` files highlight settings for each profile
and optional extension. Copy and adapt the files you need, then pass them with
`--values`; Helm does not load them automatically. [`values.yaml`](values.yaml)
remains the complete default configuration. Set the single top-level `ha` flag
to false (default) or true. Each reference uses a generated partial editor schema
with canonical field types; Helm validates all requirements after merging defaults.

Each `@param` comment declares the accepted type, such as `[string]`, `[boolean]`,
`[integer]` or `[number]`. Collections use `[array]` or `[object]`, and `nullable`
allows `null`. The parameter tables below show these types alongside the actual
default values. Schema checks keep annotations in all shipped values files in sync.

| Reference file | Configuration and placement | Guide |
| --- | --- | --- |
| [`values-singular.reference.yaml`](values-singular.reference.yaml) | One dense operator and a persistent cache in the release cluster | [Singular](../../docs/deployment/deployment-profiles.md#one-dense-operator) |
| [`values-ha.reference.yaml`](values-ha.reference.yaml) | Replicated dense operators; also opts into cache HA | [HA](../../docs/deployment/deployment-profiles.md#ha-in-one-cluster) |
| [`values-keda.reference.yaml`](values-keda.reference.yaml) | Optional bundled KEDA installation and observation in the root operator Graph | [KEDA installation](../../docs/deployment/local-services.md#install-keda-with-the-chart) |
| [`values-components.reference.yaml`](values-components.reference.yaml) | HA bootstrap plus a self-managed gateway/executor/telemetry Graph and KEDA scaling in the release cluster | [Components](../../docs/deployment/components.md) |
| [`values-federation.reference.yaml`](values-federation.reference.yaml) | Remote cluster registrations for PolyGraph placement; destinations have independent execution operators | [Federation](../../docs/deployment/multicluster.md#placement-and-ownership) |
| [`values-root-control-plane.reference.yaml`](values-root-control-plane.reference.yaml) | HA management release that installs and controls remote execution pools | [Root control plane](../../docs/deployment/root-control-plane.md) |
| [`values-worker.reference.yaml`](values-worker.reference.yaml) | Downstream Helm-owned executors attached to a root, with explicit root or local scaling authority | [Helm workers](../../docs/deployment/helm-workers.md) |
| [`values-multicluster.reference.yaml`](values-multicluster.reference.yaml) | Istio transport, peer gateways and local network identity; adapt separately per cluster | [Multicluster networking](../../docs/deployment/multicluster.md#istio-across-different-networks) |
| [`values-observer.reference.yaml`](values-observer.reference.yaml) | Read-only observers alongside this release's operator | [Observers](../../docs/deployment/multicluster.md#optional-shared-observers) |
| [`values-postgresql.reference.yaml`](values-postgresql.reference.yaml) | Optional persistent state, database HA and connection-driven KEDA scaling in the release cluster | [PostgreSQL](../../docs/deployment/postgresql.md) |
| [`values-authentication.reference.yaml`](values-authentication.reference.yaml) | Scoped service/operator keys, workload Secret assignments and optional dedicated authentication storage | [API keys](../../docs/operations/api-keys.md) |
| [`values-connections.reference.yaml`](values-connections.reference.yaml) | Service consent events and separate connection/reconciliation pulse budgets | [Temporary connections](../../docs/apis/temporary-connections.md) |
| [`values-demo.reference.yaml`](values-demo.reference.yaml) | Public demonstration endpoints without authentication or HTTP quotas | [Demo mode](../../docs/operations/api-keys.md#demonstrations-without-authentication) |
| [`values-tracing.reference.yaml`](values-tracing.reference.yaml) | OTLP/HTTP traces and independent decision logs, parent-based trace sampling and optional exporter credentials | [OpenTelemetry traces and logs](../../docs/operations/tracing.md) |
| [`values-tuning.reference.yaml`](values-tuning.reference.yaml) | Work-graph worker and planner limits, validation cadence, polling intervals, Cheeger computation ceilings and metrics | [Write-pipeline configuration](../../docs/development/write-pipeline.md#configuration), [performance tuning](../../docs/operations/performance.md) |

Each file can render with chart defaults. Installation also requires the
infrastructure and Secrets called out in its comments. Replace example cluster
names, addresses, CIDRs and Secret references with your environment's values.
Observer values add observers to an operator release; they do not create an
observer-only release.

For example, combine HA, split components and optional PostgreSQL:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --values charts/polyad/values-ha.reference.yaml \
  --values charts/polyad/values-components.reference.yaml \
  --values charts/polyad/values-postgresql.reference.yaml
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
for metric behavior, prerequisites and an example with both targets.

Federation, mesh and observers remain optional. Root-managed execution requires
HA, using either Dense or Distributed mode. Remote execution Deployments
are created by the root operator from OperatorPools, rather than rendered separately
by Helm. `NOTES.txt` remains at the template root, and install-time CRDs remain in
`crds/`. Directory placement organizes the source; values select the rendered
resources.

See [Dense and Distributed deployments](../../docs/deployment/components.md) and
[the root control plane](../../docs/deployment/root-control-plane.md) for architecture details.

## Parameters

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
| `worker.rootGraph`        | **Type: string.** Name of the root's reserved PolyGraph; normally ROOT_RELEASE-operators                                                           | `""` |
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
| `operator.logLevel`                                                  | **Type: string.** Operator and observer Python logging verbosity; INFO for normal operation, DEBUG for reconciliation diagnostics (DEBUG, INFO, WARNING, ERROR or CRITICAL)                                                              | `INFO` |
| `operator.replicaCount`                                              | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha, and must remain null for Root-scaled workers                                                                                                      | `null` |
| `operator.nodeSelector`                                              | **Type: object.** Node labels selecting the operator node group, independent of workload graph placement                                                                                                                                 | `{}` |
| `operator.tolerations`                                               | **Type: array.** Taints tolerated by the operator replicas                                                                                                                                                                               | `[]` |
| `operator.autoscaling.enabled`                                       | **Type: boolean.** Enable operator HPA using CPU and optional memory utilization                                                                                                                                                         | `false` |
| `operator.autoscaling.minReplicas`                                   | **Type: integer.** Minimum operator replicas                                                                                                                                                                                             | `2` |
| `operator.autoscaling.maxReplicas`                                   | **Type: integer.** Maximum operator replicas, at least minReplicas and at most 32                                                                                                                                                        | `8` |
| `operator.autoscaling.targetCPUUtilizationPercentage`                | **Type: integer.** Target operator CPU utilization relative to requested CPU                                                                                                                                                             | `70` |
| `operator.autoscaling.targetMemoryUtilizationPercentage`             | **Type: integer or null.** Optional target memory utilization relative to requested memory (1-100); null disables the memory metric                                                                                                      | `null` |
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
| `operator.tuning.rescanIntervalSeconds`                              | **Type: number.** Pause after local and root-managed remote inventory scans (1-15s); watch polyad_inventory_sample_age_seconds and polyad_cluster_inventory_sample_fresh; lower values add Kubernetes and optional state writes          | `5` |
| `operator.tuning.consumeIntervalSeconds`                             | **Type: number.** Pause after local and remote queue consumption passes (0.1-5s); watch polyad_inbound_updates and polyad_kubernetes_writes_queued before increasing dispatch pressure                                                   | `1` |
| `operator.tuning.metricsIntervalSeconds`                             | **Type: number.** Pause after cached metrics publication and component/worker reporting (1-5s); also controls enabled PostgreSQL and Dragonfly connection sampling, not Prometheus scrape frequency or trace export                      | `5` |
| `operator.tuning.backlogIntervalSeconds`                             | **Type: number.** Pause after local shared-queue samples (1-5s); watch polyad_inbound_sample_age_seconds; root-managed remote backlog is sampled by rescanIntervalSeconds                                                                | `5` |
| `operator.image.repository`                                          | **Type: string.** Operator image repository                                                                                                                                                                                              | `ghcr.io/astrivant/polyad` |
| `operator.image.tag`                                                 | **Type: string.** Operator image tag                                                                                                                                                                                                     | `0.0.1-alpha3` |
| `operator.image.pullPolicy`                                          | **Type: string.** Operator image pull policy                                                                                                                                                                                             | `IfNotPresent` |
| `operator.resources.requests.cpu`                                    | **Type: string.** Requested operator CPU, required for CPU autoscaling                                                                                                                                                                   | `100m` |
| `operator.resources.requests.memory`                                 | **Type: string.** Requested operator memory, required when the HPA memory metric is enabled                                                                                                                                              | `128Mi` |
| `operator.resources.limits.memory`                                   | **Type: string.** Operator memory limit                                                                                                                                                                                                  | `512Mi` |
| `operator.terminationGracePeriodSeconds`                             | **Type: integer.** Time allowed for operator shutdown and outstanding API calls                                                                                                                                                          | `60` |

### Optional PostgreSQL state storage

| Name                                            | Description                                                                                                                                  | Value                                                    |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `postgresql.enabled`                            | **Type: boolean.** Persist graph state and tracked parameters in PostgreSQL                                                                  | `false` |
| `postgresql.managed`                            | **Type: boolean.** Create a CloudNativePG Cluster; install its operator first                                                                | `true` |
| `postgresql.existingSecret`                     | **Type: string.** External database Secret containing the connection DSN when managed is false                                               | `""` |
| `postgresql.secretKey`                          | **Type: string.** DSN key in the external database Secret                                                                                    | `uri` |
| `postgresql.database`                           | **Type: string.** String. Application database for graph state and optional event history; set only when bootstrapping a new managed cluster | `polyad` |
| `postgresql.username`                           | **Type: string.** String. Dedicated application owner; PUBLIC database access is revoked and superuser access stays disabled                 | `polyad` |
| `postgresql.events.enabled`                     | **Type: boolean.** Boolean. Archive approved application events in PostgreSQL; bounded live replay still uses Dragonfly                      | `true` |
| `postgresql.events.retentionDays`               | **Type: integer.** Integer. Retain durable event history for this many days; graph snapshots retain current state independently              | `30` |
| `postgresql.scope`                              | **Type: string.** State identity within the database; empty uses the release namespace and name                                              | `""` |
| `postgresql.image`                              | **Type: string.** PostgreSQL image for the managed cluster                                                                                   | `ghcr.io/cloudnative-pg/postgresql:18.3-standard-trixie` |
| `postgresql.maxConnections`                     | **Type: integer.** Maximum connections per managed database instance, including administration                                               | `100` |
| `postgresql.storage.size`                       | **Type: string.** Persistent storage per PostgreSQL instance                                                                                 | `10Gi` |
| `postgresql.storage.storageClass`               | **Type: string.** Storage class; empty uses the cluster default                                                                              | `""` |
| `postgresql.ha.enabled`                         | **Type: boolean.** Enable primary plus standby instances and synchronous replication                                                         | `false` |
| `postgresql.ha.instances`                       | **Type: integer.** Total instances when HA is enabled and autoscaling is disabled                                                            | `3` |
| `postgresql.ha.topologyKey`                     | **Type: string.** Failure-domain label for required database pod anti-affinity                                                               | `kubernetes.io/hostname` |
| `postgresql.resources.requests.cpu`             | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.     | `250m` |
| `postgresql.resources.requests.memory`          | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.     | `512Mi` |
| `postgresql.resources.limits.memory`            | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi.     | `1Gi` |
| `postgresql.autoscaling.enabled`                | **Type: boolean.** Create a KEDA ScaledObject for the managed Cluster using operator connection counts                                       | `false` |
| `postgresql.autoscaling.minInstances`           | **Type: integer.** Minimum instances; at least 3 with HA, never zero                                                                         | `3` |
| `postgresql.autoscaling.maxInstances`           | **Type: integer.** Maximum database instances                                                                                                | `6` |
| `postgresql.autoscaling.connectionsPerInstance` | **Type: integer.** Operator connections per desired database instance; does not add primary write capacity                                   | `20` |

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

| Name                    | Description                                                                                                 | Value           |
| ----------------------- | ----------------------------------------------------------------------------------------------------------- | --------------- |
| `events.enabled`        | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091           | `false` |
| `events.existingSecret` | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                  | `polyad-events` |
| `events.key`            | **Type: string.** Inline subscriber token; creates a managed Secret and requires existingSecret to be empty | `""` |
| `events.retention`      | **Type: integer.** Maximum observations retained in the shared replay stream                                | `10000` |
| `events.maxConnections` | **Type: integer.** Maximum simultaneous event subscribers per operator replica                              | `16` |

### Scheduler metrics

| Name                                    | Description                                                                                                                                                                       | Value            |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `metrics.enabled`                       | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement      | `false` |
| `metrics.graphLabels`                   | **Type: boolean.** Add per-object hierarchy, resources, shape and local/remote workload-signal Prometheus series; increases cardinality, does not gate JSON or KEDA scalar routes | `false` |
| `metrics.authentication.enabled`        | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                      | `false` |
| `metrics.authentication.existingSecret` | **Type: string.** Existing or ESO-managed Secret containing the metrics token                                                                                                     | `polyad-metrics` |
| `metrics.authentication.secretKey`      | **Type: string.** Key containing the metrics bearer token                                                                                                                         | `token` |
| `metrics.authentication.key`            | **Type: string.** Inline metrics token; requires existingSecret to be empty                                                                                                       | `""` |

### Named operator API credentials

| Name                                      | Description                                                                                                                                                       | Value                   |
| ----------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| `authentication.mode`                     | **Type: string.** String. Required enforces endpoint credentials; Disabled opts into an unauthenticated demo without HTTP request quotas                          | `Required` |
| `authentication.backend`                  | **Type: string.** String. Builtin uses Polyad's Flask hooks; FlaskHTTPAuth loads the adapter included in official operator images                                 | `Builtin` |
| `authentication.storage.enabled`          | **Type: boolean.** Boolean. Persist one-way key verifiers and policy records in PostgreSQL and enforce database revocation; raw tokens stay in Kubernetes Secrets | `false` |
| `authentication.storage.separateDatabase` | **Type: boolean.** Boolean. Use an isolated authentication database; false reuses the state database and its role, requiring postgresql.enabled                   | `true` |
| `authentication.storage.managed`          | **Type: boolean.** Boolean. Provision a separate CloudNativePG Cluster; false uses the DSN from existingSecret                                                    | `true` |
| `authentication.storage.existingSecret`   | **Type: string.** String. Externally managed authentication database connection Secret; used with separateDatabase=true and managed=false                         | `""` |
| `authentication.storage.secretKey`        | **Type: string.** String. DSN key in the external authentication database Secret                                                                                  | `uri` |
| `authentication.storage.database`         | **Type: string.** String. Database name for a new managed authentication cluster                                                                                  | `polyad-authentication` |
| `authentication.storage.username`         | **Type: string.** String. Sole application login owning the managed authentication database; PostgreSQL administrators retain administrative access               | `polyad_authentication` |
| `authentication.storage.size`             | **Type: string.** String. Persistent storage per managed authentication database instance                                                                         | `1Gi` |
| `authentication.storage.storageClass`     | **Type: string.** String. Authentication database storage class; empty uses the cluster default                                                                   | `""` |
| `authentication.services`                 | **Type: array.** Service API keys with direction, Secret reference, endpoint scopes, outbound baseUrl and individual rate/concurrency limits                      | `[]` |
| `authentication.operators`                | **Type: array.** Peer operator API keys; Inbound, Outbound or Bidirectional, with HA-wide per-key lanes                                                           | `[]` |

### KEDA installation, observation and credentials

| Name                           | Description                                                                                                                                                                    | Value   |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------- |
| `keda.install`                 | **Type: boolean.** Install the pinned upstream KEDA chart in this release namespace; leave false to use an existing cluster installation                                       | `false` |
| `keda.observation.enabled`     | **Type: boolean.** Include KEDA workloads and Services in the reserved root Graph when root mode is enabled; disable if this installation does not use KEDA                    | `true` |
| `keda.observation.namespace`   | **Type: string.** Namespace of an existing KEDA installation; bundled KEDA always uses the release namespace                                                                   | `keda` |
| `keda.observation.deployments` | **Type: array.** Existing KEDA Deployment names to observe; remove disabled components or replace customized names; ignored for bundled KEDA                                   | `["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]` |
| `keda.observation.services`    | **Type: array.** Existing KEDA Service names to observe; match the installed KEDA chart; ignored for bundled KEDA                                                              | `["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]` |
| `keda.authentication.enabled`  | **Type: boolean.** Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics                                                     | `false` |
| `keda.authentication.name`     | **Type: string.** TriggerAuthentication name; empty uses the release metrics name                                                                                              | `""` |
| `kedaOperator`                 | **Type: object.** Upstream KEDA chart overrides used only when keda.install is true; its rendered workloads and Services are automatically included in the reserved root Graph | `{}` |

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

| Name                                                              | Description                                                                                                                      | Value             |
| ----------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ----------------- |
| `mesh.enabled`                                                    | **Type: boolean.** Allow graph HTTP and service-identity authorization and generate Istio security resources                     | `false` |
| `mesh.install`                                                    | **Type: boolean.** Install the pinned upstream Istio base and istiod dependencies; requires mesh.enabled                         | `false` |
| `mesh.multicluster.enabled`                                       | **Type: boolean.** Enable the existing or bundled sidecar mesh's multicluster configuration                                      | `false` |
| `mesh.multicluster.eastWest.enabled`                              | **Type: boolean.** Install a dedicated east-west gateway for separate networks                                                   | `false` |
| `mesh.multicluster.eastWest.portName`                             | **Type: string.** Istio Gateway listener name; protocol TLS and AUTO_PASSTHROUGH mode are required by this integration           | `tls` |
| `mesh.multicluster.eastWest.hosts`                                | **Type: array.** Service SNI suffixes exposed through AUTO_PASSTHROUGH                                                           | `["*.local"]` |
| `mesh.multicluster.peers`                                         | **Type: array.** Remote transport registrations; see docs/deployment/multicluster.md for same-network and gateway configurations | `[]` |
| `mesh.operator.enabled`                                           | **Type: boolean.** Inject the operator pods and authorize their API and event ports with Istio                                   | `false` |
| `mesh.operator.compositionPrincipals`                             | **Type: array.** Exact mTLS source identities allowed to use the composition endpoint                                            | `[]` |
| `mesh.operator.metricsPrincipals`                                 | **Type: array.** Exact mTLS source identities allowed to read scheduler metrics                                                  | `[]` |
| `mesh.operator.eventPrincipals`                                   | **Type: array.** Exact mTLS source identities allowed to subscribe to events                                                     | `[]` |
| `mesh.operator.connectionPrincipals`                              | **Type: array.** Exact mTLS source identities allowed to request temporary connections                                           | `[]` |
| `mesh.ingress.enabled`                                            | **Type: boolean.** Install the optional upstream Istio gateway dependency                                                        | `false` |
| `mesh.ingress.hosts`                                              | **Type: array.** Hosts served by the Istio Gateway and VirtualService                                                            | `[]` |
| `mesh.ingress.tlsSecret`                                          | **Type: string.** TLS credential Secret in the gateway namespace, required when exposing the APIs                                | `""` |
| `istioBase`                                                       | **Type: object.** Upstream Istio base chart overrides                                                                            | `{}` |
| `istiod.env.ENABLE_NATIVE_SIDECARS`                               | **Type: string.** Settings for istiod.env.ENABLE_NATIVE_SIDECARS.                                                                | `true` |
| `istiod.meshConfig.enableAutoMtls`                                | **Type: boolean.** Settings for istiod.meshConfig.enableAutoMtls.                                                                | `true` |
| `istiod.meshConfig.defaultConfig.holdApplicationUntilProxyStarts` | **Type: boolean.** Settings for istiod.meshConfig.defaultConfig.holdApplicationUntilProxyStarts.                                 | `true` |
| `istioIngress.labels.istio`                                       | **Type: string.** Settings for istioIngress.labels.istio.                                                                        | `polyad-ingress` |
| `istioEastWest.name`                                              | **Type: string.** Settings for istioEastWest.name.                                                                               | `polyad-eastwest` |
| `istioEastWest.labels.istio`                                      | **Type: string.** Settings for istioEastWest.labels.istio.                                                                       | `polyad-eastwest` |
| `istioEastWest.networkGateway`                                    | **Type: string or null.** Null retains the pinned upstream Istio profile default.                                                | `""` |
| `istioEastWest.networkGatewayPorts.status-port.port`              | **Type: integer.** Settings for istioEastWest.networkGatewayPorts.status-port.port.                                              | `15021` |
| `istioEastWest.networkGatewayPorts.status-port.targetPort`        | **Type: integer.** Settings for istioEastWest.networkGatewayPorts.status-port.targetPort.                                        | `15021` |
| `istioEastWest.networkGatewayPorts.tls.port`                      | **Type: integer.** Settings for istioEastWest.networkGatewayPorts.tls.port.                                                      | `15443` |
| `istioEastWest.networkGatewayPorts.tls.targetPort`                | **Type: integer.** Settings for istioEastWest.networkGatewayPorts.tls.targetPort.                                                | `15443` |

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
| `observer.resources.requests.cpu`    | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `100m` |
| `observer.resources.requests.memory` | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `128Mi` |
| `observer.resources.limits.cpu`      | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `1` |
| `observer.resources.limits.memory`   | **Type: string.** Non-negative Kubernetes resource quantity as a string; quote whole cores such as "1". Examples: 250m, 0.5, 128Mi, 1Gi. | `512Mi` |

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

<!-- The parameters table is maintained by the helm-readme-generator pre-commit hook. -->
