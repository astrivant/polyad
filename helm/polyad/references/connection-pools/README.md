# connection-pools configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Overlay for a local HA operator with KEDA installed (or add keda.install=true).

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/connection-pools/values.yaml
```

## Parameters

### Configuration

| Name                                                           | Description                                                                                                                                                        | Value  |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ |
| `ha`                                                           | **Type: boolean.** Keep at least two local operator replicas and permit autoscaling.                                                                               | `true` |
| `operator.autoscaling.enabled`                                 | **Type: boolean.** Scale the local dense/bootstrap Deployment; requires ha.                                                                                        | `true` |
| `operator.autoscaling.minReplicas`                             | **Type: integer.** Keep two operator replicas available.                                                                                                           | `2` |
| `operator.autoscaling.maxReplicas`                             | **Type: integer.** Bound aggregate pools along with each per-process maximum.                                                                                      | `8` |
| `operator.autoscaling.targetMemoryUtilizationPercentage`       | **Type: integer or null.** Target requested memory utilization; null omits memory.                                                                                 | `80` |
| `operator.autoscaling.connections.enabled`                     | **Type: boolean.** Enable KEDA pool pressure and automatically enable operator metrics; with architecture.autoscaling also extends component ReplicaGroup scalers. | `true` |
| `operator.autoscaling.connections.targetUtilizationPercentage` | **Type: integer.** Target the busiest active-plus-waiting pool fraction per process; 70 leaves headroom before pool exhaustion.                                    | `70` |
| `operator.autoscaling.connections.pollingInterval`             | **Type: integer.** KEDA polling seconds for the local Deployment; HPA has its own cluster sync cadence.                                                            | `15` |
| `operator.connections.cache.maxConnections`                    | **Type: integer.** Per-pool maximum; include all dense/split/remote processes in backend capacity planning.                                                        | `128` |
| `operator.connections.lanes.maxConnections`                    | **Type: integer.** Per-process lane pool ceiling; increasing replicas adds separate pools.                                                                         | `32` |
| `operator.connections.lanes.connectTimeoutSeconds`             | **Type: number.** Fail connection establishment after two seconds.                                                                                                 | `2` |
| `operator.connections.lanes.socketTimeoutSeconds`              | **Type: number.** Fail stalled Redis commands after two seconds; admission remains fail-closed.                                                                    | `2` |
| `operator.connections.rateLimits.maxConnections`               | **Type: integer.** Per-process quota-pool ceiling; idle retained sockets are excluded from scaling demand.                                                         | `32` |
| `operator.connections.state.maxConnections`                    | **Type: integer.** Per-process state connections; separate from server postgresql.maxConnections.                                                                  | `2` |
| `operator.connections.authentication.maxConnections`           | **Type: integer.** Per-process credential lookup connections; independently budget a separate database if configured.                                              | `2` |
