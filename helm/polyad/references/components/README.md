# components configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Split gateway, executor and telemetry components in the Helm release cluster.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/components/values.yaml
```

## Parameters

### Configuration

| Name                                                   | Description                                                                                                                                                                                                                                   | Value            |
| ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `ha`                                                   | **Type: boolean.** Run at least two operator replicas and permit split components or remote workers; false runs one dense operator                                                                                                            | `true` |
| `operator.serviceAccess.discovery`                     | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Cluster` |
| `operator.logLevel`                                    | **Type: string.** INFO keeps normal operational logs; DEBUG traces Polyad reconciliation and API timings, including observers. Choices: DEBUG, INFO, WARNING, ERROR, CRITICAL.                                                                | `INFO` |
| `operator.replicaCount`                                | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha Recovery/bootstrap replicas, separate from the component copies. Minimum: 1. Maximum: 32.                                                               | `2` |
| `architecture.mode`                                    | **Type: string.** Dense combines responsibilities; Distributed manages gateway, executor and telemetry ReplicaGroups inside a Graph; requires ha Choices: Dense, Distributed.                                                                 | `Distributed` |
| `architecture.autoscaling`                             | **Type: boolean.** Enable KEDA scaling of all component ReplicaGroup definitions KEDA requests counts; the operator admits them through GraphPolicies.                                                                                        | `true` |
| `architecture.cheegerMinimum`                          | **Type: number.** Minimum edge expansion of the three-component Graph; the default chain has h=1 Structural edge-expansion bound, not measured throughput. Minimum: 0. Maximum: 1.                                                            | `1` |
| `architecture.cheegerMaximum`                          | **Type: number or null.** Optional upper structural bound; null leaves it unbounded. Use 1 to permit the initial chain while excluding a fully connected triangle; values below 1 cannot admit this chart's topology.                         | `null` |
| `architecture.expandedNodes`                           | **Type: integer.** Maximum recursive vertices in the self-managed Graph Budget for recursive vertices in the component Graph. Minimum: 6. Maximum: 1000000.                                                                                   | `27` |
| `architecture.components.gateway.replicas`             | **Type: integer.** Initial gateway copies Minimum: 2. Maximum: 32.                                                                                                                                                                            | `2` |
| `architecture.components.gateway.minReplicas`          | **Type: integer.** Minimum gateway copies Minimum: 2. Maximum: 32.                                                                                                                                                                            | `2` |
| `architecture.components.gateway.maxReplicas`          | **Type: integer.** Maximum gateway copies Minimum: 2. Maximum: 32.                                                                                                                                                                            | `8` |
| `architecture.components.gateway.requestsPerSecond`    | **Type: number.** Target total HTTP requests per second per copy Demand per gateway copy.                                                                                                                                                     | `50` |
| `architecture.components.gateway.concurrentRequests`   | **Type: integer.** Target open HTTP requests and event streams per copy Includes open event streams. Minimum: 1.                                                                                                                              | `8` |
| `architecture.components.executor.replicas`            | **Type: integer.** Initial execution copies Minimum: 2. Maximum: 32.                                                                                                                                                                          | `2` |
| `architecture.components.executor.minReplicas`         | **Type: integer.** Minimum execution copies Minimum: 2. Maximum: 32.                                                                                                                                                                          | `2` |
| `architecture.components.executor.maxReplicas`         | **Type: integer.** Maximum execution copies Minimum: 2. Maximum: 32.                                                                                                                                                                          | `8` |
| `architecture.components.executor.backlog`             | **Type: integer.** Target outstanding graph hints per execution copy across all managed clusters Outstanding graph hints per copy across managed clusters. Minimum: 1.                                                                        | `8` |
| `architecture.components.telemetry.replicas`           | **Type: integer.** Initial metrics-serving copies Minimum: 2. Maximum: 32.                                                                                                                                                                    | `2` |
| `architecture.components.telemetry.minReplicas`        | **Type: integer.** Minimum metrics-serving copies Minimum: 2. Maximum: 32.                                                                                                                                                                    | `2` |
| `architecture.components.telemetry.maxReplicas`        | **Type: integer.** Maximum metrics-serving copies Minimum: 2. Maximum: 32.                                                                                                                                                                    | `8` |
| `architecture.components.telemetry.requestsPerSecond`  | **Type: number.** Target total metrics requests per second per copy                                                                                                                                                                           | `50` |
| `architecture.components.telemetry.concurrentRequests` | **Type: integer.** Target concurrent metrics requests per copy Minimum: 1.                                                                                                                                                                    | `2` |
| `api.enabled`                                          | **Type: boolean.** Serve authenticated composition requests on port 8090 through a ClusterIP Service                                                                                                                                          | `true` |
| `api.existingSecret`                                   | **Type: string.** Existing Secret with a token key authorizing namespace-scoped composition submissions and audit reads                                                                                                                       | `polyad-api` |
| `events.enabled`                                       | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091                                                                                                                                             | `true` |
| `events.existingSecret`                                | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                                                                                                                                                    | `polyad-events` |
| `metrics.enabled`                                      | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement                                                                  | `true` |
| `metrics.authentication.enabled`                       | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                                                                                  | `true` |
| `metrics.authentication.existingSecret`                | **Type: string.** Existing or ESO-managed Secret containing the metrics token                                                                                                                                                                 | `polyad-metrics` |
| `keda.authentication.enabled`                          | **Type: boolean.** Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics KEDA reads the operator metrics API using its bearer token.                                                        | `true` |
