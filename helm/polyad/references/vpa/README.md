# vpa configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Opt-in compatibility with a separately installed Vertical Pod Autoscaler.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/vpa/values.yaml
```

## Parameters

### Configuration

| Name                             | Description                                                                                                                                                  | Value  |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------ |
| `verticalPodAutoscaling.enabled` | **Type: boolean.** Allow graph Resource nodes to own native VPA policies and project their bounds into targeted application containers; does not install VPA | `true` |
