# istio-traffic configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Istio traffic strategies for locality and long-lived event connections.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/istio-traffic/values.yaml
```

## Parameters

### Configuration

| Name                                             | Description                                                                                                       | Value              |
| ------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | ------------------ |
| `global.meshID`                                  | **Type: string.** Shared Istio mesh identity.                                                                     | `application-mesh` |
| `global.network`                                 | **Type: string.** Network containing this release.                                                                | `east-network` |
| `global.multiCluster.clusterName`                | **Type: string.** Unique cluster name within the mesh.                                                            | `east` |
| `operator.terminationGracePeriodSeconds`         | **Type: integer.** Allow the configured event drain plus normal application and proxy shutdown.                   | `90` |
| `mesh.enabled`                                   | **Type: boolean.** Generate Polyad's Istio resources.                                                             | `true` |
| `mesh.operator.enabled`                          | **Type: boolean.** Enable operator sidecars and mesh security.                                                    | `true` |
| `mesh.multicluster.enabled`                      | **Type: boolean.** Enable multicluster identity without requiring an east-west gateway for same-network clusters. | `true` |
| `mesh.multicluster.routing.enabled`              | **Type: boolean.** Add endpoint ejection and locality-aware load balancing.                                       | `true` |
| `mesh.multicluster.routing.mode`                 | **Type: string.** LocalFirst prefers local healthy endpoints; alternatives are Failover and Distributed.          | `LocalFirst` |
| `mesh.multicluster.routing.consecutive5xxErrors` | **Type: integer.** Consecutive server failures before ejecting an endpoint.                                       | `5` |
| `mesh.multicluster.routing.intervalSeconds`      | **Type: integer.** Seconds between endpoint health analyses.                                                      | `10` |
| `mesh.multicluster.routing.baseEjectionSeconds`  | **Type: integer.** Minimum seconds an unhealthy endpoint remains ejected.                                         | `30` |
| `mesh.multicluster.routing.maxEjectionPercent`   | **Type: integer.** Maximum percentage of endpoints that may be ejected.                                           | `50` |
| `events.enabled`                                 | **Type: boolean.** Serve SSE subscriptions; WebSocket remains independently optional.                             | `true` |
| `events.rebalance.enabled`                       | **Type: boolean.** Enable membership discovery and controlled reconnect instructions.                             | `true` |
| `events.rebalance.routing`                       | **Type: string.** Keep Service routing so Istio selects a healthy Pod for each new connection.                    | `Service` |
| `events.rebalance.drainSeconds`                  | **Type: number.** Stop admission and allow active connections to reconnect before termination.                    | `20` |
| `events.istio.enabled`                           | **Type: boolean.** Create the event Service DestinationRule.                                                      | `true` |
| `events.istio.loadBalancer`                      | **Type: string.** LEAST_REQUEST favors replicas with fewer active requests; ROUND_ROBIN is the alternative.       | `LEAST_REQUEST` |
| `events.istio.warmupSeconds`                     | **Type: number.** Gradually introduce newly ready replicas for new connections.                                   | `30` |
