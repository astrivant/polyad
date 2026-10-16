# replicaGroups configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [ReplicaGroup field reference](#replicagroup-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/replicaGroups/values.yaml
```

## Parameters

### ReplicaGroup field reference

| Name                                                    | Description                                                                                                                                                         | Value         |
| ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `replicaGroups.example.metadata.name`                   | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                        | `example` |
| `replicaGroups.example.metadata.namespace`              | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                        | `example` |
| `replicaGroups.example.metadata.labels`                 | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                              | `{}` |
| `replicaGroups.example.metadata.annotations`            | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                              | `{}` |
| `replicaGroups.example.spec.template.kind`              | **Type: string.** Required. No CRD default; the value shown is illustrative. Choices: "Workload", "Daemon", "Resource", "Graph", "PolyGraph", "ReplicaGroup".       | `Workload` |
| `replicaGroups.example.spec.template.ref`               | **Type: string.** Required. No CRD default; the value shown is illustrative. Namespaced definition name.                                                            | `example` |
| `replicaGroups.example.spec.replicas`                   | **Type: integer or string.** Optional. Default: 1. Requested number of copies, exposed through the scale subresource.                                               | `1` |
| `replicaGroups.example.spec.minReplicas`                | **Type: integer or string.** Optional. Default: 0. Inclusive lower bound, including zero.                                                                           | `0` |
| `replicaGroups.example.spec.maxReplicas`                | **Type: integer or string.** Optional. Default: 32. Inclusive upper bound, at most 256 copies per group.                                                            | `32` |
| `replicaGroups.example.spec.templateOnly`               | **Type: boolean or string.** Optional. Default: false. Reusable group controlling all inheriting instances.                                                         | `false` |
| `replicaGroups.example.spec.replicaSource.name`         | **Type: string.** Required. No CRD default; the value shown is illustrative. Reusable ReplicaGroup name.                                                            | `example` |
| `replicaGroups.example.spec.replicaSource.uid`          | **Type: string.** Required. No CRD default; the value shown is illustrative. Incarnation fence for the reusable group.                                              | `example` |
| `replicaGroups.example.spec.inheritReplicas`            | **Type: boolean or string.** Optional. Default: true. Whether a generated instance follows its reusable group.                                                      | `true` |
| `replicaGroups.example.spec.suspend`                    | **Type: boolean or string.** Optional. Default: false. Drain execution until resumed.                                                                               | `false` |
| `replicaGroups.example.spec.placement`                  | **Type: object or string or null.** Optional. No CRD default; the value shown is illustrative.                                                                      | `{}` |
| `replicaGroups.example.spec.policies`                   | **Type: array or string.** Optional. No CRD default; the value shown is illustrative. Structural restrictions for every replicated graph.                           | `[]` |
| `replicaGroups.example.spec.network`                    | **Type: object or string or null.** Optional. No CRD default; the value shown is illustrative.                                                                      | `{}` |
| `replicaGroups.example.spec.capacity`                   | **Type: object or string or null.** Optional. No CRD default; the value shown is illustrative.                                                                      | `{}` |
| `replicaGroups.example.spec.shutdownPolicy`             | **Type: string or null.** Optional. No CRD default; the value shown is illustrative.                                                                                | `example` |
| `replicaGroups.example.spec.activation`                 | **Type: object or string or null.** Optional. No CRD default; the value shown is illustrative.                                                                      | `{}` |
| `replicaGroups.example.spec.connectivity.mode`          | **Type: string.** Optional. Default: "Independent". Choices: "Independent", "Chain", "Ring", "Star", "FullMesh", "Custom". Connection pattern over active ordinals. | `Independent` |
| `replicaGroups.example.spec.connectivity.bidirectional` | **Type: boolean or string.** Optional. Default: false. Add a reverse connection with the same ports for each edge.                                                  | `false` |
| `replicaGroups.example.spec.connectivity.ports`         | **Type: array or string.** Optional. No CRD default; the value shown is illustrative. Destination grants for built-in patterns; Custom uses each edge's ports.      | `[{"port": 1, "protocol": "TCP"}]` |
| `replicaGroups.example.spec.connectivity.edges`         | **Type: array or string.** Optional. No CRD default; the value shown is illustrative. Custom edges, active only when both endpoints exist.                          | `[{"source": "example", "target": "example", "ports": [{"port": 1, "protocol": "TCP"}]}]` |
| `replicaGroups.example.spec.remoteScaling.root`         | **Type: string.** Required. No CRD default; the value shown is illustrative. Registered root cluster identity.                                                      | `example` |
| `replicaGroups.example.spec.remoteScaling.namespace`    | **Type: string.** Required. No CRD default; the value shown is illustrative. Namespace of the approved RemoteScale.                                                 | `example` |
| `replicaGroups.example.spec.remoteScaling.name`         | **Type: string.** Required. No CRD default; the value shown is illustrative. Name of the approved RemoteScale.                                                      | `example` |
| `replicaGroups.example.spec.remoteScaling.uid`          | **Type: string.** Required. No CRD default; the value shown is illustrative. Exact request incarnation; recreating it requires local approval.                      | `example` |
