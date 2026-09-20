# Advance capacity planning

<!-- toc:start -->
**Table of contents**

- [Enable the integration](#enable-the-integration)
- [Describe a forecast](#describe-a-forecast)
- [Scheduling demand and placement](#scheduling-demand-and-placement)
- [Backend selection and handoff](#backend-selection-and-handoff)
  - [ProvisioningRequest](#provisioningrequest)
  - [Placeholder Pods](#placeholder-pods)
- [Ownership, cancellation and expiry](#ownership-cancellation-and-expiry)
- [Status and limits](#status-and-limits)
<!-- toc:end -->

[Documentation](../README.md)

Polyad can signal upcoming demand to a node autoscaler while upstream work is
still running. An optional `capacity` policy tells each graph boundary how far
to look ahead, how many future Pods to request, and how long to retain that
request. Kubernetes and the configured node autoscaler still place Pods and
provision machines.

For example, a preparation Job can run while Polyad requests machines for the
next processing stage. The processing Jobs remain behind their dependencies
and gates until both their admission conditions and capacity checks pass.

[Soul searching load profiles](load-profiles.md) can select forecast depth and
Pod budgets from sustained application demand. Enable `trigger: Demand` for
preparation before a throughput shortfall. This adjusts planning within fixed
ceilings; existing KEDA/HPA replica scaling remains separate.

## Enable the integration

Enable the additional namespaced permissions and placeholder PriorityClass:

```sh
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
    --set capacity.enabled=true
```

Use the image and cache settings from your existing installation. This option
does not install or configure a node autoscaler. For ProvisioningRequest, install
the upstream `autoscaling.x-k8s.io/v1` CRD and enable a compatible autoscaler
controller and provisioning class. Merely installing the CRD does not establish
that the controller is processing requests. See the
[Cluster Autoscaler setup and provider requirements](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#how-can-i-use-provisioningrequest-to-run-batch-workloads).

`capacity.provisioningClassName` defaults to
`best-effort-atomic-scale-up.autoscaling.x-k8s.io`. A graph can override it with a
provider-specific provisioning class and parameters. The check-only
`check-capacity.autoscaling.x-k8s.io` class is rejected because it does not request
scale-out.

## Describe a forecast

Add this policy to a Graph or PolyGraph's spec:

```yaml
capacity:
  backend: Auto
  lookaheadStages: 1
  maxPods: 8
  timeoutSeconds: 900
placement:
  nodeSelector:
    pool: processing
```

| Field | Meaning |
| --- | --- |
| `backend` | `Auto`, `ProvisioningRequest` or `Placeholders`; default `Auto`. |
| `lookaheadStages` | Missing dependency layers to forecast, from 1 to 32. The default 1 includes nodes whose upstream execution resources already exist, even if those resources are not ready or complete. |
| `maxPods` | Maximum unconsumed forecast Pods at this boundary, from 1 to 1,024; also capped by Helm's `capacity.maxPods`. |
| `timeoutSeconds` | Time from recording the plan until expiry, including dependency and gate waits. Default 600, maximum 86,400. |
| `provisioningClassName` | Optional override for the operator's provisioning class. |
| `parameters` | Provider-specific string parameters. The default request includes `ValidUntilSeconds` matching the timeout. |
| `retryToken` | Change this string to retry failed or expired requests. |

The public Python type is `polyad.graph.CapacityPlan`. Its attrs fields generate
the CRD properties, and the composition API exposes the same policy through
`GraphSpec.capacity`. Rewrites can replace it with the rest of the topology.

Graph instances inherit their parent's policy unless their definition provides
one. Each execution boundary owns its own forecasts. A PolyGraph propagates
policy and combines descendant status; it does not reserve all uninstantiated
descendants in advance. Repeated activations have separate execution identities, so
future unsubmitted requests do not multiply demand.

Lookahead measures dependency layers, not estimated time to completion. Future
branches can reserve capacity concurrently even if some eventually run
sequentially or never pass their gates. Start with one layer and increase it
only when the extra lead time is useful. Replica groups are never silently
truncated to fit a budget: a single group larger than the cap is invalid;
additional groups wait for budget to become available.

## Scheduling demand and placement

Polyad compiles the future Job, Deployment or StatefulSet, applies inherited placement and
storage rules, and submits a **dry-run Pod admission**. This runs admission
validation and defaulting without creating an application Pod. The forecast
therefore includes admitted resource requests, sequential init-container peaks,
native sidecars and RuntimeClass overhead. At least one nonzero resource request
is required. Daemon forecasts include their configured replica count.

ProvisioningRequest receives a native PodTemplate with the admitted scheduling
spec. Its selectors, affinity, tolerations and volume requirements describe the
same destination as the actual workload. Node-group labels must correspond to
capacity the autoscaler knows how to provision; a toleration alone does not
select a group or guarantee capacity.

Dry-run checks and stored templates do not freeze admission webhooks or cluster
configuration. A later admission change can alter the eventual Pod. Polyad does
not infer workload resource requirements from graph breadth, historical usage
or aggregate CPU utilization.

## Backend selection and handoff

### ProvisioningRequest

`Auto` probes the namespaced v1 API. If available, Polyad uses it and persists
that backend choice for the request revision. Each execution node creates a
PodTemplate and one ProvisioningRequest containing a PodSet for its replicas.
These resources have deterministic names, graph ownership and audit annotations.

Creation acknowledgement and `Accepted` do not open admission. Polyad requires
a freshly read `Provisioned=True` condition for the request's current generation.
`Failed`, `BookingExpired` and `CapacityRevoked` take precedence over a previous
success. These outcomes never trigger an automatic switch to placeholders.

Before creating the actual Job, Deployment or StatefulSet, Polyad adds the autoscaler's
consumption and provisioning-class annotations to the resource and its Pod
template. The request remains until the corresponding Pods are scheduled,
execution completes, or the plan timeout is reached. Already admitted work
continues under its normal graph lifecycle.

### Placeholder Pods

`Auto` falls back only when the ProvisioningRequest v1 API returns not found.
`Placeholders` selects this mechanism explicitly. Forbidden access, timeouts,
rejected manifests and provider failures remain visible errors.

Polyad creates inert pause Pods with the supported scheduling requirements and
resource requests of the future workload. They have dedicated capacity labels,
no application labels, no mounted application storage, no service-account token,
and a deny-all NetworkPolicy. They do not execute workload commands or receive
the workload's Service selectors. The cluster's CNI must enforce NetworkPolicies
for the deny-all policy to take effect.

The chart's PriorityClass has priority -5 and cannot preempt other Pods. Actual
workload priority must be higher. If your autoscaler uses a different expendable
Pod cutoff, configure the priority accordingly; Pods below the cutoff do not
trigger Cluster Autoscaler scale-out. An existing PriorityClass can be selected
with `capacity.priorityClass.create=false` and `capacity.priorityClass.name`;
set `capacity.priorityClass.value` to its actual value. See
[Kubernetes overprovisioning](https://kubernetes.io/docs/tasks/administer-cluster/node-overprovisioning/)
and [Cluster Autoscaler priority handling](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#how-does-cluster-autoscaler-work-with-pod-priority-and-preemption).

All placeholders must be observed Ready before handoff. Once dependencies and
gates permit admission, Polyad persists the release decision, deletes the
placeholders, waits for their disappearance, then creates the real workload.
Other workloads can consume capacity during the scheduling gap between
placeholder deletion and admission of the real workload.

Placeholder forecasts reject PVCs and ephemeral claim templates, Pod affinity
or anti-affinity, topology spread, dynamic resource claims, host ports and
custom schedulers. Reproducing these with anonymous pause Pods could advertise
incorrect placement or attach application storage. Use a supporting
ProvisioningRequest backend for those workloads. Pods with `nodeName` or
`schedulingGates` are rejected for both backends.

StatefulSet execution with `volumeClaimTemplates` currently rejects advance
capacity planning because each ordinal requires a distinct claim. Omit
`spec.capacity` on that execution boundary to use ordinary Kubernetes scheduling.
StatefulSets with explicit Pod volumes remain supported. See
[workload storage](../workloads/workload-storage.md#replication-rules-and-capacity).

## Ownership, cancellation and expiry

Capacity mutations use the existing graph-family lease and serialized API
adapter. Status writes carry resource versions; deletes carry UID and resource
version preconditions. A new replica rereads the durable plan and backing
resources before acting. A lost create response cannot create a second request
for the same revision.

Graph generation, compiled workload, policy and operator capacity settings
contribute to the revision. Old helpers must finish deletion before a changed
forecast can create replacements. Suspension, deletion and invalid graph intent
cancel forecasts and release helpers. Request deletion precedes PodTemplate
cleanup; placeholder network guards remain until the Pods are gone. Graph
finalizers retain their existing cleanup responsibility.

Failed and expired plans remain terminal across rescans. Change `retryToken`
or the relevant graph intent to retry. Expiry also releases capacity held behind
a gate that never opens. Disable the chart integration only after draining
existing plans, since removing its RBAC permissions prevents helper cleanup.

## Status and limits

`status.capacity` is a typed observation tree with `observedGeneration` and
per-node records. Records contain the selected backend, revision, requested
resources and Pod count, ready Pod count, start time, elapsed seconds, request
name and an explanatory message.

Phases are `Planned`, `Provisioning`, `Ready`, `Releasing`, `Consumed`, `Failed`,
`Expired` and `Cancelled`. Capacity readiness is independent of graph readiness:
a graph can have ready capacity while a dependency or delay gate still blocks
its workload.

Root `status.metrics.rollup` aggregates `capacityPlans`, `capacityRequestedPods`,
`capacityReadyPods` and `capacityFailedPlans` from current descendant observations.
Existing observation-completeness fields identify stale or missing child status.
Helper resources contribute to inventory counts, never to workload completion,
node counts or slot consumption.

Capacity availability remains subject to autoscaler limits, cloud supply,
competing workloads and configuration propagation. This integration does not
resize cloud node groups directly, install Karpenter, or guarantee gang scheduling.

See the [advance-capacity example](../../examples/capacity.yaml).
