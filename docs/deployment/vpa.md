# Vertical Pod Autoscaler compatibility

Polyad can own a native `autoscaling.k8s.io/v1` `VerticalPodAutoscaler` beside a
graph workload. The integration is disabled by default and does not install the
VPA controller. Enable it with `verticalPodAutoscaling.enabled=true` only after
installing VPA in the cluster.

Define the VPA as a Polyad `Resource` and use `${nodes.NAME.name}` for its
`targetRef.name`. At admission Polyad validates that the target is an `apps/v1`
Deployment, StatefulSet or DaemonSet in the same graph. It also parses every
container policy's CPU and memory `minAllowed` and `maxAllowed`, rejects invalid
or inverted quantities, and prevents multiple graph-owned VPAs from targeting
the same controller. The chart compatibility switch is demonstrated in
[`values-vpa.reference.yaml`](../../charts/polyad/references/values-vpa.reference.yaml).

The VPA itself is a resource definition referenced by a graph node:

```yaml
apiVersion: polyad.astrivant.com/v1alpha1
kind: Resource
metadata:
  name: worker-vpa
spec:
  manifest:
    apiVersion: autoscaling.k8s.io/v1
    kind: VerticalPodAutoscaler
    spec:
      targetRef:
        apiVersion: apps/v1
        kind: Deployment
        name: ${nodes.worker.name}
      updatePolicy:
        updateMode: InPlaceOrRecreate
      resourcePolicy:
        containerPolicies:
          - containerName: worker
            controlledResources: [cpu, memory]
            controlledValues: RequestsAndLimits
            minAllowed:
              cpu: 100m
              memory: 128Mi
            maxAllowed:
              cpu: "2"
              memory: 4Gi
```

VPA owns vertical mutations. Polyad continues to own implementation selection,
graph topology, replica strategy and traffic. Do not configure an HPA and VPA to
control the same resource signal. `InPlace` and `InPlaceOrRecreate` also depend
on the installed VPA and Kubernetes versions; the latter may still evict a Pod
when an in-place resize cannot be completed.

## Application contract

For each targeted regular container, Polyad adds these policy variables to its
generated Pod template:

| Variable | Unit |
| --- | --- |
| `POLYAD_VPA_UPDATE_MODE` | VPA update mode |
| `POLYAD_VPA_MIN_CPU_MILLICORES`, `POLYAD_VPA_MAX_CPU_MILLICORES` | millicores |
| `POLYAD_VPA_MIN_MEMORY_BYTES`, `POLYAD_VPA_MAX_MEMORY_BYTES` | bytes |

An explicit container policy overrides the `containerName: "*"` policy. A policy
with `mode: Off` exposes only the overall update mode. These values describe the
policy interval and remain constant for that Pod incarnation.

The existing `POLYAD_CPU_*` and `POLYAD_MEMORY_*` variables are Kubernetes
Downward API startup snapshots of that container's assigned requests and limits.
Applications using in-place resize should read the SDK's live cgroup sample:

```python
from polyad_sdk import WorkloadContext, container_metrics

context = WorkloadContext.from_environment()
workers = context.vpa.clamp("cpu", 750)  # application decision inside policy bounds
sample = container_metrics()
print(context.vpa, context.resources, sample.memory_available_bytes)
```

`container_metrics()` exposes cumulative CPU usage, current memory usage, and
the live cgroup CPU/memory limits. This requires cgroup v2; unavailable or
unbounded values are returned as `None`. It needs no Kubernetes API credential.
