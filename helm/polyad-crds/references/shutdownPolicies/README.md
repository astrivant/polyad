# shutdownPolicies configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [ShutdownPolicy field reference](#shutdownpolicy-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/shutdownPolicies/values.yaml
```

## Parameters

### ShutdownPolicy field reference

| Name                                            | Description                                                                                                                                                                                                                                                                            | Value     |
| ----------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `shutdownPolicies.example.metadata.name`        | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                           | `example` |
| `shutdownPolicies.example.metadata.namespace`   | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                           | `example` |
| `shutdownPolicies.example.metadata.labels`      | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                 | `{}` |
| `shutdownPolicies.example.metadata.annotations` | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                 | `{}` |
| `shutdownPolicies.example.spec.graceSeconds`    | **Type: integer or string.** Optional. Default: 30. Pod termination grace period in seconds. Applications should drain or checkpoint during SIGTERM or preStop handling before Kubernetes ends this period.                                                                            | `30` |
| `shutdownPolicies.example.spec.afterSeconds`    | **Type: integer or string.** Optional. No CRD default; the value shown is illustrative. Graph lifetime limit in seconds from the Graph CR creation timestamp, including operator downtime. Reaching the limit stops admission and drains owned execution; omission disables the limit. | `0` |
