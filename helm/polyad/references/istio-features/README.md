# istio-features configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Optional Istio feature bundle for an existing or bundled mesh.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/istio-features/values.yaml
```

## Parameters

### Configuration

| Name                                              | Description                                                                                           | Value                                                         |
| ------------------------------------------------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `mesh.enabled`                                    | **Type: boolean.** Enable generation of Istio resources.                                              | `true` |
| `mesh.operator.enabled`                           | **Type: boolean.** Inject and secure Polyad operator Pods.                                            | `true` |
| `mesh.telemetry.enabled`                          | **Type: boolean.** Create an Istio Telemetry resource without changing mesh-wide defaults.            | `true` |
| `mesh.telemetry.metricsProviders`                 | **Type: array.** Existing Istio extension providers receiving proxy metrics.                          | `["prometheus"]` |
| `mesh.telemetry.accessLogging.enabled`            | **Type: boolean.** Export selected Envoy access records.                                              | `true` |
| `mesh.telemetry.accessLogging.providers`          | **Type: array.** Existing Istio extension providers receiving access logs.                            | `["envoy"]` |
| `mesh.telemetry.accessLogging.filter`             | **Type: string.** CEL expression limiting volume to failures and requests taking at least one second. | `response.code >= 500 \|\| response.duration >= duration('1s')` |
| `mesh.telemetry.tracing.enabled`                  | **Type: boolean.** Export Envoy spans through the configured provider.                                | `true` |
| `mesh.telemetry.tracing.providers`                | **Type: array.** Existing Istio tracing providers; replace otel with the installed provider name.     | `["otel"]` |
| `mesh.telemetry.tracing.randomSamplingPercentage` | **Type: number.** Percentage of requests sampled independently by Envoy.                              | `1` |
| `mesh.sidecar.enabled`                            | **Type: boolean.** Create a workload-scoped Sidecar resource.                                         | `true` |
| `mesh.sidecar.egressHosts`                        | **Type: array.** Service namespaces and registry hosts visible to operator proxies.                   | `["./*", "istio-system/*"]` |
| `mesh.authorization.audit.enabled`                | **Type: boolean.** Enable Istio AUDIT action for the selected methods and paths.                      | `true` |
| `mesh.authorization.audit.paths`                  | **Type: array.** Endpoint patterns included in audit telemetry.                                       | `["/v1/*"]` |
| `mesh.authorization.audit.methods`                | **Type: array.** HTTP methods included in audit telemetry.                                            | `["POST"]` |
| `mesh.authorization.dryRunDeny.enabled`           | **Type: boolean.** Mark the generated DENY policy with Istio's dry-run annotation.                    | `true` |
| `mesh.authorization.dryRunDeny.paths`             | **Type: array.** Endpoint patterns that would be denied.                                              | `["/v1/compositions"]` |
| `mesh.authorization.dryRunDeny.methods`           | **Type: array.** HTTP methods that would be denied.                                                   | `["POST"]` |
| `mesh.egress.enabled`                             | **Type: boolean.** Create namespace-private ServiceEntries for declared destinations.                 | `true` |
| `mesh.egress.destinations`                        | **Type: array.** Unique DNS destinations; the managed gateway supports TLS on port 443.               | `[{"name": "payments", "host": "payments.example.com", "port": 443, "protocol": "TLS"}]` |
| `mesh.egress.gateway.enabled`                     | **Type: boolean.** Install the pinned gateway dependency and route TLS/443 destinations through it.   | `true` |
