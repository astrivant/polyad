# root-control-plane configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

One HA root manages execution replicas in registered workload clusters.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/root-control-plane/values.yaml
```

## Parameters

### Configuration

| Name                                    | Description                                                                                                                                                                                                                                   | Value                                    |
| --------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `ha`                                    | **Type: boolean.** Run at least two operator replicas and permit split components or remote workers; false runs one dense operator                                                                                                            | `true` |
| `operator.serviceAccess.discovery`      | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Atlas` |
| `operator.logLevel`                     | **Type: string.** INFO keeps normal operational logs; DEBUG traces Polyad reconciliation and API timings, including observers. Choices: DEBUG, INFO, WARNING, ERROR, CRITICAL.                                                                | `INFO` |
| `operator.replicaCount`                 | **Type: integer or null.** Operator replicas; null selects 1 for singular or 2 for ha Minimum: 1. Maximum: 32.                                                                                                                                | `2` |
| `global.multiCluster.clusterName`       | **Type: string.** Unique, stable cluster identity used by Istio and Polyad                                                                                                                                                                    | `management` |
| `federation.enabled`                    | **Type: boolean.** Allow PolyGraph nodes to deploy Graphs and nested PolyGraphs in registered clusters                                                                                                                                        | `true` |
| `federation.clusters`                   | **Type: array.** Cluster name, namespace and kubeconfigSecret registrations; credentials are mounted read-only                                                                                                                                | `[{"name": "west", "namespace": "workloads", "kubeconfigSecret": "west-access"}]` |
| `rootControlPlane.enabled`              | **Type: boolean.** Manage registered clusters and remote execution replicas through one root scheduler; requires ha                                                                                                                           | `true` |
| `rootControlPlane.kubeconfigSecret`     | **Type: string.** Existing root-namespace Secret with embedded, verified root kubeconfig under config, reachable from worker clusters                                                                                                         | `root-access` |
| `rootControlPlane.pools`                | **Type: array.** Root-owned OperatorPools: unique name and registered cluster, replicas and optional Pod overrides. To attach a Helm worker, replace Pod overrides with existingDeployment and scalingAuthority (Root or Local).              | `[{"name": "west-workers", "cluster": "west", "controller": "Deployment", "replicas": 2, "nodeSelector": {}, "tolerations": [], "resources": {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}}]` |
| `rootControlPlane.meshPeers`            | **Type: array.** Complete workload-cluster mesh peer registry; the controller excludes its current execution cluster                                                                                                                          | `[]` |
| `rootControlPlane.endpoints.api`        | **Type: string.** Externally reachable root composition API URL advertised to workloads                                                                                                                                                       | `https://polyad.example.com/composition` |
| `rootControlPlane.endpoints.events`     | **Type: string.** Externally reachable root events URL advertised to workloads                                                                                                                                                                | `https://polyad.example.com/events` |
| `rootControlPlane.endpoints.metrics`    | **Type: string.** Externally reachable root metrics URL advertised to workloads                                                                                                                                                               | `https://polyad.example.com/metrics` |
| `dragonfly.enabled`                     | **Type: boolean.** Deploy Dragonfly through the upstream operator Helm dependency                                                                                                                                                             | `false` |
| `dragonfly.existingSecret`              | **Type: string.** Existing Secret containing a url key for Dragonfly, taking precedence over other connection settings                                                                                                                        | `root-cache` |
| `api.enabled`                           | **Type: boolean.** Serve authenticated composition requests on port 8090 through a ClusterIP Service                                                                                                                                          | `true` |
| `api.existingSecret`                    | **Type: string.** Existing Secret with a token key authorizing namespace-scoped composition submissions and audit reads                                                                                                                       | `polyad-api` |
| `events.enabled`                        | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091                                                                                                                                             | `true` |
| `events.existingSecret`                 | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                                                                                                                                                    | `polyad-events` |
| `metrics.enabled`                       | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement                                                                  | `true` |
| `metrics.graphLabels`                   | **Type: boolean.** Expose per-graph topology, policy, Cheeger, spectrum, throughput and workload-signal series; increases cardinality, does not gate JSON or KEDA scalar routes                                                               | `true` |
| `metrics.authentication.enabled`        | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                                                                                  | `true` |
| `metrics.authentication.existingSecret` | **Type: string.** Existing or ESO-managed Secret containing the metrics token                                                                                                                                                                 | `polyad-metrics` |
