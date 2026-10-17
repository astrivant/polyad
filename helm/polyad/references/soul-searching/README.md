# soul-searching configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Guide: ../../../../docs/graphs/load-profiles.md

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/soul-searching/values.yaml
```

## Parameters

### Configuration

| Name               | Description                                                                                                              | Value  |
| ------------------ | ------------------------------------------------------------------------------------------------------------------------ | ------ |
| `capacity.enabled` | **Type: boolean.** Permit advance-capacity helpers; a node autoscaler must be configured separately.                     | `true` |
| `capacity.maxPods` | **Type: integer.** Fixed per-boundary operator ceiling; each graph and its approved profiles may choose smaller budgets. | `32` |
