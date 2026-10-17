# operatorPools configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [OperatorPool field reference](#operatorpool-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/operatorPools/values.yaml
```

## Parameters

### OperatorPool field reference

| Name                                            | Description                                                                                                                                                                                       | Value        |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------ |
| `operatorPools.example.metadata.name`           | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                      | `example` |
| `operatorPools.example.metadata.namespace`      | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                      | `example` |
| `operatorPools.example.metadata.labels`         | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                            | `{}` |
| `operatorPools.example.metadata.annotations`    | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                            | `{}` |
| `operatorPools.example.spec.existingDeployment` | **Type: string.** Optional. No CRD default; the value shown is illustrative. Helm-owned Deployment in the registered namespace; root never replaces its template or deletes it.                   | `example` |
| `operatorPools.example.spec.scalingAuthority`   | **Type: string.** Optional. Default: "Root". Choices: "Root", "Local". Root may patch replicas after admission; Local only observes the Helm-owned Deployment.                                    | `Root` |
| `operatorPools.example.spec.controller`         | **Type: string.** Optional. Default: "Deployment". Choices: "Deployment", "DaemonSet". Deployment scales by replicas; DaemonSet executes through a reserved PolyGraph and follows eligible nodes. | `Deployment` |
| `operatorPools.example.spec.cluster`            | **Type: string.** Required. No CRD default; the value shown is illustrative.                                                                                                                      | `example` |
| `operatorPools.example.spec.replicas`           | **Type: integer or string.** Required. Default: 1.                                                                                                                                                | `1` |
| `operatorPools.example.spec.resources`          | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                            | `{}` |
| `operatorPools.example.spec.nodeSelector`       | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                            | `{}` |
| `operatorPools.example.spec.tolerations`        | **Type: array or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                             | `[]` |
