# observer configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Add read-only Graph observers alongside this release's execution operator.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/observer/values.yaml
```

## Parameters

### Configuration

| Name                                 | Description                                                                                                                                                                          | Value             |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------- |
| `operator.logLevel`                  | **Type: string.** INFO keeps normal operational logs; DEBUG traces Polyad reconciliation and API timings, including observers. Choices: DEBUG, INFO, WARNING, ERROR, CRITICAL.       | `INFO` |
| `global.multiCluster.clusterName`    | **Type: string.** Unique, stable cluster identity used by Istio and Polyad Set to this release's identity; management for the root example.                                          | `east` |
| `observer.enabled`                   | **Type: boolean.** Deploy shared read-only observers independently of execution operators                                                                                            | `true` |
| `observer.replicaCount`              | **Type: integer.** Number of stateless read replicas in this cluster and namespace Stateless observation-serving copies, independent of execution. Minimum: 1. Maximum: 32.          | `2` |
| `observer.existingSecret`            | **Type: string.** Existing Secret containing a dedicated read credential under token Release-namespace Secret with a token key.                                                      | `polyad-observer` |
| `observer.resources.requests.cpu`    | **Type: string.** Kubernetes CPU quantity; requests reserve scheduling capacity and limits cap usage.                                                                                | `1` |
| `observer.resources.requests.memory` | **Type: string.** Kubernetes memory quantity; requests reserve capacity and limits bound container memory.                                                                           | `1Gi` |
| `observer.resources.limits.cpu`      | **Type: string.** Kubernetes CPU quantity; requests reserve scheduling capacity and limits cap usage.                                                                                | `1` |
| `observer.resources.limits.memory`   | **Type: string.** Kubernetes memory quantity; requests reserve capacity and limits bound container memory.                                                                           | `1Gi` |
| `observer.mesh`                      | **Type: boolean.** Enable native sidecar injection, strict mTLS and source-identity authorization for observer Pods Set true only with mesh.enabled, then supply allowed principals. | `false` |
| `observer.principals`                | **Type: array.** Exact source principals allowed to read the observer when observer.mesh is enabled Exact mTLS identities; enforced when observer.mesh=true.                         | `[]` |
| `observer.peers`                     | **Type: array.** NetworkPolicy peers allowed to reach observer TCP 8094 when networkPolicy.enabled is set NetworkPolicy peers allowed on TCP 8094 when networkPolicy.enabled.        | `[]` |
