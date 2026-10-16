# compositions configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Composition field reference](#composition-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/compositions/values.yaml
```

## Parameters

### Composition field reference

| Name                                        | Description                                                                                                                                                                                                                                           | Value     |
| ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `compositions.example.metadata.name`        | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                          | `example` |
| `compositions.example.metadata.namespace`   | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                          | `example` |
| `compositions.example.metadata.labels`      | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                | `{}` |
| `compositions.example.metadata.annotations` | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                | `{}` |
| `compositions.example.spec.requestId`       | **Type: string.** Required. No CRD default; the value shown is illustrative. Stable client-generated request identity.                                                                                                                                | `example` |
| `compositions.example.spec.document`        | **Type: string.** Required. No CRD default; the value shown is illustrative. Canonical JSON CompositionRequest with rootId and ID-addressed objects. Stored as a string so CEL can enforce immutability even for arbitrary nested workload manifests. | `example` |
