# Polyad Helm chart

Deploy the Kubernetes workload scheduler and its shared Dragonfly queue. See the
[operator guide](../../docs/operator.md) for graph semantics, replica coordination,
health metrics, and installation examples.

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
are two Polyad replicas, two leader-elected Dragonfly controller replicas, and one
Dragonfly data instance with five-minute PVC snapshots and eviction disabled.

Enable HA with one primary and one replica:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --set dragonfly.ha.enabled=true
```

HA instances require distinct nodes by default. Set `dragonfly.ha.replicas=3` for
one primary and two replicas, or change `dragonfly.ha.topologyKey` to
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

**Migration from the former bundled StatefulSet:** the managed cache uses a new
`<release>-queue` name and fresh volumes. Existing queue snapshots are not
imported. Kubernetes remains authoritative and rescans repopulate notifications;
expect a reconciliation pause while the new cache starts. Review and remove the
old `data-<release>-dragonfly-0` PVC separately when it is no longer needed.

See the [networking guide](../../docs/networking.md) for scoped graph isolation,
optional Istio installation and endpoint authorization, event subscribers, and
Secret-driven health replacement. Both networking integrations are disabled by default.

Operator deployment settings are grouped under `operator`. When upgrading existing
values files, nest replicas, placement, autoscaling, image, resources and shutdown
grace settings under that key (for example, `image.tag` becomes `operator.image.tag`).

## Parameters

### Operator and shared queue parameters

| Name                                                  | Description                                                                                          | Value                                                 |
| ----------------------------------------------------- | ---------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| `operator.logLevel`                                   | Polyad logging verbosity (DEBUG, INFO, WARNING, ERROR or CRITICAL)                                   | `INFO`                                                |
| `operator.replicaCount`                               | Operator replicas when autoscaling is disabled                                                       | `2`                                                   |
| `operator.nodeSelector`                               | Node labels selecting the operator node group, independent of workload graph placement               | `{}`                                                  |
| `operator.tolerations`                                | Taints tolerated by the operator replicas                                                            | `[]`                                                  |
| `operator.autoscaling.enabled`                        | Enable CPU-based operator autoscaling                                                                | `false`                                               |
| `operator.autoscaling.minReplicas`                    | Minimum operator replicas                                                                            | `2`                                                   |
| `operator.autoscaling.maxReplicas`                    | Maximum operator replicas, at least minReplicas and at most 32                                       | `8`                                                   |
| `operator.autoscaling.targetCPUUtilizationPercentage` | Target operator CPU utilization relative to requested CPU                                            | `70`                                                  |
| `operator.image.repository`                           | Operator image repository                                                                            | `ghcr.io/astrivant/polyad`                            |
| `operator.image.tag`                                  | Operator image tag                                                                                   | `0.1.0`                                               |
| `operator.image.pullPolicy`                           | Operator image pull policy                                                                           | `IfNotPresent`                                        |
| `operator.resources.requests.cpu`                     | Requested operator CPU, required for CPU autoscaling                                                 | `100m`                                                |
| `operator.resources.requests.memory`                  | Requested operator memory                                                                            | `128Mi`                                               |
| `operator.resources.limits.memory`                    | Operator memory limit                                                                                | `512Mi`                                               |
| `operator.terminationGracePeriodSeconds`              | Time allowed for operator shutdown and outstanding API calls                                         | `60`                                                  |
| `dragonfly.enabled`                                   | Deploy Dragonfly through the upstream operator Helm dependency                                       | `true`                                                |
| `dragonfly.image`                                     | Bundled Dragonfly image                                                                              | `docker.dragonflydb.io/dragonflydb/dragonfly:v1.39.0` |
| `dragonfly.ha.enabled`                                | Enable primary/replica replication and automatic failover                                            | `false`                                               |
| `dragonfly.ha.replicas`                               | Total Dragonfly instances in HA mode, including the primary                                          | `2`                                                   |
| `dragonfly.ha.topologyKey`                            | Place HA instances on distinct values of this node label                                             | `kubernetes.io/hostname`                              |
| `dragonfly.externalUrl`                               | External Redis-compatible URL when bundled Dragonfly is disabled                                     | `redis://dragonfly:6379/0`                            |
| `dragonfly.existingSecret`                            | Existing Secret containing a url key for Dragonfly, taking precedence over other connection settings | `""`                                                  |
| `dragonfly.persistence.enabled`                       | Persist bundled Dragonfly snapshots on a PVC                                                         | `true`                                                |
| `dragonfly.persistence.size`                          | Snapshot volume capacity                                                                             | `1Gi`                                                 |
| `dragonfly.persistence.storageClass`                  | Snapshot volume storage class; empty uses the cluster default                                        | `""`                                                  |

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

### Event subscriptions

| Name                    | Description                                                                               | Value           |
| ----------------------- | ----------------------------------------------------------------------------------------- | --------------- |
| `events.enabled`        | Serve namespace graph observations through a separate SSE Service on port 8091            | `false`         |
| `events.existingSecret` | Existing Secret containing a token key for read-only event subscriptions                  | `polyad-events` |
| `events.key`            | Inline subscriber token; creates a managed Secret and requires existingSecret to be empty | `""`            |
| `events.retention`      | Maximum observations retained in the shared replay stream                                 | `10000`         |
| `events.maxConnections` | Maximum simultaneous event subscribers per operator replica                               | `16`            |

### Scheduler metrics

| Name                  | Description                                                                               | Value   |
| --------------------- | ----------------------------------------------------------------------------------------- | ------- |
| `metrics.enabled`     | Serve cached Prometheus and JSON metrics on an internal Service at port 8092              | `false` |
| `metrics.graphLabels` | Include per-object hierarchy and direct-resource Prometheus series; increases cardinality | `false` |

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

| Name                                  | Description                                                                               | Value   |
| ------------------------------------- | ----------------------------------------------------------------------------------------- | ------- |
| `mesh.enabled`                        | Allow graph HTTP and service-identity authorization and generate Istio security resources | `false` |
| `mesh.install`                        | Install the pinned upstream Istio base and istiod dependencies; requires mesh.enabled     | `false` |
| `mesh.operator.enabled`               | Inject the operator pods and authorize their API and event ports with Istio               | `false` |
| `mesh.operator.compositionPrincipals` | Exact mTLS source identities allowed to use the composition endpoint                      | `[]`    |
| `mesh.operator.metricsPrincipals`     | Exact mTLS source identities allowed to read scheduler metrics                            | `[]`    |
| `mesh.operator.eventPrincipals`       | Exact mTLS source identities allowed to subscribe to events                               | `[]`    |
| `mesh.ingress.enabled`                | Install the optional upstream Istio gateway dependency                                    | `false` |
| `mesh.ingress.hosts`                  | Hosts served by the Istio Gateway and VirtualService                                      | `[]`    |
| `mesh.ingress.tlsSecret`              | TLS credential Secret in the gateway namespace, required when exposing the APIs           | `""`    |
| `istioBase`                           | Upstream Istio base chart overrides                                                       | `{}`    |
| `istiod`                              | Upstream Istio control-plane chart overrides                                              | `{}`    |
| `istioIngress`                        | Upstream Istio gateway chart overrides                                                    | `{}`    |

### Shared Istio namespace

| Name                    | Description                                                                                  | Value          |
| ----------------------- | -------------------------------------------------------------------------------------------- | -------------- |
| `global.istioNamespace` | Istio control-plane namespace; must equal the release namespace when mesh.install is enabled | `istio-system` |

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

<!-- The parameters table is maintained by the helm-readme-generator pre-commit hook. -->
