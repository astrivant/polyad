# gates configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Gate field reference](#gate-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/gates/values.yaml
```

## Parameters

### Gate field reference

| Name                                 | Description                                                                                                                                                                                                                                                                                                                    | Value     |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------- |
| `gates.example.metadata.name`        | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                                                                   | `example` |
| `gates.example.metadata.namespace`   | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                                                                   | `example` |
| `gates.example.metadata.labels`      | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                                                         | `{}` |
| `gates.example.metadata.annotations` | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                                                         | `{}` |
| `gates.example.spec.expression`      | **Type: object or string.** Optional. No CRD default; the value shown is illustrative. Serialized Gate expression with operator, optional operands and signal name. Operators are signal, and, or, not, xor and nxor. Signals use NODE.started, NODE.ready, NODE.completed or NODE.failed; absent observations remain unknown. | `{}` |
| `gates.example.spec.delaySeconds`    | **Type: number or string.** Optional. No CRD default; the value shown is illustrative. Standalone admission delay after dependencies are observed. Each graph instance persists its own deadline in status; waits do not occupy workers or slots.                                                                              | `0` |
