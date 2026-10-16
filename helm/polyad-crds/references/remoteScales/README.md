# remoteScales configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [RemoteScale field reference](#remotescale-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/remoteScales/values.yaml
```

## Parameters

### RemoteScale field reference

| Name                                          | Description                                                                                                                                                          | Value     |
| --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `remoteScales.example.metadata.name`          | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                         | `example` |
| `remoteScales.example.metadata.namespace`     | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                         | `example` |
| `remoteScales.example.metadata.labels`        | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                               | `{}` |
| `remoteScales.example.metadata.annotations`   | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                               | `{}` |
| `remoteScales.example.spec.cluster`           | **Type: string.** Required. No CRD default; the value shown is illustrative.                                                                                         | `example` |
| `remoteScales.example.spec.replicas`          | **Type: integer or string.** Required. Default: 1.                                                                                                                   | `1` |
| `remoteScales.example.spec.target.name`       | **Type: string.** Required. No CRD default; the value shown is illustrative.                                                                                         | `example` |
| `remoteScales.example.spec.target.uid`        | **Type: string.** Required. No CRD default; the value shown is illustrative.                                                                                         | `example` |
| `remoteScales.example.spec.target.generation` | **Type: integer or string.** Required. No CRD default; the value shown is illustrative. Locally approved target spec generation; local edits invalidate the request. | `1` |
