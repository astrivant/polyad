# OpenTelemetry traces and decision logs

Polyad includes the OpenTelemetry Python API, SDK and OTLP/HTTP trace and log
exporters as operator dependencies. Both exports are opt-in and disabled by default. Tracing uses the
existing Flask application and exports batches in a background thread; it does
not start another API server. Metrics remain available through the existing
Prometheus endpoint for KEDA.

## Table of contents

- [Enable tracing](#enable-tracing)
- [Span coverage and propagation](#span-coverage-and-propagation)
- [Decision and conflict logs](#decision-and-conflict-logs)
  - [Read a decision](#read-a-decision)
  - [Export logs](#export-logs)
  - [Coverage and severity](#coverage-and-severity)
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
| `polyad.operator_topology.refresh` | Refresh reserved root PolyGraph membership |
| `polyad.operator_pool.reconcile` | Install, update or retire a root-managed remote operator group |
| `polyad.remote_scale.reconcile` | Forward a locally authorized remote scale request or reject a conflict |

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

## Decision and conflict logs

Operator decisions use Python logging with a human-readable explanation and
structured attributes. Console logging works without a collector. Optional OTLP
export maps records to the [OpenTelemetry log data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/):
the message is the `Body`, with timestamps, normalized severity, instrumentation
scope, resource identity, `EventName` and decision attributes. When a current span
exists, the SDK attaches its trace ID, span ID and flags.

### Read a decision

For example, a local ReplicaGroup edit can invalidate a remote scaling request:

```text
2026-09-16T12:00:00Z WARNING [polyad-kopf] polyad.operator.observability.decisions:
A local edit superseded this remote scaling request; local intent takes precedence.
| event=polyad.remote_scale.conflict polyad.decision.outcome=blocked
  polyad.decision.reason=local_edit_wins polyad.resource.kind=RemoteScale
  k8s.namespace.name=management polyad.resource.name=consumers
  polyad.resource.uid=request-123 polyad.target.cluster=west
  polyad.generation.expected=7 polyad.generation.observed=8
```

The example wraps one console record across lines for readability. A real traced
record also includes `trace_id`, `span_id` and `trace_flags`. Records outside a
trace omit these identifiers. The request UID distinguishes competing requests
with similar names; the generations explain why local intent won.

| Field | Meaning |
| --- | --- |
| `event` in console / `EventName` in OTLP | Stable class of decision, such as `polyad.remote_scale.conflict` |
| `polyad.decision.outcome`, `polyad.decision.reason` | Result and a machine-readable explanation |
| `polyad.resource.*`, `k8s.namespace.name` attributes | Subject kind, name, UID and generation where available, and its namespace |
| `polyad.target.cluster` | Cluster whose resource or operator group the decision concerns |
| `polyad.replica.id`, `polyad.shard` | Replica and graph shard holding the reconciliation duty, when available |
| `polyad.request.*` | Mutation identities that must be ordered or whose preconditions failed |
| Resource `service.name`, `service.instance.id`, `process.pid` | Emitting service and this process incarnation, shared by logs and traces |
| Resource `k8s.cluster.name`, `k8s.namespace.name`, `k8s.pod.name`, `k8s.pod.uid` | Hosting cluster and Pod; populated by Helm and retained correctly when the root provisions a remote worker |

The emitting Pod may be in a different cluster from the target. Follow
`polyad.operator_topology.membership` to see the root group and remote operator
groups enter or leave the [reserved root PolyGraph](../deployment/root-control-plane.md#reserved-operator-hierarchy).
Membership logs identify the group node, Graph definition and destination cluster.
Registering credentials alone does not add a workload group: provision an
OperatorPool to create and link its Graph.

### Export logs

Log export is independently enabled; trace sampling does not discard decision
logs. This configuration exports logs while leaving tracing disabled:

```yaml
operator:
  logLevel: INFO
tracing:
  enabled: false
  serviceName: polyad-operator
  timeoutSeconds: 10
  resourceAttributes: deployment.environment.name=production
  headersSecret: otel-credentials
  logs:
    enabled: true
    endpoint: http://otel-collector.observability.svc:4318/v1/logs
```

`headersSecret` is optional; omit it for an unauthenticated collector. Logs share
the trace configuration's service name, resource attributes, timeout and Secret,
but use their own full `/v1/logs` endpoint. Root-managed workers inherit the export
configuration and copied Secret references. The collector needs a logs receiver
and pipeline as well as any traces pipeline. Both endpoints must be reachable
from each hosting cluster.

Outside Helm, set `POLYAD_LOGS_ENABLED=true` and
`OTEL_EXPORTER_OTLP_LOGS_ENDPOINT=http://localhost:4318/v1/logs`. Standard
`OTEL_EXPORTER_OTLP_LOGS_*` settings configure log headers, TLS and timeouts;
`OTEL_BLRP_*` configures the bounded batch queue. Only HTTP/protobuf is supported.
`OTEL_SDK_DISABLED=true` disables both exporters. Log filtering still follows
`operator.logLevel`, `POLYAD_LOG_LEVEL` or the CLI verbosity setting.

### Coverage and severity

| Decision | Normal visibility |
| --- | --- |
| Operator group linked/unlinked; root-managed resources created/updated | INFO after an acknowledged write; unchanged membership is quiet |
| Workload creation/deletion, committed phase, scale and capacity transitions | INFO; heartbeat and metric-only status updates do not repeat decisions |
| Throughput observation, stabilization, recommendation, cooldown and applied layout | INFO when the decision, target, mode or recommended layout changes |
| GraphRule rejection, ownership collision, rewrite/remote-scale generation conflict | WARNING, with the rule or conflicting resource/request identities |
| Kubernetes HTTP 409 | WARNING; refresh state before retrying, without logging the API error body |
| Pending Kubernetes write conflict or stale queued target | WARNING via `polyad.kubernetes.write_deferred`; includes a stable reason and target identity, without request bodies or digests |
| Identical pending write and dependency contract | DEBUG via `polyad.kubernetes.write_coalesced`; one queue entry and acknowledgement serve the callers |
| Independent approved writes overlap | INFO via `polyad.kubernetes.write_parallel`; records occupied writer slots after planner and captured-dependency checks |
| Root authority or shard lease lost during a write | WARNING; mutation is fenced until valid authority returns |
| Another replica owns the work, dependency/delay/gate/capacity waits, successful rule checks | DEBUG for detailed reconciliation diagnostics |
| Mutation ordering and dependency explanations | DEBUG with both request identities; changed preconditions or shared budgets are WARNING |
| Unexpected reconciliation failure | ERROR with its exception type and resource identity |

The [pending write check](../development/mutations.md#queued-kubernetes-write-conflicts)
uses `overlapping_pending_writes` when incompatible changes are waiting for the
same object. `queued_write_revision_changed` and `queued_write_uid_changed`
distinguish changed state from object replacement after a queue delay. These
decisions stop dispatch and require refreshed intent; they do not select a winning
policy or roll back a completed write.

`dependency_state_changed` identifies drift in the decision's captured read set.
`write_target_disappeared` covers an update rejected with 404 after validation;
`write_queue_full` identifies bounded admission backpressure. These failures
request [targeted recovery](../development/write-pipeline.md#failure-and-cancellation),
not automatic replay of old patches. Cached validations expire or are invalidated
by relevant watches and known mutations.

Repeated failed attempts can emit repeated warnings. These records are diagnostic
observations, not an exactly-once audit journal. They neither bypass GraphRules nor
change conflict resolution. Logs are independent of downstream workload event
subscriptions: logging an internal operator graph does not expose it through those
[event streams](../workloads/workload-events.md).

Structured decisions select identities and bounded scalar details; they do not
serialize resource specifications, Secret contents, authorization headers or
application payloads. OTLP exception records include the exception type without
copying its message or traceback. Existing console exception diagnostics retain
their traceback behavior.

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
builders directly can call `polyad.operator.observability.tracing.configure_tracing()` before
starting workers and `shutdown_tracing()` after stopping them. Standalone client
and types packages do not gain the operator's SDK dependencies.

Log export similarly owns one process-local provider and one bounded batch
processor. Entrypoints initialize it before workers start and drain it after they
stop. Embedders use `polyad.operator.observability.logging.configure_log_export()` and
`shutdown_log_export()`. Collector failures do not change admitted operations;
buffer overflow or abrupt termination can lose records. Disabled log export loads
no log SDK/exporter and starts no log export thread.

## References

- [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/)
- [Python instrumentation](https://opentelemetry.io/docs/languages/python/instrumentation/)
- [Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [OpenTelemetry log data model](https://opentelemetry.io/docs/specs/otel/logs/data-model/)
