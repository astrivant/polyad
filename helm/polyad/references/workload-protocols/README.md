# workload-protocols configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Optional workload protocols; guide: ../../../docs/workloads/workload-protocols.md.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/workload-protocols/values.yaml
```

## Parameters

### Configuration

| Name                  | Description                                                                                                     | Value   |
| --------------------- | --------------------------------------------------------------------------------------------------------------- | ------- |
| `mesh.enabled`        | **Type: boolean.** Install the bundled Istio components; keep false with a separately managed mesh.             | `false` |
| `connections.enabled` | **Type: boolean.** Enable only after configuring workload RBAC and connection access ceilings.                  | `false` |
| `events.enabled`      | **Type: boolean.** Enable with event credentials and authorization when using adaptive receipt-checked clients. | `false` |
