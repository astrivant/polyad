# Operator performance and autoscaling

Operator tuning lives under `operator` in the chart values. Scaling controls
change how many replicas run; polling controls change how often each replica
checks for work and publishes observations. Both affect response time and the
load placed on Kubernetes and the shared cache.

Start with the typed [tuning values reference](../../charts/polyad/values-tuning.reference.yaml).
It also exposes `operator.cheeger` computation ceilings; see
[Cheeger search budgets and priorities](../graphs/cheeger-tuning.md#understand-computation-and-scale)
for how they bound per-rule and application-feedback work.
The [metric inventory](metrics.md#metric-inventory-and-scope) lists every exposed
family, its scope and the feature required to publish it.

## Table of contents

- [Resource baseline and right-sizing](#resource-baseline-and-right-sizing)
- [Autoscaling response](#autoscaling-response)
- [Worker cadence](#worker-cadence)
- [Write admission and validation](#write-admission-and-validation)
- [KEDA-managed targets](#keda-managed-targets)

## Resource baseline and right-sizing

Each operator Python container starts with **1 CPU and 1 GiB of memory**, with
requests equal to limits. This gives unmodified operator Pods the `Guaranteed`
QoS class. Dense and HA replicas, the Distributed bootstrap/gateway/executor/
telemetry components, and Helm-installed workers use `operator.resources`.
Root-provisioned workers inherit that Pod template unless their OperatorPool
sets `resources`; the shipped pool reference uses the same baseline. Read-only
observers use the same default through `observer.resources`.

```yaml
operator:
  resources:
    requests: {cpu: "1", memory: 1Gi}
    limits: {cpu: "1", memory: 1Gi}
```

When operator or observer mesh injection is enabled, `mesh.proxyResources`
sets equal requests and limits for Istio proxy and init containers: `100m` CPU
and `128Mi` memory by default. That allocation is **additional** to the Python
container's 1 CPU / 1 GiB. Preserve matching, nonzero CPU and memory pairs in
any other containers injected by your cluster to retain Guaranteed QoS.

Use [Grafana during a load study](../../studies/load/README.md#monitoring-and-traces)
to observe CPU usage and throttling, memory working set, restarts, reconciliation
latency and write backlog before changing allocations. Adjust both requests and
limits together, then repeat the same benchmark plan. Changes to requests also
change the HPA's utilization baseline. PostgreSQL, Dragonfly and collection
agents retain their separate resource settings.

## Autoscaling response

Configure CPU and optional memory targets, and tune both HPA scaling directions
independently:

```yaml
operator:
  resources:
    requests:
      cpu: "1"
      memory: 1Gi
    limits:
      cpu: "1"
      memory: 1Gi
  autoscaling:
    enabled: true
    minReplicas: 2
    maxReplicas: 8
    targetCPUUtilizationPercentage: 70
    targetMemoryUtilizationPercentage: 80
    behavior:
      scaleUp:
        stabilizationWindowSeconds: 0
        selectPolicy: Max
        policies:
          - type: Pods
            value: 2
            periodSeconds: 30
      scaleDown:
        stabilizationWindowSeconds: 300
        selectPolicy: Min
        policies:
          - type: Pods
            value: 1
            periodSeconds: 60
```

`targetMemoryUtilizationPercentage` accepts an integer from 1 to 100. Its default
is `null`, which omits memory from the HPA. Enabling this metric requires
`operator.resources.requests.memory`; Helm rejects an enabled memory target
without that request. Without sidecars, the example targets 80% of the requested `1Gi`, or
`819.2Mi` per Pod. Memory limits do not determine this percentage. Resource
metrics cover the Pod, so injected sidecars also need appropriate resource
requests. See [Kubernetes resource metrics](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/#support-for-resource-metrics).

With both metrics enabled, Kubernetes uses the larger CPU or memory replica
recommendation, subject to replica bounds and scaling behavior. Memory pressure
can therefore request more replicas even when CPU utilization is low. The HPA
requires the cluster resource metrics API, usually provided by Metrics Server;
it does not scrape Polyad's application metrics endpoint. See
[multiple HPA metrics](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/#scaling-on-multiple-metrics).

The HPA scales the dense operator or, in Distributed mode, its bootstrap
Deployment. Gateway, executor and telemetry groups retain their separate
[KEDA scaling configuration](../deployment/components.md#scaling-and-structural-bounds).

The chart passes `behavior` directly into the HPA. Stabilization windows accept
0–3600 seconds, including an explicit zero. The window considers recent scaling
recommendations to reduce oscillation. Rate policies separately limit changes
over `periodSeconds` (1–1800).
`Pods` uses an absolute count and `Percent` uses a percentage. `Max` chooses the
largest permitted change, `Min` the smallest, and `Disabled` prevents scaling
in that direction. See [Kubernetes scaling behavior](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/#configurable-scaling-behavior).

Defaults retain a zero-second scale-up window and a 300-second scale-down
window, with Kubernetes' standard rate policies. The example above instead
limits each scale-up to two Pods per 30 seconds and each scale-down to one Pod
per minute. Helm replaces policy arrays when overridden.

The cluster controls the HPA synchronization interval and availability of resource
metrics. Resource requests, replica bounds, memory limits and termination grace are
also available under `operator`; see the [Helm parameters](../../charts/polyad/README.md).

## Worker cadence

```yaml
operator:
  tuning:
    rescanIntervalSeconds: 5
    consumeIntervalSeconds: 1
    metricsIntervalSeconds: 5
    backlogIntervalSeconds: 5
```

| Value | Default; bounds in seconds | Effect and observations |
| --- | --- | --- |
| `rescanIntervalSeconds` | 5; 1–15 | Local and root-managed remote inventory scans, optional state persistence and remote backlog samples. Watch inventory sample ages/freshness and cluster inventory freshness. |
| `consumeIntervalSeconds` | 1; 0.1–5 | Local/remote queue passes, delivering at most one notification per owned shard. Compare inbound backlog with local/worker write pressure before increasing dispatch. |
| `metricsIntervalSeconds` | 5; 1–5 | Cached metrics publication, enabled PostgreSQL/Dragonfly connection samples, component demand and worker heartbeat reports. Watch snapshot timestamps, component/worker freshness and backend freshness. |
| `backlogIntervalSeconds` | 5; 1–5 | Local shared-queue sampling. Watch inbound sample age/freshness; remote queues instead follow the rescan cadence. |

`polyad_operator_interval_seconds{loop="rescan|consume|metrics|backlog"}` exposes
the effective settings on each metrics-serving process (the label takes one of
those four values). `/v1/metrics` also includes `tuning`. These are configured
pauses, not measured loop durations. In split deployments, the telemetry replica
does not report the effective configuration of a different executor process.

Metrics publication and component reporting are independent tasks sharing the
same configured pause. Component HTTP rates use a fixed sixty-second window;
changing publication cadence does not change that window. Optional backend
sampling adds I/O to publication passes, so their duration also affects freshness.

These are pauses **after** a pass completes, not guarantees that a pass starts
on a fixed schedule. Lowering queue delay can improve notification throughput;
lowering rescan delay adds Kubernetes reads even when no work has changed.
Faster metrics publication does not make the underlying inventory fresher;
rescan duration and frequency determine that. Bounds leave room within the
existing telemetry expiry windows, but slow API calls can still make observations
stale. Stale observations retain their existing unavailable status.

Publication, queue and component/worker heartbeat freshness use fifteen-second
windows; inventory and controller signal observations use thirty-second windows.
Remote root reports have a TTL capped at fifteen seconds and by the remaining
inventory lifetime. These deadlines are not administrator tuning knobs. Coordination
renewal and ownership fences also retain their fixed timing.

Set fractional intervals as YAML numbers in a values file, or use
`--set-json operator.tuning.consumeIntervalSeconds=0.25`; Helm's plain `--set`
treats fractional values as strings.

For local runs, use `POLYAD_RESCAN_INTERVAL_SECONDS`,
`POLYAD_CONSUME_INTERVAL_SECONDS`, `POLYAD_METRICS_INTERVAL_SECONDS` and
`POLYAD_BACKLOG_INTERVAL_SECONDS`. Settings are validated once at startup;
invalid values stop startup. Helm changes update the Pod template and roll out
new replicas.

Shard identity, ownership renewal, write fences and mutation ordering remain
fixed. More replicas distribute independent graph families across the 32 logical
shards. A single graph family remains serialized, and increasing replica counts
or polling frequency can worsen Kubernetes API saturation. Use
[queue and write metrics](metrics.md) to distinguish inbound pressure from slow
API writes before changing these controls.

Keep `metrics.graphLabels: false` for aggregate monitoring. Enable it when you
need named hierarchy, shape, resource or local/remote workload-signal series;
JSON and scalar KEDA endpoints do not require it. It does not add CPU/memory,
Cheeger or application-throughput Prometheus series. Graph throughput stabilization
is configured on Graph/PolyGraph policies; changing polling delays does not change
those admission requirements.

Trace batching is independent of polling and scraping. The Python SDK's
`OTEL_BSP_*` settings control batching for [OpenTelemetry traces](tracing.md);
the [tracing reference](../../charts/polyad/values-tracing.reference.yaml) covers
export enablement, sampling and endpoint selection. The chart does not turn a
trace sampling ratio into a workload or autoscaler metric.

## Write admission and validation

`operator.writeQueue` tunes the internal work graph, from reconciliation and
mutation planning through validation and API writes. Polling cadence and metrics
publication have separate settings:

| Setting | Default | Tradeoff |
| --- | --- | --- |
| `plannerParallelism` | 1 | Callbacks per approved mutation batch; callers may lower the ceiling, and parallel transport also needs writer/admission capacity |
| `maxInFlight` | 1 | Concurrent writer slots per adapter; only independent approved planner siblings overlap |
| `maxPending` | 1 | Extra admissions beyond writer slots; larger values retain more potentially stale decisions |
| `validationIntervalSeconds` | 1 | Maximum pause between look-ahead passes; watch notifications wake validation sooner |
| `validationWindowSeconds` | 5 | Maximum age of a reusable receipt, including its read latency; shorter windows reduce unobserved drift and increase API reads |
| `validationBurst` | 8 | Candidates examined per producer pass; lets spare time reach third, fourth and later entries without adding concurrent mutations |
| `validationWorkers` | 1 | Concurrent candidate checks shared by producer and dispatchers; higher values add Kubernetes read pressure |
| `reconciliationWorkers` | 1 | Concurrent local refresh attempts and remote deliveries per cluster; same-family ownership remains ordered |
| `reconciliationCooldownSeconds` | 0 | Optional per-resource/cluster fixed window shared across HA replicas; zero disables, maximum 300 seconds |
| `reconciliationBurst` | 1 | Starts per resource per cooldown window, from 1–128; caps churn independently of worker counts |

The interval cannot exceed the window. Admission allows 0–128 waiters, validation
allows 0.01–60 second intervals/windows and a burst of 1–128. Overflow returns a
retryable conflict with status 429. Identical pending decisions share one entry
before admission is checked. Watches and known writes invalidate relevant receipts
immediately; a fresh receipt never bypasses the ownership check or Kubernetes
resource-version fence. See the [write pipeline](../development/write-pipeline.md)
for contracts, failure recovery and ordering limits, and the typed
[values reference](../../charts/polyad/values-tuning.reference.yaml) for the overlay.

The [parallel example and deployment flow](../development/write-pipeline.md#choosing-concurrency)
explain how to size the stages together and propagate changes to downstream
operators. Effective limits appear as `workGraph` in health and metrics JSON,
and in fresh worker reports collected at the root. They apply to each process's
plans, adapters and cluster deliveries; additional replicas add capacity.
Reconciliation pulse budgets are the exception: replicas share their counters.
Connection negotiation has [separate cooldown and burst controls](../apis/temporary-connections.md#administrator-pulse-limits),
so application proposal traffic can be tuned independently of queued reconciliation.

## KEDA-managed targets

When KEDA owns a Deployment or ReplicaGroup, put the equivalent behavior in its
ScaledObject under `spec.advanced.horizontalPodAutoscalerConfig.behavior`.
The chart's operator HPA settings do not modify separately managed ScaledObjects.
Disable `operator.autoscaling.enabled` before letting KEDA own the operator
Deployment. Use `ha: true` and keep KEDA's `minReplicaCount` at least two to
preserve the HA profile's replica floor.

KEDA's `pollingInterval` and `cooldownPeriod` are separate controls; cooldown
governs returning to zero, while HPA behavior governs scaling between active
replica counts. See the [KEDA ScaledObject specification](https://keda.sh/docs/2.20/reference/scaledobject-spec/#horizontalpodautoscalerconfig)
and the [authenticated workload scaling example](../graphs/replication.md#connect-keda).
