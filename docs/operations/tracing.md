# OpenTelemetry traces

Polyad includes the OpenTelemetry Python API, SDK and OTLP/HTTP trace exporter as
operator dependencies. Tracing is opt-in and disabled by default. It uses the
existing Flask application and exports batches in a background thread; it does
not start another API server. Metrics remain available through the existing
Prometheus endpoint for KEDA.

## Table of contents

- [Enable tracing](#enable-tracing)
- [Span coverage and propagation](#span-coverage-and-propagation)
- [Export configuration and lifecycle](#export-configuration-and-lifecycle)
- [References](#references)

## Enable tracing

Use the typed [tracing values reference](../../charts/polyad/values-tracing.reference.yaml)
with any deployment architecture:

```yaml
tracing:
  enabled: true
  endpoint: http://otel-collector.observability.svc:4318/v1/traces
  serviceName: polyad-operator
  samplingRatio: 0.1
  timeoutSeconds: 10
  resourceAttributes: deployment.environment.name=production
  headersSecret: ''
```

The endpoint is the full **HTTP/protobuf** trace URL, usually ending in
`/v1/traces`. The chart configures parent-based ratio sampling: `0.1` samples 10%
of new root traces, while children honor the parent's decision. Use `1` to sample
every new root trace or `0` to sample only traces with a sampled incoming parent.

For authenticated collectors, set `headersSecret` to an existing Secret in the
release namespace. Its `headers` key contains the exporter's comma-separated
`key=value` header configuration, with values URL-encoded where necessary.
The chart references that key through `secretKeyRef`; it does not embed its value.
Changing an environment-backed Secret requires a Pod restart; the chart's optional
[Secret-change reloader](authentication.md) can handle this.

Operators, split components and read-only observers receive the configuration.
Root-managed execution workers inherit the root Pod configuration and its
referenced Secrets through the existing worker bootstrap. Choose a collector URL
reachable from every execution cluster; a management-cluster `.svc` hostname does
not automatically resolve in remote clusters. Install collectors separately.

When NetworkPolicy is enabled, allow the collector through
`networkPolicy.extraEgress` and any observer/remote-cluster egress policies. For
example, adapt the selectors to the collector's actual labels:

```yaml
networkPolicy:
  extraEgress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: observability
          podSelector:
            matchLabels:
              app.kubernetes.io/name: opentelemetry-collector
      ports:
        - protocol: TCP
          port: 4318
```

## Span coverage and propagation

| Span | Meaning |
| --- | --- |
| `METHOD /route/<parameter>` | Dispatch of a request through the shared Flask application, including authentication and handler execution |
| `polyad.reconcile` | One awaited controller reconciliation pass |
| `polyad.kubernetes.request` | An awaited Kubernetes API operation, including admission/transport waits |

HTTP spans continue valid W3C `traceparent` headers. Invalid headers start a new
trace. Only trace identity and sampling are consumed; baggage and arbitrary
request headers are not collected. Context follows in-process async operations,
including requests submitted from HTTP threads to the operator loop. Background
queue reconciliation starts a new trace: trace context is not persisted in the
queue, database or Kubernetes resources, nor injected into outbound HTTP calls.

HTTP attributes include the method, route template, endpoint family and returned
status. Kubernetes spans identify the method and resource kind. Unhandled errors
record their type and an error status without exception messages or stack traces.
No bodies, credentials, query strings or actual route parameter values are added.
Resource attributes supplied by administrators are exported as configured.

`/metrics`, `/healthz` and `/readyz` requests are excluded. A streaming response's
span ends after dispatch prepares the response; it does not remain open for the
SSE subscription or cover later stream iteration. Requests rejected before Flask,
such as by the HTTP server, do not produce Flask spans.

## Export configuration and lifecycle

Outside Helm, enable tracing with `POLYAD_TRACING_ENABLED=true` and standard
OpenTelemetry environment variables:

```bash
export POLYAD_TRACING_ENABLED=true
export OTEL_SERVICE_NAME=polyad-operator
export OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://localhost:4318/v1/traces
export OTEL_TRACES_SAMPLER=parentbased_traceidratio
export OTEL_TRACES_SAMPLER_ARG=0.1
polyad-operator --namespace workloads
```

The SDK honors `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG`,
`OTEL_RESOURCE_ATTRIBUTES` and `OTEL_BSP_*` batch controls. Without explicit sampler
settings, its default samples root traces and follows parent decisions. The HTTP
exporter honors standard `OTEL_EXPORTER_OTLP_*` and trace-specific overrides for
the endpoint, headers, timeout and TLS certificates. A generic OTLP endpoint is a
base URL; a trace-specific endpoint is the complete trace URL. Only
`http/protobuf` is supported; configuring `grpc` fails at startup.

`OTEL_SDK_DISABLED=true` disables Polyad's exporter even when its own enable flag
is set. Disabled processes create no export thread. Enabled processes own one
provider, using a bounded batch queue, and shut it down after operator/HTTP work
stops to export remaining spans. Collector failures do not roll back graph
operations; spans may be lost when export fails, buffers fill or a process is
terminated abruptly.

Polyad owns this provider explicitly and does not replace the global Python
provider or automatically instrument other libraries. Embedders using the app
builders directly can call `polyad.operator.tracing.configure_tracing()` before
starting workers and `shutdown_tracing()` after stopping them. Standalone client
and types packages do not gain the operator's SDK dependencies.

## References

- [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/)
- [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)
