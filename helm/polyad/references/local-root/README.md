# local-root configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Local self-management with one operator and one cache, without federation.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/local-root/values.yaml
```

## Parameters

### Configuration

| Name                              | Description                                                                                                                                                                  | Value   |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `ha`                              | **Type: boolean.** Enable replicated operators; false runs one dense operator. Split components and root-managed remote workers require true.                                | `false` |
| `operator.replicaCount`           | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha                                                                                        | `1` |
| `operator.autoscaling.enabled`    | **Type: boolean.** Enable operator HPA using CPU and optional memory utilization                                                                                             | `false` |
| `rootControlPlane.enabled`        | **Type: boolean.** Maintain the reserved root PolyGraph and local service hierarchy; Federated mode also manages remote workers and requires ha                              | `true` |
| `rootControlPlane.mode`           | **Type: string.** Local observes this installation without remote credentials; Federated also manages registered clusters and remote workers                                 | `Local` |
| `global.multiCluster.clusterName` | **Type: string.** Unique, stable cluster identity used by Istio and Polyad                                                                                                   | `local` |
| `metrics.enabled`                 | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement | `true` |
| `dragonfly.ha.enabled`            | **Type: boolean.** Enable primary/replica replication and automatic failover                                                                                                 | `false` |
| `dragonfly.autoscaling.enabled`   | **Type: boolean.** Use KEDA for bundled HA Dragonfly and enable Polyad metrics; inactive outside bundled HA                                                                  | `false` |
| `dragonflyOperator.replicaCount`  | **Type: integer.** Leader-elected Dragonfly controller replicas                                                                                                              | `1` |
| `keda.observation.enabled`        | **Type: boolean.** Include KEDA workloads and Services in the reserved root Graph when root mode is enabled; disable if this installation does not use KEDA                  | `false` |
