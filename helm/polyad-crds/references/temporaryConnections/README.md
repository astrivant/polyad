# temporaryConnections configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [TemporaryConnection field reference](#temporaryconnection-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/temporaryConnections/values.yaml
```

## Parameters

### TemporaryConnection field reference

| Name                                                   | Description                                                                                                                                            | Value     |
| ------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------ | --------- |
| `temporaryConnections.example.metadata.name`           | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                           | `example` |
| `temporaryConnections.example.metadata.namespace`      | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                           | `example` |
| `temporaryConnections.example.metadata.labels`         | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                 | `{}` |
| `temporaryConnections.example.metadata.annotations`    | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                 | `{}` |
| `temporaryConnections.example.spec.requestId`          | **Type: string.** Required. No CRD default; the value shown is illustrative. Client idempotency key retained with the receipt.                         | `example` |
| `temporaryConnections.example.spec.namespace`          | **Type: string.** Required. No CRD default; the value shown is illustrative. Namespace of the target graph and receipt.                                | `example` |
| `temporaryConnections.example.spec.kind`               | **Type: string.** Required. No CRD default; the value shown is illustrative. Choices: "Graph", "PolyGraph", "ReplicaGroup". Target boundary kind.      | `Graph` |
| `temporaryConnections.example.spec.graph`              | **Type: string.** Required. No CRD default; the value shown is illustrative. Persisted instance name, not a reusable template name.                    | `example` |
| `temporaryConnections.example.spec.graphUid`           | **Type: string.** Required. No CRD default; the value shown is illustrative. Expected graph incarnation.                                               | `example` |
| `temporaryConnections.example.spec.source`             | **Type: string.** Required. No CRD default; the value shown is illustrative. Sending logical node or replica ordinal.                                  | `example` |
| `temporaryConnections.example.spec.target`             | **Type: string.** Required. No CRD default; the value shown is illustrative. Receiving logical node or replica ordinal.                                | `example` |
| `temporaryConnections.example.spec.ttlSeconds`         | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. Lifetime measured from Kubernetes receipt creation.            | `1` |
| `temporaryConnections.example.spec.ports`              | **Type: array or string.** Optional. No CRD default; the value shown is illustrative. Destination grants, empty for topology only.                     | `[{"port": 1, "protocol": "TCP"}]` |
| `temporaryConnections.example.spec.bidirectional`      | **Type: boolean or string.** Optional. No CRD default; the value shown is illustrative. Whether to add the reverse connection with the same ports.     | `false` |
| `temporaryConnections.example.spec.peers`              | **Type: object or string.** Optional. No CRD default; the value shown is illustrative. Exact service participants for an atlas-coordinated connection. | `{}` |
| `temporaryConnections.example.spec.requester.cluster`  | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                           | `example` |
| `temporaryConnections.example.spec.requester.username` | **Type: string.** Required. No CRD default; the value shown is illustrative.                                                                           | `example` |
| `temporaryConnections.example.spec.requester.uid`      | **Type: string.** Required. No CRD default; the value shown is illustrative.                                                                           | `example` |
