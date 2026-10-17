# prometheus-agent configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Alternative metrics-only collector. Do not install two collectors for the same release targets.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/prometheus-agent/values.yaml
```

## Parameters

### Configuration

| Name                                           | Description                                                                                                                                                                                                    | Value                                                                      |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `telemetry.enabled`                            | **Type: boolean.** Install a release-scoped collector; backends are supplied separately.                                                                                                                       | `true` |
| `telemetry.mode`                               | **Type: string.** Alloy collects metrics, container logs and traces; PrometheusAgent collects metrics only.                                                                                                    | `PrometheusAgent` |
| `telemetry.replicas`                           | **Type: integer.** Alloy distributes scrape and log targets across these replicas; PrometheusAgent requires one.                                                                                               | `1` |
| `telemetry.remoteWrite.url`                    | **Type: string.** Prometheus-compatible remote-write URL, including its write path.                                                                                                                            | `http://benchmarks-monitoring-prometheus.monitoring.svc:9090/api/v1/write` |
| `telemetry.logs.enabled`                       | **Type: boolean.** Alloy tails selected containers through the Kubernetes API without host mounts.                                                                                                             | `false` |
| `telemetry.traces.enabled`                     | **Type: boolean.** Alloy accepts OTLP/gRPC and OTLP/HTTP traces on Pod IPs and forwards them over OTLP/HTTP.                                                                                                   | `false` |
| `telemetry.traces.routeOperator`               | **Type: boolean.** Route Helm-installed operator and observer traces through local Alloy; tracing.enabled must also be true. Use false for root-provisioned cross-cluster workers requiring a shared endpoint. | `false` |
| `kedaOperator.prometheus.operator.enabled`     | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                                                                                  | `true` |
| `kedaOperator.prometheus.metricServer.enabled` | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                                                                                  | `true` |
| `kedaOperator.prometheus.webhooks.enabled`     | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                                                                                  | `true` |
