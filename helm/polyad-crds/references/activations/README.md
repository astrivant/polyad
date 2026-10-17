# activations configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Activation field reference](#activation-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/activations/values.yaml
```

## Parameters

### Activation field reference

| Name                                            | Description                                                                                                                                                   | Value     |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `activations.example.metadata.name`             | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                  | `example` |
| `activations.example.metadata.namespace`        | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                  | `example` |
| `activations.example.metadata.labels`           | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                        | `{}` |
| `activations.example.metadata.annotations`      | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                        | `{}` |
| `activations.example.spec.requestId`            | **Type: string.** Required. No CRD default; the value shown is illustrative. Client idempotency identity, shared across API retries.                          | `example` |
| `activations.example.spec.kind`                 | **Type: string.** Required. No CRD default; the value shown is illustrative. Choices: "Graph", "PolyGraph", "ReplicaGroup". Containing executable graph kind. | `Graph` |
| `activations.example.spec.graph`                | **Type: string.** Required. No CRD default; the value shown is illustrative. Containing graph instance name.                                                  | `example` |
| `activations.example.spec.graphUid`             | **Type: string.** Required. No CRD default; the value shown is illustrative. Graph incarnation; recreated graphs cannot consume old requests.                 | `example` |
| `activations.example.spec.node`                 | **Type: string.** Required. No CRD default; the value shown is illustrative. Downstream logical vertex name.                                                  | `example` |
| `activations.example.spec.graphGeneration`      | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. Graph revision observed at acceptance.                                | `1` |
| `activations.example.spec.definitionUid`        | **Type: string.** Required. No CRD default; the value shown is illustrative. Reusable target definition incarnation.                                          | `example` |
| `activations.example.spec.definitionGeneration` | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. Reusable target definition revision.                                  | `1` |
