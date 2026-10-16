# singular configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

One dense operator in the Helm release cluster and namespace.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/singular/values.yaml
```

## Parameters

### Configuration

| Name                                 | Description                                                                                                                                                                      | Value   |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------- |
| `ha`                                 | **Type: boolean.** Run at least two operator replicas and permit split components or remote workers; false runs one dense operator                                               | `false` |
| `operator.logLevel`                  | **Type: string.** INFO keeps normal operational logs; DEBUG traces Polyad reconciliation and API timings, including observers. Choices: DEBUG, INFO, WARNING, ERROR, CRITICAL.   | `INFO` |
| `operator.replicaCount`              | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha Minimum: 1. Maximum: 32.                                                                   | `1` |
| `operator.autoscaling.enabled`       | **Type: boolean.** Enable operator HPA using CPU and optional memory utilization Singular requires exactly one operator replica.                                                 | `false` |
| `operator.nodeSelector`              | **Type: object.** Node labels selecting the operator node group, independent of workload graph placement Operator placement; graph workloads have their own placement.           | `{}` |
| `operator.tolerations`               | **Type: array.** Taints tolerated by the operator replicas                                                                                                                       | `[]` |
| `operator.resources.requests.cpu`    | **Type: string.** Requested operator CPU, required for CPU autoscaling                                                                                                           | `1` |
| `operator.resources.requests.memory` | **Type: string.** Requested operator memory, required when the HPA memory metric is enabled                                                                                      | `1Gi` |
| `operator.resources.limits.cpu`      | **Type: string.** CPU limit; match the request to retain Guaranteed QoS                                                                                                          | `1` |
| `operator.resources.limits.memory`   | **Type: string.** Operator memory limit                                                                                                                                          | `1Gi` |
| `architecture.mode`                  | **Type: string.** Dense combines responsibilities; Distributed manages gateway, executor and telemetry ReplicaGroups inside a Graph; requires ha Choices: Dense, Distributed.    | `Dense` |
| `architecture.autoscaling`           | **Type: boolean.** Enable KEDA scaling of all component ReplicaGroup definitions                                                                                                 | `false` |
| `rootControlPlane.enabled`           | **Type: boolean.** Manage registered clusters and remote execution replicas through one root scheduler; requires ha Root-managed execution requires HA.                          | `false` |
| `rootControlPlane.pools`             | **Type: array.** Root-owned OperatorPools: unique name and registered cluster, replicas, and optional resources, nodeSelector and tolerations; requires rootControlPlane.enabled | `[]` |
| `dragonfly.enabled`                  | **Type: boolean.** Deploy Dragonfly through the upstream operator Helm dependency                                                                                                | `true` |
| `dragonfly.ha.enabled`               | **Type: boolean.** Enable primary/replica replication and automatic failover                                                                                                     | `false` |
| `dragonfly.persistence.enabled`      | **Type: boolean.** Persist bundled Dragonfly snapshots on a PVC                                                                                                                  | `true` |
| `dragonfly.persistence.size`         | **Type: string.** Snapshot volume capacity                                                                                                                                       | `1Gi` |
| `dragonfly.persistence.storageClass` | **Type: string.** Snapshot volume storage class; empty uses the cluster default Empty uses the cluster default.                                                                  | `""` |
