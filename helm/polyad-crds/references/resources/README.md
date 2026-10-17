# resources configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Resource field reference](#resource-field-reference)
<!-- toc:end -->

Generated from the CRDs; regenerate with scripts/schemas/generate-all.py.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad-crds --values helm/polyad-crds/references/resources/values.yaml
```

## Parameters

### Resource field reference

| Name                                         | Description                                                                                                                                                                                                                                                                               | Value     |
| -------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `resources.example.metadata.name`            | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                              | `example` |
| `resources.example.metadata.namespace`       | **Type: string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                              | `example` |
| `resources.example.metadata.labels`          | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                    | `{}` |
| `resources.example.metadata.annotations`     | **Type: object or string.** Optional. No CRD default; the value shown is illustrative.                                                                                                                                                                                                    | `{}` |
| `resources.example.spec.manifest.apiVersion` | **Type: string.** Required. No CRD default; the value shown is illustrative. Choices: "v1", "autoscaling.k8s.io/v1". API version of the owned resource. VPA manifests require the opt-in operator compatibility feature.                                                                  | `v1` |
| `resources.example.spec.manifest.kind`       | **Type: string.** Required. No CRD default; the value shown is illustrative. Choices: "Service", "ConfigMap", "PersistentVolumeClaim", "VerticalPodAutoscaler". Kind of namespaced resource to create: Service, ConfigMap, PersistentVolumeClaim or VerticalPodAutoscaler.                | `Service` |
| `resources.example.spec.manifest.spec`       | **Type: object or string.** Optional. No CRD default; the value shown is illustrative. Native resource spec. VPA targetRef.name may reference `${nodes.NAME.name}`; resourcePolicy containerPolicies retain and validate minAllowed and maxAllowed CPU/memory quantities before creation. | `{}` |
| `resources.example.spec.manifest.data`       | **Type: object or string.** Optional. No CRD default; the value shown is illustrative. UTF-8 string data for a ConfigMap. Keys are preserved in the graph-owned resource.                                                                                                                 | `{}` |
| `resources.example.spec.manifest.binaryData` | **Type: object or string.** Optional. No CRD default; the value shown is illustrative. Base64-encoded binary values for a ConfigMap.                                                                                                                                                      | `{}` |
| `resources.example.spec.manifest.immutable`  | **Type: boolean or string.** Optional. No CRD default; the value shown is illustrative. Make ConfigMap data immutable when true. Intended for ConfigMap manifests.                                                                                                                        | `false` |
