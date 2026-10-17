# federation configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Cross-cluster PolyGraph placement from this release's cluster, named east.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/federation/values.yaml
```

## Parameters

### Configuration

| Name                              | Description                                                                                                                     | Value  |
| --------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `global.multiCluster.clusterName` | **Type: string.** Unique, stable cluster identity used by Istio and Polyad Stable, unique identity of the Helm release cluster. | `east` |
| `federation.enabled`              | **Type: boolean.** Allow PolyGraph nodes to deploy Graphs and nested PolyGraphs in registered clusters                          | `true` |
| `federation.clusters`             | **Type: array.** Cluster name, namespace and kubeconfigSecret registrations; credentials are mounted read-only                  | `[{"name": "west", "namespace": "workloads", "kubeconfigSecret": "west-access"}, {"name": "north", "namespace": "workloads", "kubeconfigSecret": "north-access"}]` |
