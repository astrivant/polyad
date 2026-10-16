# telemetry configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Supply existing remote-write, Loki and OTLP/HTTP backends. No backend is installed here.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/telemetry/values.yaml
```

## Parameters

### Configuration

| Name                                           | Description                                                                                                                                                                                                    | Value                                                                      |
| ---------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| `telemetry.enabled`                            | **Type: boolean.** Install a release-scoped collector; backends are supplied separately.                                                                                                                       | `true` |
| `telemetry.mode`                               | **Type: string.** Alloy collects metrics, container logs and traces; PrometheusAgent collects metrics only.                                                                                                    | `Alloy` |
| `telemetry.replicas`                           | **Type: integer.** Alloy distributes scrape and log targets across these replicas; PrometheusAgent requires one.                                                                                               | `2` |
| `telemetry.clusterName`                        | **Type: string.** Stable cluster label attached to collected signals; empty uses global.multiCluster.clusterName or the release namespace.                                                                     | `study` |
| `telemetry.remoteWrite.url`                    | **Type: string.** Prometheus-compatible remote-write URL, including its write path.                                                                                                                            | `http://benchmarks-monitoring-prometheus.monitoring.svc:9090/api/v1/write` |
| `telemetry.logs.enabled`                       | **Type: boolean.** Alloy tails selected containers through the Kubernetes API without host mounts.                                                                                                             | `true` |
| `telemetry.logs.url`                           | **Type: string.** Loki push URL including /loki/api/v1/push.                                                                                                                                                   | `http://loki-gateway.monitoring.svc/loki/api/v1/push` |
| `telemetry.traces.enabled`                     | **Type: boolean.** Alloy accepts OTLP/gRPC and OTLP/HTTP traces on Pod IPs and forwards them over OTLP/HTTP.                                                                                                   | `true` |
| `telemetry.traces.endpoint`                    | **Type: string.** Destination OTLP/HTTP base URL, without /v1/traces.                                                                                                                                          | `http://benchmarks-tempo.monitoring.svc:4318` |
| `telemetry.traces.routeOperator`               | **Type: boolean.** Route Helm-installed operator and observer traces through local Alloy; tracing.enabled must also be true. Use false for root-provisioned cross-cluster workers requiring a shared endpoint. | `true` |
| `tracing.enabled`                              | **Type: boolean.** Create a batched trace exporter only when enabled.                                                                                                                                          | `true` |
| `kedaOperator.prometheus.operator.enabled`     | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                                                                                  | `true` |
| `kedaOperator.prometheus.metricServer.enabled` | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                                                                                  | `true` |
| `kedaOperator.prometheus.webhooks.enabled`     | **Type: boolean.** Expose this bundled KEDA component metrics for collection.                                                                                                                                  | `true` |
