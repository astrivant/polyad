# Adapting services to Kubernetes conditions

<!-- toc:start -->
**Table of contents**

- [Choose the observation source](#choose-the-observation-source)
- [Map conditions to adaptations](#map-conditions-to-adaptations)
- [Wait for node capacity](#wait-for-node-capacity)
- [Handle intermittent connectivity](#handle-intermittent-connectivity)
- [Mutate local processing safely](#mutate-local-processing-safely)
- [Implement a strategy component](#implement-a-strategy-component)
- [Measure recovery](#measure-recovery)
<!-- toc:end -->

Service Symbiosis lets application code change how it accepts, routes and
processes work while Kubernetes changes the resources around it. This guide maps
common conditions to SDK strategies and concrete microservice responses.

## Choose the observation source

A Kubernetes **Event** is a diagnostic record, such as `FailedScheduling`.
Pod `Ready=False`, Node `Ready=Unknown`, `OOMKilled` and `CrashLoopBackOff` are
conditions or container states, not interchangeable Event types. Network timeouts
can occur while infrastructure status looks healthy.

| Source | Information | Application access |
| --- | --- | --- |
| Polyad observations | Permitted membership, execution identities, connection receipts, graph resources and decisions | SDK snapshots, deltas, subscriptions and `service.view` |
| Kubernetes diagnostics and status | Scheduling failures, readiness, eviction and restart reasons | Authorized monitoring or an explicitly provisioned status adapter |
| Application measurements | Completion rate, queue age, credits, RPC failures, memory and compatibility | Application instrumentation and bounded health checks |

The SDK does not broadcast raw Kubernetes Events or automatically collect every
Pod condition, timeout or cgroup measurement. Use diagnostics to trigger current
status checks. Keep identities and timestamps with observations. Projected
[workload environment](workload-environment.md) and
[Pod context](../deployment/pod-context.md) identify placement and declared budgets;
they are not a live readiness or usage feed.

## Map conditions to adaptations

| Condition | SDK component or interface | Application response | Recovery condition |
| --- | --- | --- | --- |
| Pending replicas; `FailedScheduling` reports insufficient capacity | `ResourceStrategy` for available graph metrics; custom `AdaptationStrategy` for demand; `ReachabilityStrategy` for queue bounds | Limit admission to ready capacity, pause pulls, reduce optional fan-out or route work to ready peers | New consumers pass readiness, compatibility and capacity checks; ramp admission gradually |
| Pod exists but is not Ready; image or storage preparation is delayed | `PeerAvailabilityStrategy` with an application `usable` callback | Exclude that execution from new assignments; retain bounded work | That execution becomes usable; desired replicas alone are insufficient |
| Timeouts, resets, DNS or mesh failures | Custom `ConstraintStrategy` for transport health; `TopologyStrategy` for membership changes | Open a per-peer circuit, cap retries and outstanding work, reroute eligible new jobs, preserve uncertain receipts | Bounded probes succeed over a recovery window |
| Node readiness lost, disappearing execution or failed health checks | Peer/freshness guards and application ownership checks | Withdraw the destination; resolve or fence accepted work before reassignment | A healthy execution and recovered ownership state |
| Rollout, scale-down, Pod deletion or eviction | `TopologyStrategy`, peer guard and application supervisor | Stop assignments, drain or checkpoint accepted work within shutdown grace | Replacement is ready and ownership transfer is complete |
| Memory pressure or prior `OOMKilled` termination | `ContainerBudgetStrategy` with measured usage; custom pressure strategy | Reduce batches/concurrency, pause admission, propose a low-memory profile if overlap fits | Sustained headroom including replacement overlap |
| CPU throttling and growing queue age | Local pressure measurements; `ResourceStrategy` where its metric exists | Reduce competing work, improve batching, pace callers or propose an approved profile | Useful completion and queue age recover; more local workers alone do not add CPU |
| Slow database or connection-pool exhaustion | Custom `ConstraintStrategy`, consumer credits and bounded retries | Reduce downstream concurrency and propagate backpressure | Measured downstream capacity and queue age recover |
| Startup failures or `CrashLoopBackOff` | Peer guard; `ProcessPlan` readiness/rollback for local children | Retain traffic on usable instances and keep the committed profile when a candidate fails | Corrected configuration or implementation passes readiness |
| Secret rotation | Application credential watcher and custom strategy | Reload credentials or roll affected clients/workers with bounded overlap | New authenticated sessions succeed before old sessions retire |
| Temporary connection expires or is revoked | `ConnectionPermissionStrategy`, `TopologyStrategy` | Stop new assignments and settle work within the grant lifetime | A newly admitted, ready connection |
| Lost Polyad stream or expired context | `FreshnessStrategy` and SDK replay/recovery | Pause actions needing unverifiable context, retain accepted work, rebuild a baseline | Current authorized context and successful application checks |
| Soul searching proposal is pending or blocked by rules | `DecisionStrategy`, `DecisionGuardStrategy` | Wait for the required phase; retain valid routes or apply permitted backpressure | Applied decision, actual readiness and all other guards |

The [adaptation catalog](adaptation-strategies.md) defines these components.
Missing fields remain unknown; they do not imply zero load or ready capacity.
Each independent blocker must recover before its protected action proceeds.

## Wait for node capacity

Treat a two-to-three-minute provisioning delay as a measured scenario, not a
Kubernetes timing guarantee. Include node creation, image pulls, storage,
application startup and readiness in time until usable capacity.

Suppose arrivals are 1,200 records/s, ready consumers complete 1,000 records/s
and 6,000 buffer slots remain. Under constant rates, the buffer fills in
`6,000 / (1,200 - 1,000) = 30` seconds. A forecast of 180 seconds until new capacity
is ready cannot justify accepting at the higher rate throughout the wait.

Reduce admission, pause upstream pulls, use an already permitted compatible peer,
or apply an explicitly allowed reduction in work. Preserve identities and
deadlines; bound bytes as well as counts. A promised node is preparation
information, not usable capacity.

The [queue model](reachability.md#model-a-producer-and-its-consumers) can include a
readiness clock. The [state-variable study](../../studies/reachability-state/README.md)
shows why omitting it can produce optimistic decisions. To study a 180-second
wait, change `warmup_max`, clock observations and the horizon together before
preparation, then rerun within numerical budgets. KEDA/HPA and the node autoscaler
retain infrastructure control; service adaptation bridges the wait. Polyad's
[capacity preparation](../graphs/load-profiles.md) can act earlier when configured
demand signals provide enough notice.

## Handle intermittent connectivity

Track transport health per peer execution. `TopologyStrategy` will not run for
every HTTP timeout when membership stays unchanged. An application timer or
request scheduler updates health and rechecks guards before new assignments.

A custom `ConstraintStrategy.evaluate(current)` can inspect a bounded failure
window, probe age and an open-circuit deadline. Require it alongside permission
and available capacity. Cap retries with jitter, a work deadline and the same
work identity. A timeout after submission leaves acceptance uncertain: resolve
the receipt or fence ownership before offering that job elsewhere.

Restore a small amount of traffic after successful probes, observe completion,
then ramp up. Separate failure/recovery thresholds and cooldowns prevent constant
reversals. With Polyad-managed Istio percentages, retain the declared Service
address and report pressure through the authorized reporting path.

## Mutate local processing safely

Define approved concurrency, batch-size or worker-implementation profiles before
startup. Combine proposals with budget and health guards and
[`ProcessSupervisor`](sdk-runtime.md#construct-workers-and-approved-plans).

`propose(profile)` records intent. The application's supervisor thread calls
`reconcile()` to check guards, start children, await readiness, commit routing
and drain retired workers. Account for old/new overlap. Under memory pressure,
admission may need to slow and accepted work drain before a replacement fits.
Never sleep through provisioning or solve an HJ grid in a strategy callback.

Internal workers share the current container's CPU and memory. Pod scaling goes
through its controllers and GraphRules. A local profile cannot change a quota
or authorize a forbidden connection.

## Implement a strategy component

This example extends the real SDK ABC. `app` supplies application-owned fresh
pressure measurements and bounded intent updates. `supervisor` is a configured
`ProcessSupervisor` with an approved `low-memory` plan.

```python
from dataclasses import dataclass
from typing import Protocol

from polyad_sdk import AdaptationStrategy, Change, Environment, ProcessSupervisor


@dataclass(frozen=True)
class Pressure:
    expired: bool
    seconds_until_full: float
    seconds_until_ready: float
    margin_seconds: float
    sustainable_ready_rate: float  # Jobs per second.
    memory_high: bool


class ApplicationPressure(Protocol):
    def current_pressure(self) -> Pressure | None: ...

    def pause_new_assignments(self) -> None: ...

    def set_admission_limit(self, jobs_per_second: float) -> None: ...


class CapacityWaitStrategy(AdaptationStrategy):
    def __init__(self, app: ApplicationPressure, supervisor: ProcessSupervisor) -> None:
        self.app = app
        self.supervisor = supervisor

    def adapt(self, change: Change, current: Environment) -> None:
        pressure = self.app.current_pressure()
        if not current.available or pressure is None or pressure.expired:
            self.app.pause_new_assignments()
            return
        if pressure.seconds_until_full <= pressure.seconds_until_ready + pressure.margin_seconds:
            self.app.set_admission_limit(pressure.sustainable_ready_rate)
        if pressure.memory_high:
            self.supervisor.propose("low-memory")
```

`Pressure` and `ApplicationPressure` are application types defined by this
example. The protocol lists the methods the strategy needs; your application
implements them. `None` means no pressure measurement is available, and `expired`
indicates that the application's measurement has exceeded its age limit.

Supply it in `strategies=[...]` before starting `AdaptiveService`. The application
timer/admission loop must also reassess local pressure between SDK changes.
Define recovery after sustained headroom; this example shows the pressure
response. Plan guards and budgets still govern execution. Implement
`ConstraintStrategy` for independent conditions and
[combine their assessments](adaptation-strategies.md#compose-independent-constraints)
so one recovered condition cannot clear another's blocker.

## Measure recovery

Record signal source, execution identity, revision, action, assessment reason
and resulting completion. Measure detection time, time to usable capacity, queue
age, rejected work, retries, draining and profile reversals with the SDK's
[traces and metrics](sdk-runtime.md#trace-operations-and-collect-metrics).

Exercise delayed readiness, lost replies, stale observations and resource
pressure separately before combining them. The
[rerouting study](../../studies/reachability-routing/README.md) compares actual
processes with identical budgets; the [load study](../../studies/load/README.md)
measures the Kubernetes operator.
