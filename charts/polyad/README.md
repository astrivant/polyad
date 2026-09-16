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
  - [KEDA credential integration](#keda-credential-integration)
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

Install KEDA, then enable HA with an initial primary and one replica:

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

| Reference file | Configuration and placement | Guide |
| --- | --- | --- |
| [`values-singular.reference.yaml`](values-singular.reference.yaml) | One dense operator and a persistent cache in the release cluster | [Singular](../../docs/deployment/deployment-profiles.md#one-dense-operator) |
| [`values-ha.reference.yaml`](values-ha.reference.yaml) | Replicated dense operators; also opts into cache HA | [HA](../../docs/deployment/deployment-profiles.md#ha-in-one-cluster) |
| [`values-components.reference.yaml`](values-components.reference.yaml) | HA bootstrap plus a self-managed gateway/executor/telemetry Graph and KEDA scaling in the release cluster | [Components](../../docs/deployment/components.md) |
| [`values-federation.reference.yaml`](values-federation.reference.yaml) | Remote cluster registrations for PolyGraph placement; destinations have independent execution operators | [Federation](../../docs/deployment/multicluster.md#placement-and-ownership) |
| [`values-root-control-plane.reference.yaml`](values-root-control-plane.reference.yaml) | HA management release that installs and controls remote execution pools | [Root control plane](../../docs/deployment/root-control-plane.md) |
| [`values-multicluster.reference.yaml`](values-multicluster.reference.yaml) | Istio transport, peer gateways and local network identity; adapt separately per cluster | [Multicluster networking](../../docs/deployment/multicluster.md#istio-across-different-networks) |
| [`values-observer.reference.yaml`](values-observer.reference.yaml) | Read-only observers alongside this release's operator | [Observers](../../docs/deployment/multicluster.md#optional-shared-observers) |
| [`values-postgresql.reference.yaml`](values-postgresql.reference.yaml) | Optional persistent state, database HA and connection-driven KEDA scaling in the release cluster | [PostgreSQL](../../docs/deployment/postgresql.md) |
| [`values-authentication.reference.yaml`](values-authentication.reference.yaml) | Scoped service/operator keys, workload Secret assignments and optional dedicated authentication storage | [API keys](../../docs/operations/api-keys.md) |
| [`values-demo.reference.yaml`](values-demo.reference.yaml) | Public demonstration endpoints without authentication or HTTP quotas | [Demo mode](../../docs/operations/api-keys.md#demonstrations-without-authentication) |
| [`values-tracing.reference.yaml`](values-tracing.reference.yaml) | OTLP/HTTP traces and independent decision logs, parent-based trace sampling and optional exporter credentials | [OpenTelemetry traces and logs](../../docs/operations/tracing.md) |
| [`values-tuning.reference.yaml`](values-tuning.reference.yaml) | Runtime polling intervals, exposed metrics, cardinality and authenticated scraping | [Performance tuning](../../docs/operations/performance.md) |

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

Choose the `singular` or `ha` deployment tag. With neither selected, HA is the
default. `operator.replicaCount: null` resolves to one or two respectively.
See [deployment profiles](../../docs/deployment/deployment-profiles.md) for install commands,
replica floors and the management/workload cluster placement table.

Templates are grouped by the deployment architecture they support:

| Directory | Purpose | Enabled by |
| --- | --- | --- |
| [`templates/singular/`](templates/singular) | One combined operator replica | `ha: false` (default) |
| [`templates/ha/`](templates/ha) | Replicated dense operator and optional root-owned execution pool declarations | `ha: true` |
| [`templates/ha/distributed/`](templates/ha/distributed) | Bootstrap Deployment and the gateway, executor and telemetry Graph, including component scaling | HA with `architecture.mode: Distributed` |
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

| Name | Description                                                                                                              | Value   |
| ---- | ------------------------------------------------------------------------------------------------------------------------ | ------- |
| `ha` | Boolean. Run at least two operator replicas and permit split components or remote workers; false runs one dense operator | `false` |

### Helm-installed downstream workers

| Name                      | Description                                                                                                                     | Value   |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `worker.enabled`          | Install an execution replica connected to an existing root; disables root election and requires external root/cache credentials | `false` |
| `worker.rootNamespace`    | Namespace of the root's queues, leases and OperatorPool; independent of this Helm release namespace                             | `""`    |
| `worker.rootClusterName`  | Root federation identity; global.multiCluster.clusterName identifies this worker's hosting cluster instead                      | `""`    |
| `worker.rootDeployment`   | Name of the existing root Deployment, used to identify the attachment authority                                                 | `""`    |
| `worker.rootGraph`        | Name of the root's reserved PolyGraph; normally ROOT_RELEASE-operators                                                          | `""`    |
| `worker.poolName`         | Name of the root-namespace OperatorPool that may attach this Deployment                                                         | `""`    |
| `worker.scalingAuthority` | Root lets the matching OperatorPool/KEDA control replicas; Local keeps replicas and optional HPA under this Helm release        | `Root`  |

### OpenTelemetry traces and logs

| Name                         | Description                                                                                                                         | Value                                  |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `tracing.enabled`            | Export operator and observer traces over OTLP/HTTP; disabled creates no exporter                                                    | `false`                                |
| `tracing.endpoint`           | Full HTTP/protobuf trace URL, including /v1/traces; use a Collector reachable from every execution cluster                          | `http://otel-collector:4318/v1/traces` |
| `tracing.serviceName`        | Service identity in the trace backend; resource attributes can identify cluster and environment                                     | `polyad-operator`                      |
| `tracing.samplingRatio`      | Fraction of new root traces to sample (number, 0-1); child spans honor their parent's sampling decision                             | `1`                                    |
| `tracing.timeoutSeconds`     | Export request timeout in seconds; exports are batched off the reconciliation path                                                  | `10`                                   |
| `tracing.resourceAttributes` | Comma-separated OpenTelemetry resource attributes, for example deployment.environment.name=production                               | `""`                                   |
| `tracing.headersSecret`      | Existing Secret with a headers key containing OTLP exporter headers; empty for collectors without authentication                    | `""`                                   |
| `tracing.logs.enabled`       | Export structured operator logs independently of trace enablement and sampling; console logging remains available                   | `false`                                |
| `tracing.logs.endpoint`      | Full HTTP/protobuf log URL, including /v1/logs; shares service identity, timeout, resource attributes and headersSecret with traces | `http://otel-collector:4318/v1/logs`   |

### Operator and shared queue parameters

| Name                                                                 | Description                                                                                                                                                                                                   | Value                      |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| `operator.logLevel`                                                  | Operator and observer Python logging verbosity; INFO for normal operation, DEBUG for reconciliation diagnostics (DEBUG, INFO, WARNING, ERROR or CRITICAL)                                                     | `INFO`                     |
| `operator.replicaCount`                                              | Operator replicas; null selects 1 for singular or 2 for ha                                                                                                                                                    | `nil`                      |
| `operator.nodeSelector`                                              | Node labels selecting the operator node group, independent of workload graph placement                                                                                                                        | `{}`                       |
| `operator.tolerations`                                               | Taints tolerated by the operator replicas                                                                                                                                                                     | `[]`                       |
| `operator.autoscaling.enabled`                                       | Enable operator HPA using CPU and optional memory utilization                                                                                                                                                 | `false`                    |
| `operator.autoscaling.minReplicas`                                   | Minimum operator replicas                                                                                                                                                                                     | `2`                        |
| `operator.autoscaling.maxReplicas`                                   | Maximum operator replicas, at least minReplicas and at most 32                                                                                                                                                | `8`                        |
| `operator.autoscaling.targetCPUUtilizationPercentage`                | Target operator CPU utilization relative to requested CPU                                                                                                                                                     | `70`                       |
| `operator.autoscaling.targetMemoryUtilizationPercentage`             | Optional target memory utilization relative to requested memory (1-100); null disables the memory metric                                                                                                      | `nil`                      |
| `operator.autoscaling.behavior.scaleUp.stabilizationWindowSeconds`   | Scale-up recommendation window in seconds (0-3600)                                                                                                                                                            | `0`                        |
| `operator.autoscaling.behavior.scaleUp.selectPolicy`                 | Choose the largest or smallest permitted change, or disable scale-up (Max, Min, Disabled)                                                                                                                     | `Max`                      |
| `operator.autoscaling.behavior.scaleUp.policies`                     | Rate limits (Pods or Percent); defaults to 100 percent or 4 Pods per 15 seconds; periodSeconds accepts 1-1800                                                                                                 | `[]`                       |
| `operator.autoscaling.behavior.scaleDown.stabilizationWindowSeconds` | Scale-down recommendation window in seconds (0-3600)                                                                                                                                                          | `300`                      |
| `operator.autoscaling.behavior.scaleDown.selectPolicy`               | Choose the largest or smallest permitted change, or disable scale-down (Max, Min, Disabled)                                                                                                                   | `Max`                      |
| `operator.autoscaling.behavior.scaleDown.policies`                   | Rate limits (Pods or Percent); defaults to 100 percent per 15 seconds; periodSeconds accepts 1-1800                                                                                                           | `[]`                       |
| `operator.tuning.rescanIntervalSeconds`                              | Pause after local and root-managed remote inventory scans (1-15s); watch polyad_inventory_sample_age_seconds and polyad_cluster_inventory_sample_fresh; lower values add Kubernetes and optional state writes | `5`                        |
| `operator.tuning.consumeIntervalSeconds`                             | Pause after local and remote queue consumption passes (0.1-5s); watch polyad_inbound_updates and polyad_kubernetes_writes_queued before increasing dispatch pressure                                          | `1`                        |
| `operator.tuning.metricsIntervalSeconds`                             | Pause after cached metrics publication and component/worker reporting (1-5s); also controls enabled PostgreSQL and Dragonfly connection sampling, not Prometheus scrape frequency or trace export             | `5`                        |
| `operator.tuning.backlogIntervalSeconds`                             | Pause after local shared-queue samples (1-5s); watch polyad_inbound_sample_age_seconds; root-managed remote backlog is sampled by rescanIntervalSeconds                                                       | `5`                        |
| `operator.image.repository`                                          | Operator image repository                                                                                                                                                                                     | `ghcr.io/astrivant/polyad` |
| `operator.image.tag`                                                 | Operator image tag                                                                                                                                                                                            | `0.0.1-alpha3`             |
| `operator.image.pullPolicy`                                          | Operator image pull policy                                                                                                                                                                                    | `IfNotPresent`             |
| `operator.resources.requests.cpu`                                    | Requested operator CPU, required for CPU autoscaling                                                                                                                                                          | `100m`                     |
| `operator.resources.requests.memory`                                 | Requested operator memory, required when the HPA memory metric is enabled                                                                                                                                     | `128Mi`                    |
| `operator.resources.limits.memory`                                   | Operator memory limit                                                                                                                                                                                         | `512Mi`                    |
| `operator.terminationGracePeriodSeconds`                             | Time allowed for operator shutdown and outstanding API calls                                                                                                                                                  | `60`                       |

### Optional PostgreSQL state storage

| Name                                            | Description                                                                                                                | Value                                                    |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `postgresql.enabled`                            | Persist graph state and tracked parameters in PostgreSQL                                                                   | `false`                                                  |
| `postgresql.managed`                            | Create a CloudNativePG Cluster; install its operator first                                                                 | `true`                                                   |
| `postgresql.existingSecret`                     | External database Secret containing the connection DSN when managed is false                                               | `""`                                                     |
| `postgresql.secretKey`                          | DSN key in the external database Secret                                                                                    | `uri`                                                    |
| `postgresql.database`                           | String. Application database for graph state and optional event history; set only when bootstrapping a new managed cluster | `polyad`                                                 |
| `postgresql.username`                           | String. Dedicated application owner; PUBLIC database access is revoked and superuser access stays disabled                 | `polyad`                                                 |
| `postgresql.events.enabled`                     | Boolean. Archive approved application events in PostgreSQL; bounded live replay still uses Dragonfly                       | `true`                                                   |
| `postgresql.events.retentionDays`               | Integer. Retain durable event history for this many days; graph snapshots retain current state independently               | `30`                                                     |
| `postgresql.scope`                              | State identity within the database; empty uses the release namespace and name                                              | `""`                                                     |
| `postgresql.image`                              | PostgreSQL image for the managed cluster                                                                                   | `ghcr.io/cloudnative-pg/postgresql:18.3-standard-trixie` |
| `postgresql.maxConnections`                     | Maximum connections per managed database instance, including administration                                                | `100`                                                    |
| `postgresql.storage.size`                       | Persistent storage per PostgreSQL instance                                                                                 | `10Gi`                                                   |
| `postgresql.storage.storageClass`               | Storage class; empty uses the cluster default                                                                              | `""`                                                     |
| `postgresql.ha.enabled`                         | Enable primary plus standby instances and synchronous replication                                                          | `false`                                                  |
| `postgresql.ha.instances`                       | Total instances when HA is enabled and autoscaling is disabled                                                             | `3`                                                      |
| `postgresql.ha.topologyKey`                     | Failure-domain label for required database pod anti-affinity                                                               | `kubernetes.io/hostname`                                 |
| `postgresql.resources`                          | Resource requests and limits for each PostgreSQL instance                                                                  | `{}`                                                     |
| `postgresql.autoscaling.enabled`                | Create a KEDA ScaledObject for the managed Cluster using operator connection counts                                        | `false`                                                  |
| `postgresql.autoscaling.minInstances`           | Minimum instances; at least 3 with HA, never zero                                                                          | `3`                                                      |
| `postgresql.autoscaling.maxInstances`           | Maximum database instances                                                                                                 | `6`                                                      |
| `postgresql.autoscaling.connectionsPerInstance` | Operator connections per desired database instance; does not add primary write capacity                                    | `20`                                                     |

### Optional HA component deployment architecture

| Name                                                   | Description                                                                                                                    | Value                                                 |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| `architecture.mode`                                    | Dense combines responsibilities; Distributed manages gateway, executor and telemetry ReplicaGroups inside a Graph; requires ha | `Dense`                                               |
| `architecture.cheegerMinimum`                          | Minimum edge expansion of the three-component Graph; the default chain has h=1                                                 | `1`                                                   |
| `architecture.expandedNodes`                           | Maximum recursive vertices in the self-managed Graph                                                                           | `27`                                                  |
| `architecture.autoscaling`                             | Enable KEDA scaling of all component ReplicaGroup definitions                                                                  | `false`                                               |
| `architecture.components.gateway.replicas`             | Initial gateway copies                                                                                                         | `2`                                                   |
| `architecture.components.gateway.minReplicas`          | Minimum gateway copies                                                                                                         | `2`                                                   |
| `architecture.components.gateway.maxReplicas`          | Maximum gateway copies                                                                                                         | `8`                                                   |
| `architecture.components.gateway.requestsPerSecond`    | Target total HTTP requests per second per copy                                                                                 | `50`                                                  |
| `architecture.components.gateway.concurrentRequests`   | Target open HTTP requests and event streams per copy                                                                           | `8`                                                   |
| `architecture.components.executor.replicas`            | Initial execution copies                                                                                                       | `2`                                                   |
| `architecture.components.executor.minReplicas`         | Minimum execution copies                                                                                                       | `2`                                                   |
| `architecture.components.executor.maxReplicas`         | Maximum execution copies                                                                                                       | `8`                                                   |
| `architecture.components.executor.backlog`             | Target outstanding graph hints per execution copy across all managed clusters                                                  | `8`                                                   |
| `architecture.components.telemetry.replicas`           | Initial metrics-serving copies                                                                                                 | `2`                                                   |
| `architecture.components.telemetry.minReplicas`        | Minimum metrics-serving copies                                                                                                 | `2`                                                   |
| `architecture.components.telemetry.maxReplicas`        | Maximum metrics-serving copies                                                                                                 | `8`                                                   |
| `architecture.components.telemetry.requestsPerSecond`  | Target total metrics requests per second per copy                                                                              | `50`                                                  |
| `architecture.components.telemetry.concurrentRequests` | Target concurrent metrics requests per copy                                                                                    | `2`                                                   |
| `dragonfly.enabled`                                    | Deploy Dragonfly through the upstream operator Helm dependency                                                                 | `true`                                                |
| `dragonfly.image`                                      | Bundled Dragonfly image                                                                                                        | `docker.dragonflydb.io/dragonflydb/dragonfly:v1.39.0` |
| `dragonfly.ha.enabled`                                 | Enable primary/replica replication and automatic failover                                                                      | `false`                                               |
| `dragonfly.ha.replicas`                                | Initial Dragonfly instances in HA mode, including the primary; fixed when autoscaling is disabled                              | `2`                                                   |
| `dragonfly.ha.topologyKey`                             | Place HA instances on distinct values of this node label                                                                       | `kubernetes.io/hostname`                              |
| `dragonfly.autoscaling.enabled`                        | Use KEDA for bundled HA Dragonfly and enable Polyad metrics; inactive outside bundled HA                                       | `true`                                                |
| `dragonfly.autoscaling.minReplicas`                    | Minimum total cache instances, including the primary; never below two                                                          | `2`                                                   |
| `dragonfly.autoscaling.maxReplicas`                    | Maximum total cache instances; requires sufficient eligible nodes and storage                                                  | `5`                                                   |
| `dragonfly.autoscaling.connectionsPerReplica`          | Primary connected clients per desired cache instance; scales failover copies, not write capacity                               | `50`                                                  |
| `dragonfly.externalUrl`                                | External Redis-compatible URL when bundled Dragonfly is disabled                                                               | `redis://dragonfly:6379/0`                            |
| `dragonfly.existingSecret`                             | Existing Secret containing a url key for Dragonfly, taking precedence over other connection settings                           | `""`                                                  |
| `dragonfly.persistence.enabled`                        | Persist bundled Dragonfly snapshots on a PVC                                                                                   | `true`                                                |
| `dragonfly.persistence.size`                           | Snapshot volume capacity                                                                                                       | `1Gi`                                                 |
| `dragonfly.persistence.storageClass`                   | Snapshot volume storage class; empty uses the cluster default                                                                  | `""`                                                  |

### Upstream Dragonfly operator dependency

| Name                                    | Description                                                                                     | Value                |
| --------------------------------------- | ----------------------------------------------------------------------------------------------- | -------------------- |
| `dragonflyOperator.nameOverride`        | Upstream controller resource name                                                               | `dragonfly-operator` |
| `dragonflyOperator.replicaCount`        | Leader-elected Dragonfly controller replicas                                                    | `2`                  |
| `dragonflyOperator.crds.install`        | Must remain false; Polyad installs the pinned upstream CRD from crds before rendering instances | `false`              |
| `dragonflyOperator.manager.extraArgs`   | Additional Dragonfly controller command arguments                                               | `[]`                 |
| `dragonflyOperator.rbacProxy.extraArgs` | Additional metrics proxy command arguments                                                      | `[]`                 |

### Composition API

| Name                              | Description                                                                                                                      | Value        |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| `api.enabled`                     | Serve authenticated composition requests on port 8090 through a ClusterIP Service                                                | `false`      |
| `api.existingSecret`              | Existing Secret with a token key authorizing namespace-scoped composition submissions and audit reads                            | `polyad-api` |
| `api.key`                         | Inline composition token; creates a managed Secret and requires existingSecret to be empty                                       | `""`         |
| `api.rateLimit.enabled`           | Enforce shared Redis/Dragonfly composition request quotas on every logical shard                                                 | `true`       |
| `api.rateLimit.requestsPerMinute` | Combined submission and audit requests per minute per shard, shared by all replicas                                              | `60`         |
| `api.gateway.enabled`             | Expose the composition Service through a Gateway API v1 HTTPRoute; requires api.enabled                                          | `false`      |
| `api.gateway.create`              | Create a Gateway in this release namespace instead of attaching to an existing Gateway                                           | `false`      |
| `api.gateway.name`                | Existing Gateway name when create is false; created Gateways use the release API name                                            | `""`         |
| `api.gateway.namespace`           | Existing Gateway namespace; empty uses the release namespace                                                                     | `""`         |
| `api.gateway.className`           | Installed GatewayClass used when create is true                                                                                  | `""`         |
| `api.gateway.sectionName`         | Listener name to attach to or create                                                                                             | `http`       |
| `api.gateway.hostnames`           | DNS hostnames matched by the HTTPRoute; empty matches all listener hostnames                                                     | `[]`         |
| `api.gateway.tlsSecret`           | Existing TLS certificate Secret in the release namespace for a created HTTPS listener on port 443; empty creates HTTP on port 80 | `""`         |

### Temporary connections

| Name                           | Description                                                                   | Value     |
| ------------------------------ | ----------------------------------------------------------------------------- | --------- |
| `connections.enabled`          | Serve service-account authenticated temporary connections on port 8093        | `false`   |
| `connections.scope`            | Allowed caller and target namespaces: Cluster, OperatorNamespace or Namespace | `Cluster` |
| `connections.namespace`        | Allowed namespace when scope is Namespace; otherwise empty                    | `""`      |
| `connections.maxTtlSeconds`    | Maximum connection lifetime from receipt creation, capped at 86400 seconds    | `3600`    |
| `connections.retentionSeconds` | Retain terminal receipts for retry identity and audit before cleanup          | `3600`    |

### Event subscriptions

| Name                    | Description                                                                               | Value           |
| ----------------------- | ----------------------------------------------------------------------------------------- | --------------- |
| `events.enabled`        | Serve namespace graph observations through a separate SSE Service on port 8091            | `false`         |
| `events.existingSecret` | Existing Secret containing a token key for read-only event subscriptions                  | `polyad-events` |
| `events.key`            | Inline subscriber token; creates a managed Secret and requires existingSecret to be empty | `""`            |
| `events.retention`      | Maximum observations retained in the shared replay stream                                 | `10000`         |
| `events.maxConnections` | Maximum simultaneous event subscribers per operator replica                               | `16`            |

### Scheduler metrics

| Name                                    | Description                                                                                                                                                    | Value            |
| --------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `metrics.enabled`                       | Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement      | `false`          |
| `metrics.graphLabels`                   | Add per-object hierarchy, resources, shape and local/remote workload-signal Prometheus series; increases cardinality, does not gate JSON or KEDA scalar routes | `false`          |
| `metrics.authentication.enabled`        | Require a dedicated bearer token on all metrics endpoints                                                                                                      | `false`          |
| `metrics.authentication.existingSecret` | Existing or ESO-managed Secret containing the metrics token                                                                                                    | `polyad-metrics` |
| `metrics.authentication.secretKey`      | Key containing the metrics bearer token                                                                                                                        | `token`          |
| `metrics.authentication.key`            | Inline metrics token; requires existingSecret to be empty                                                                                                      | `""`             |

### Named operator API credentials

| Name                                      | Description                                                                                                                                    | Value                   |
| ----------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------- |
| `authentication.mode`                     | String. Required enforces endpoint credentials; Disabled opts into an unauthenticated demo without HTTP request quotas                         | `Required`              |
| `authentication.backend`                  | String. Builtin uses Polyad's Flask hooks; FlaskHTTPAuth loads the adapter included in official operator images                                | `Builtin`               |
| `authentication.storage.enabled`          | Boolean. Persist one-way key verifiers and policy records in PostgreSQL and enforce database revocation; raw tokens stay in Kubernetes Secrets | `false`                 |
| `authentication.storage.separateDatabase` | Boolean. Use an isolated authentication database; false reuses the state database and its role, requiring postgresql.enabled                   | `true`                  |
| `authentication.storage.managed`          | Boolean. Provision a separate CloudNativePG Cluster; false uses the DSN from existingSecret                                                    | `true`                  |
| `authentication.storage.existingSecret`   | String. Externally managed authentication database connection Secret; used with separateDatabase=true and managed=false                        | `""`                    |
| `authentication.storage.secretKey`        | String. DSN key in the external authentication database Secret                                                                                 | `uri`                   |
| `authentication.storage.database`         | String. Database name for a new managed authentication cluster                                                                                 | `polyad-authentication` |
| `authentication.storage.username`         | String. Sole application login owning the managed authentication database; PostgreSQL administrators retain administrative access              | `polyad_authentication` |
| `authentication.storage.size`             | String. Persistent storage per managed authentication database instance                                                                        | `1Gi`                   |
| `authentication.storage.storageClass`     | String. Authentication database storage class; empty uses the cluster default                                                                  | `""`                    |
| `authentication.services`                 | Service API keys with direction, Secret reference, endpoint scopes, outbound baseUrl and individual rate/concurrency limits                    | `[]`                    |
| `authentication.operators`                | Peer operator API keys; Inbound, Outbound or Bidirectional, with HA-wide per-key lanes                                                         | `[]`                    |

### KEDA credential integration

| Name                          | Description                                                                                             | Value   |
| ----------------------------- | ------------------------------------------------------------------------------------------------------- | ------- |
| `keda.authentication.enabled` | Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics | `false` |
| `keda.authentication.name`    | TriggerAuthentication name; empty uses the release metrics name                                         | `""`    |

### Optional External Secrets Operator resources

| Name                                  | Description                                                                                                                                                                                       | Value         |
| ------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `externalSecrets.enabled`             | Generate ExternalSecret resources; requires an installed ESO operator and SecretStore                                                                                                             | `false`       |
| `externalSecrets.reloadOnChange`      | Opt into Stakater Reloader annotations for ESO Secrets, operator and observer Deployments, and Daemons with spec.reloadOnSecretChange; requires externalSecrets.enabled and an installed Reloader | `false`       |
| `externalSecrets.refreshInterval`     | ESO credential refresh interval                                                                                                                                                                   | `1h`          |
| `externalSecrets.secretStoreRef.name` | Existing SecretStore or ClusterSecretStore name                                                                                                                                                   | `""`          |
| `externalSecrets.secretStoreRef.kind` | Secret store reference kind                                                                                                                                                                       | `SecretStore` |
| `externalSecrets.secrets`             | Secret mappings with name and data entries of secretKey and remoteRef; reference names via existingSecret settings                                                                                | `[]`          |

### Operator endpoint and cache isolation

| Name                             | Description                                                                              | Value   |
| -------------------------------- | ---------------------------------------------------------------------------------------- | ------- |
| `networkPolicy.enabled`          | Isolate operator pods; requires explicit Kubernetes API and cache egress rules           | `false` |
| `networkPolicy.apiServerCIDRs`   | Kubernetes API endpoint CIDRs reachable through this cluster's CNI                       | `[]`    |
| `networkPolicy.apiServerPort`    | Kubernetes API endpoint port after this cluster's service translation                    | `443`   |
| `networkPolicy.compositionPeers` | NetworkPolicy peers allowed to connect to the composition API                            | `[]`    |
| `networkPolicy.eventPeers`       | NetworkPolicy peers allowed to subscribe to events                                       | `[]`    |
| `networkPolicy.metricsPeers`     | Monitoring peers allowed to scrape the metrics API on port 8092                          | `[]`    |
| `networkPolicy.healthPeers`      | Optional monitoring peers allowed to read port 8080 health metrics                       | `[]`    |
| `networkPolicy.extraEgress`      | Additional NetworkPolicy egress rules, including any external cache or DNS configuration | `[]`    |

### Optional Istio integration

| Name                                  | Description                                                                                                                                                     | Value   |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `mesh.enabled`                        | Allow graph HTTP and service-identity authorization and generate Istio security resources                                                                       | `false` |
| `mesh.install`                        | Install the pinned upstream Istio base and istiod dependencies; requires mesh.enabled                                                                           | `false` |
| `mesh.multicluster.enabled`           | Enable the existing or bundled sidecar mesh's multicluster configuration                                                                                        | `false` |
| `mesh.multicluster.eastWest.enabled`  | Install a dedicated east-west gateway for separate networks                                                                                                     | `false` |
| `mesh.multicluster.eastWest.portName` | Istio Gateway listener name; protocol TLS and AUTO_PASSTHROUGH mode are required by this integration                                                            | `tls`   |
| `mesh.multicluster.eastWest.hosts`    | Service SNI suffixes exposed through AUTO_PASSTHROUGH                                                                                                           | `[]`    |
| `mesh.multicluster.peers`             | Remote transport registrations; see docs/deployment/multicluster.md for same-network and gateway configurations                                                 | `[]`    |
| `mesh.operator.enabled`               | Inject the operator pods and authorize their API and event ports with Istio                                                                                     | `false` |
| `mesh.operator.compositionPrincipals` | Exact mTLS source identities allowed to use the composition endpoint                                                                                            | `[]`    |
| `mesh.operator.metricsPrincipals`     | Exact mTLS source identities allowed to read scheduler metrics                                                                                                  | `[]`    |
| `mesh.operator.eventPrincipals`       | Exact mTLS source identities allowed to subscribe to events                                                                                                     | `[]`    |
| `mesh.operator.connectionPrincipals`  | Exact mTLS source identities allowed to request temporary connections                                                                                           | `[]`    |
| `mesh.ingress.enabled`                | Install the optional upstream Istio gateway dependency                                                                                                          | `false` |
| `mesh.ingress.hosts`                  | Hosts served by the Istio Gateway and VirtualService                                                                                                            | `[]`    |
| `mesh.ingress.tlsSecret`              | TLS credential Secret in the gateway namespace, required when exposing the APIs                                                                                 | `""`    |
| `istioBase`                           | Upstream Istio base chart overrides                                                                                                                             | `{}`    |
| `istiod`                              | Upstream Istio control-plane chart overrides                                                                                                                    | `{}`    |
| `istioIngress`                        | Upstream Istio gateway chart overrides                                                                                                                          | `{}`    |
| `istioEastWest`                       | Upstream east-west gateway overrides; networkGateway must equal global.network and custom tunnel ports require a matching networking.istio.io/gatewayPort label | `{}`    |

### Shared Istio namespace

| Name                              | Description                                                                                  | Value          |
| --------------------------------- | -------------------------------------------------------------------------------------------- | -------------- |
| `global.istioNamespace`           | Istio control-plane namespace; must equal the release namespace when mesh.install is enabled | `istio-system` |
| `global.meshID`                   | Shared mesh identity across participating clusters                                           | `""`           |
| `global.network`                  | Network containing this cluster; different networks use east-west gateways                   | `""`           |
| `global.multiCluster.clusterName` | Unique, stable cluster identity used by Istio and Polyad                                     | `""`           |

### Cross-cluster PolyGraph management

| Name                  | Description                                                                                   | Value   |
| --------------------- | --------------------------------------------------------------------------------------------- | ------- |
| `federation.enabled`  | Allow PolyGraph nodes to deploy Graphs and nested PolyGraphs in registered clusters           | `false` |
| `federation.clusters` | Cluster name, namespace and kubeconfigSecret registrations; credentials are mounted read-only | `[]`    |

### Optional shared graph observers

| Name                      | Description                                                                                      | Value   |
| ------------------------- | ------------------------------------------------------------------------------------------------ | ------- |
| `observer.enabled`        | Deploy shared read-only observers independently of execution operators                           | `false` |
| `observer.replicaCount`   | Number of stateless read replicas in this cluster and namespace                                  | `1`     |
| `observer.existingSecret` | Existing Secret containing a dedicated read credential under token                               | `""`    |
| `observer.mesh`           | Enable native sidecar injection, strict mTLS and source-identity authorization for observer Pods | `false` |
| `observer.principals`     | Exact source principals allowed to read the observer when observer.mesh is enabled               | `[]`    |
| `observer.peers`          | NetworkPolicy peers allowed to reach observer TCP 8094 when networkPolicy.enabled is set         | `[]`    |
| `observer.resources`      | CPU and memory requests and limits for the read replicas                                         | `{}`    |

### Advance graph capacity

| Name                             | Description                                                                              | Value                                              |
| -------------------------------- | ---------------------------------------------------------------------------------------- | -------------------------------------------------- |
| `capacity.enabled`               | Allow graph capacity plans to create autoscaler requests and inert placeholder Pods      | `false`                                            |
| `capacity.provisioningClassName` | Autoscaler class used when a graph does not select one                                   | `best-effort-atomic-scale-up.autoscaling.x-k8s.io` |
| `capacity.maxPods`               | Maximum forecast Pods per graph boundary, also bounded by each graph's maxPods           | `128`                                              |
| `capacity.placeholderImage`      | Inert image used to expose advance scheduling demand                                     | `registry.k8s.io/pause:3.10`                       |
| `capacity.priorityClass.create`  | Install a release-scoped PriorityClass for placeholder Pods                              | `true`                                             |
| `capacity.priorityClass.name`    | Existing PriorityClass name, or an override for the generated name                       | `""`                                               |
| `capacity.priorityClass.value`   | Placeholder priority; must meet the autoscaler's cutoff and be below workload priorities | `-5`                                               |

### Root control plane

| Name                                 | Description                                                                                                                                         | Value   |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `rootControlPlane.enabled`           | Manage registered clusters and remote execution replicas through one root scheduler; requires ha                                                    | `false` |
| `rootControlPlane.pools`             | Root-owned OperatorPools; set existingDeployment to attach a Helm-installed worker and scalingAuthority to Root or Local instead of provisioning it | `[]`    |
| `rootControlPlane.kubeconfigSecret`  | Existing root-namespace Secret with embedded, verified root kubeconfig under config, reachable from worker clusters                                 | `""`    |
| `rootControlPlane.meshPeers`         | Complete workload-cluster mesh peer registry; the controller excludes its current execution cluster                                                 | `[]`    |
| `rootControlPlane.endpoints.api`     | Externally reachable root composition API URL advertised to workloads                                                                               | `""`    |
| `rootControlPlane.endpoints.events`  | Externally reachable root events URL advertised to workloads                                                                                        | `""`    |
| `rootControlPlane.endpoints.metrics` | Externally reachable root metrics URL advertised to workloads                                                                                       | `""`    |

<!-- The parameters table is maintained by the helm-readme-generator pre-commit hook. -->
