# tracing configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Enable in singular, HA or split deployments. Root-managed workers inherit this

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/tracing/values.yaml
```

## Parameters

### Configuration

| Name                         | Description                                                                                                                                      | Value                                                    |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------- |
| `tracing.enabled`            | **Type: boolean.** Create a batched trace exporter only when enabled.                                                                            | `true` |
| `tracing.endpoint`           | **Type: string.** Full HTTP/protobuf trace endpoint including its trace path, usually /v1/traces.                                                | `http://otel-collector.observability.svc:4318/v1/traces` |
| `tracing.serviceName`        | **Type: string.** Service identity used to group operator traces.                                                                                | `polyad-operator` |
| `tracing.samplingRatio`      | **Type: number.** Fraction of new root traces sampled; parent sampling decisions are honored.                                                    | `0.1` |
| `tracing.timeoutSeconds`     | **Type: integer.** Bound the OTLP export request duration in seconds.                                                                            | `10` |
| `tracing.resourceAttributes` | **Type: string.** Comma-separated key=value resource attributes describing the deployment.                                                       | `deployment.environment.name=production` |
| `tracing.headersSecret`      | **Type: string.** Optional existing Secret with exporter credentials in its headers key.                                                         | `""` |
| `tracing.logs.enabled`       | **Type: boolean.** Export decision logs independently of trace sampling; disabled keeps console logs without a log exporter.                     | `true` |
| `tracing.logs.endpoint`      | **Type: string.** Full HTTP/protobuf log URL, usually /v1/logs. Shares trace service identity, timeout, resource attributes and exporter Secret. | `http://otel-collector.observability.svc:4318/v1/logs` |
