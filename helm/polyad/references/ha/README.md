# ha configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Replicated dense operators in one Helm release cluster and namespace.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/ha/values.yaml
```

## Parameters

### Configuration

| Name                                                     | Description                                                                                                                                                                                                                         | Value                    |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| `ha`                                                     | **Type: boolean.** Run at least two operator replicas and permit split components or remote workers; false runs one dense operator                                                                                                  | `true` |
| `operator.logLevel`                                      | **Type: string.** INFO keeps normal operational logs; DEBUG traces Polyad reconciliation and API timings, including observers. Choices: DEBUG, INFO, WARNING, ERROR, CRITICAL.                                                      | `INFO` |
| `operator.replicaCount`                                  | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha At least two; replicas share queues and mutation leases. Minimum: 1. Maximum: 32.                                                             | `2` |
| `operator.nodeSelector`                                  | **Type: object.** Node labels selecting the operator node group, independent of workload graph placement                                                                                                                            | `{}` |
| `operator.tolerations`                                   | **Type: array.** Taints tolerated by the operator replicas                                                                                                                                                                          | `[]` |
| `operator.autoscaling.enabled`                           | **Type: boolean.** Enable operator HPA using CPU and optional memory utilization CPU/memory HPA; requires a working resource metrics API.                                                                                           | `false` |
| `operator.autoscaling.minReplicas`                       | **Type: integer.** Minimum operator replicas HA may not scale below two operator replicas. Minimum: 1. Maximum: 32.                                                                                                                 | `2` |
| `operator.autoscaling.maxReplicas`                       | **Type: integer.** Maximum operator replicas, at least minReplicas and at most 32 Minimum: 1. Maximum: 32.                                                                                                                          | `8` |
| `operator.autoscaling.targetCPUUtilizationPercentage`    | **Type: integer.** Target operator CPU utilization relative to requested CPU Minimum: 1. Maximum: 100.                                                                                                                              | `70` |
| `operator.autoscaling.targetMemoryUtilizationPercentage` | **Type: integer or null.** Optional target memory utilization relative to requested memory (1-100); null disables the memory metric Percent of requested memory; null disables. Minimum: 1. Maximum: 100.                           | `80` |
| `operator.resources.requests.cpu`                        | **Type: string.** Requested operator CPU, required for CPU autoscaling CPU requests are needed when enabling the CPU HPA.                                                                                                           | `1` |
| `operator.resources.requests.memory`                     | **Type: string.** Requested operator memory, required when the HPA memory metric is enabled Required for the memory metric; its target is not based on limits.                                                                      | `1Gi` |
| `operator.resources.limits.cpu`                          | **Type: string.** CPU limit; match the request to retain Guaranteed QoS                                                                                                                                                             | `1` |
| `operator.resources.limits.memory`                       | **Type: string.** Operator memory limit                                                                                                                                                                                             | `1Gi` |
| `architecture.mode`                                      | **Type: string.** Dense combines responsibilities; Distributed manages gateway, executor and telemetry ReplicaGroups inside a Graph; requires ha Add components/values.yaml last for split components. Choices: Dense, Distributed. | `Dense` |
| `architecture.autoscaling`                               | **Type: boolean.** Enable KEDA scaling of all component ReplicaGroup definitions                                                                                                                                                    | `false` |
| `dragonfly.enabled`                                      | **Type: boolean.** Deploy Dragonfly through the upstream operator Helm dependency                                                                                                                                                   | `true` |
| `dragonfly.ha.enabled`                                   | **Type: boolean.** Enable primary/replica replication and automatic failover                                                                                                                                                        | `true` |
| `dragonfly.ha.replicas`                                  | **Type: integer.** Initial Dragonfly instances in HA mode, including the primary; fixed when autoscaling is disabled Minimum: 2. Maximum: 9.                                                                                        | `2` |
| `dragonfly.ha.topologyKey`                               | **Type: string.** Place HA instances on distinct values of this node label                                                                                                                                                          | `kubernetes.io/hostname` |
| `dragonfly.autoscaling.enabled`                          | **Type: boolean.** Use KEDA for bundled HA Dragonfly and enable Polyad metrics; inactive outside bundled HA Set false for the fixed ha.replicas count without KEDA.                                                                 | `true` |
| `dragonfly.autoscaling.minReplicas`                      | **Type: integer.** Minimum total cache instances, including the primary; never below two Minimum: 2. Maximum: 9.                                                                                                                    | `2` |
| `dragonfly.autoscaling.maxReplicas`                      | **Type: integer.** Maximum total cache instances; requires sufficient eligible nodes and storage Minimum: 2. Maximum: 9.                                                                                                            | `5` |
| `dragonfly.autoscaling.connectionsPerReplica`            | **Type: integer.** Primary connected clients per desired cache instance; scales failover copies, not write capacity Failover copies; does not increase primary capacity. Minimum: 1.                                                | `50` |
| `dragonfly.persistence.enabled`                          | **Type: boolean.** Persist bundled Dragonfly snapshots on a PVC                                                                                                                                                                     | `true` |
| `dragonfly.persistence.size`                             | **Type: string.** Snapshot volume capacity                                                                                                                                                                                          | `1Gi` |
| `dragonfly.persistence.storageClass`                     | **Type: string.** Snapshot volume storage class; empty uses the cluster default                                                                                                                                                     | `""` |
