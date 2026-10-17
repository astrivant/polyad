# dragonflyPools configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [DragonflyPool field reference](#dragonflypool-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/dragonflyPools/values.yaml
```

## Parameters

### DragonflyPool field reference

| Name                                          | Description                                                                             | Value     |
| --------------------------------------------- | --------------------------------------------------------------------------------------- | --------- |
| `dragonflyPools.example.metadata.name`        | **Type: string.** Optional. No CRD default; the value shown is illustrative.            | `example` |
| `dragonflyPools.example.metadata.namespace`   | **Type: string.** Optional. No CRD default; the value shown is illustrative.            | `example` |
| `dragonflyPools.example.metadata.labels`      | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.  | `{}` |
| `dragonflyPools.example.metadata.annotations` | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.  | `{}` |
| `dragonflyPools.example.spec.replicas`        | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. | `2` |
| `dragonflyPools.example.spec.minReplicas`     | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. | `2` |
| `dragonflyPools.example.spec.maxReplicas`     | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. | `2` |
