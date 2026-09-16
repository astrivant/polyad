# Operator performance and autoscaling

Operator tuning lives under `operator` in the chart values. Scaling controls
change how many replicas run; polling controls change how often each replica
checks for work and publishes observations. Both affect response time and the
load placed on Kubernetes and the shared cache.

## Autoscaling response

Configure CPU and optional memory targets, and tune both HPA scaling directions
independently:

```yaml
operator:
  resources:
    requests:
      cpu: 100m
      memory: 256Mi
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
without that request. The example targets 80% of the requested `256Mi`, or
`204.8Mi` per Pod. Memory limits do not determine this percentage. Resource
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
[KEDA scaling configuration](components.md#scaling-and-structural-bounds).

The chart passes `behavior` directly into the HPA. Stabilization windows accept
0–3600 seconds, including an explicit zero. The window considers recent scaling
recommendations to reduce oscillation; it is not a fixed sleep before every
change. Rate policies separately limit changes over `periodSeconds` (1–1800).
`Pods` uses an absolute count and `Percent` uses a percentage. `Max` chooses the
largest permitted change, `Min` the smallest, and `Disabled` prevents scaling
in that direction. See [Kubernetes scaling behavior](https://kubernetes.io/docs/concepts/workloads/autoscaling/horizontal-pod-autoscale/#configurable-scaling-behavior).

Defaults retain a zero-second scale-up window and a 300-second scale-down
window, with Kubernetes' standard rate policies. The example above instead
limits each scale-up to two Pods per 30 seconds and each scale-down to one Pod
per minute. Helm replaces policy arrays when overridden.

The cluster controls the HPA synchronization interval and availability of resource
metrics. Resource requests, replica bounds, memory limits and termination grace are
also available under `operator`; see the [Helm parameters](../charts/polyad/README.md).

## Worker cadence

```yaml
operator:
  tuning:
    rescanIntervalSeconds: 5
    consumeIntervalSeconds: 1
    metricsIntervalSeconds: 5
    backlogIntervalSeconds: 5
```

| Value | Bounds | Effect |
| --- | --- | --- |
| `rescanIntervalSeconds` | 1–15 | Pause after a namespace scan; controls missed-event recovery and inventory refresh |
| `consumeIntervalSeconds` | 0.1–5 | Pause after a queue pass, which delivers at most one notification per owned shard |
| `metricsIntervalSeconds` | 1–5 | Pause between publishing cached metrics snapshots |
| `backlogIntervalSeconds` | 1–5 | Pause between sampling shared queue sizes |

These are pauses **after** a pass completes, not guarantees that a pass starts
on a fixed schedule. Lowering queue delay can improve notification throughput;
lowering rescan delay adds Kubernetes reads even when no work has changed.
Faster metrics publication does not make the underlying inventory fresher;
rescan duration and frequency determine that. Bounds leave room within the
existing telemetry expiry windows, but slow API calls can still make observations
stale. Stale observations retain their existing unavailable status.

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

## KEDA-managed targets

When KEDA owns a Deployment or ReplicaGroup, put the equivalent behavior in its
ScaledObject under `spec.advanced.horizontalPodAutoscalerConfig.behavior`.
The chart's operator HPA settings do not modify separately managed ScaledObjects.
Disable `operator.autoscaling.enabled` before letting KEDA own the operator
Deployment, and keep at least one operator replica running.

KEDA's `pollingInterval` and `cooldownPeriod` are separate controls; cooldown
governs returning to zero, while HPA behavior governs scaling between active
replica counts. See the [KEDA ScaledObject specification](https://keda.sh/docs/2.20/reference/scaledobject-spec/#horizontalpodautoscalerconfig)
and the [authenticated workload scaling example](replication.md#connect-keda).
