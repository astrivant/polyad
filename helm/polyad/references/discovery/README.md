# discovery configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Overlay on root-control-plane/values.yaml after registering workload clusters.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/discovery/values.yaml
```

## Parameters

### Configuration

| Name                                               | Description                                                                                                                                                                                                                                   | Value           |
| -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `operator.serviceAccess.discovery`                 | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Atlas` |
| `operator.serviceAccess.connections`               | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Atlas` |
| `operator.serviceAccess.clusters.west.parent`      | **Type: string.** Configured parent cluster; empty inherits directly from the root. Cycles and missing parents prevent startup.                                                                                                               | `""` |
| `operator.serviceAccess.clusters.west.discovery`   | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Atlas` |
| `operator.serviceAccess.clusters.west.connections` | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Atlas` |
| `operator.serviceAccess.clusters.east.parent`      | **Type: string.** Configured parent cluster; empty inherits directly from the root. Cycles and missing parents prevent startup.                                                                                                               | `west` |
| `operator.serviceAccess.clusters.east.discovery`   | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Cluster` |
| `operator.serviceAccess.clusters.east.connections` | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Atlas` |
| `operator.serviceAccess.trustDomains.west`         | **Type: string.** Istio trust domain matching that cluster actual mesh identity.                                                                                                                                                              | `cluster.local` |
| `operator.serviceAccess.trustDomains.east`         | **Type: string.** Istio trust domain matching that cluster actual mesh identity.                                                                                                                                                              | `cluster.local` |
| `events.enabled`                                   | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091                                                                                                                                             | `true` |
| `events.existingSecret`                            | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                                                                                                                                                    | `""` |
| `connections.enabled`                              | **Type: boolean.** Serve service-account authenticated temporary connections on port 8093                                                                                                                                                     | `true` |
| `authentication.services`                          | **Type: array.** Service API keys with direction, Secret reference, endpoint scopes, outbound baseUrl and individual rate/concurrency limits                                                                                                  | `[{"name": "west-discovery", "direction": "Inbound", "existingSecret": "west-discovery", "secretKey": "token", "requestsPerMinute": 120, "maxConcurrentRequests": 4, "endpoints": ["discovery", "events", "topology"], "home": {"name": "application-west", "namespace": "workloads", "cluster": "west", "kind": "Graph"}, "graphs": [{"name": "application", "namespace": "orchestration", "cluster": "management", "kind": "PolyGraph", "descendants": true}]}]` |
