# Adaptation strategies for application constraints

<!-- toc:start -->
**Table of contents**

- [Start with a producer and consumer](#start-with-a-producer-and-consumer)
- [Choose an application adaptation](#choose-an-application-adaptation)
- [Choose an SDK strategy](#choose-an-sdk-strategy)
  - [Observation and callback strategies](#observation-and-callback-strategies)
- [Choose checks for common changes](#choose-checks-for-common-changes)
- [Wait for permission to use a new connection](#wait-for-permission-to-use-a-new-connection)
- [Compose independent constraints](#compose-independent-constraints)
- [Default local capacity from projected environment](#default-local-capacity-from-projected-environment)
- [Propose a bounded profile change](#propose-a-bounded-profile-change)
- [Write a strategy](#write-a-strategy)
- [Delivery, recovery and admission](#delivery-recovery-and-admission)
<!-- toc:end -->

An **adaptation strategy** is a component that helps your service respond when
the services around it or its available resources change. For example, a producer
can stop sending to a consumer that is shutting down, use a newly available
consumer, or reduce its own concurrency when memory is scarce. You define the
response; the SDK delivers changes and supplies reusable checks.

Strategies implement the `polyad_sdk.AdaptationStrategy` ABC and are supplied to
an [`AdaptiveService`](../../pkg/polyad-sdk/README.md#subclass-contract). They run
inside its existing observation loop. Your application code sends the jobs,
checks consumer health, reserves capacity and manages worker processes.

Implementations live in `polyad_sdk.symbiosis.strategies`: `base` defines the ABCs,
`topology` handles freshness, peers and permission, `capacity` handles budgets and
profile thresholds, `decisions` handles operator phases, and `callbacks` adapts
application callbacks. Import public classes from the strategy package or the
SDK root. See the [complete package map](../../pkg/polyad-sdk/README.md#package-layout).

`polyad_sdk.symbiosis.reachability.ReachabilityStrategy` is a `ConstraintStrategy`
for a modeled queue contract. It checks whether an approved routing split can
keep queues bounded and meet its terminal target, using a current artifact and
application measurements. See [reachability guards](reachability.md#use-a-reachability-guard)
for interaction models, assumptions and optional numerical studies.

## Start with a producer and consumer

Suppose your producer sends jobs to a consumer over HTTP. The producer already
needs retries, a concurrency limit and a way to check that the consumer is ready.
With Polyad, it also learns when the set of permitted consumers changes, then
updates where it sends **new** jobs. Jobs already accepted still need to finish
or be handed off safely.

The SDK exposes the information needed for that response:

| Term | Meaning for your application |
| --- | --- |
| **Topology** | The graph's nodes and connections: which services are connected and which direction work can flow. |
| **Peer** or **neighbor** | Another node connected to this service. An outgoing peer is a possible destination for its work. |
| **Snapshot** | A read-only copy of what the SDK knows about the service's surroundings when it builds a view. Inputs may have different observation times; some reports may still be missing. |
| **View** (`service.view`) | A snapshot built from the SDK's latest cached observations, with freshness and connection expiry checked when you read it. |
| **Baseline** | The initial snapshot, used to initialize application state before comparing later changes. Metrics can still be unknown. |
| **Delta** | What changed since the previous snapshot, with the old and new values. For example, a consumer was added. |
| **Guard** | A check that must pass before a particular action, such as sending to a consumer or starting a worker. |
| **Admission** | Your application's decision to accept or assign new work after its checks pass. |
| **Supervisor** | Code that manages worker processes: starting them, waiting for readiness and stopping them after accepted work finishes. |

The normal sequence is: receive a change, update the destinations your service
could use, check the relevant conditions, then send new work through your usual
HTTP or messaging code. A strategy can handle one of these responsibilities.
For example, `TopologyStrategy` refreshes your destination list through a
callback, while `PeerAvailabilityStrategy` checks that enough destinations are
ready according to your application's health and compatibility checks.

A snapshot gives your code information for choosing its next checks and preparing
a response. For example, seeing a new consumer lets a producer consider it for
future jobs. Sending those jobs still requires a permitted connection, a ready
and compatible consumer, and available capacity. The snapshot itself grants no
additional authority to communicate or change the graph.

Read `service.view` immediately before acting: a saved `change.after` describes
what the SDK knew when that change was built. Its contents stay fixed while
connections, permissions and capacity can change. Reading `service.view`
reevaluates cached information; the observation loop and `refresh()` fetch newer
topology data. See [snapshots and permission to act](../../pkg/polyad-sdk/README.md#snapshots-and-permission-to-act)
for the available information and the checks needed for each action.

## Choose an application adaptation

An **adaptation** is a change to how the application accepts, routes or processes
work in response to its observed environment. Engineers define that behavior;
SDK strategies deliver the relevant information, check conditions and suggest
changes. Application code carries out worker and connection lifecycle changes.
Each service selects the adaptations relevant to its work. The
[Kubernetes adaptation guide](kubernetes-adaptation.md) maps scheduling delays,
intermittent connectivity, rollouts and resource pressure to these components,
with implementation and recovery examples.

Use the SDK's [telemetry and subprocess plans](sdk-runtime.md) to implement those
local lifecycle changes: strategies propose approved profiles, a supervisor
checks current constraints and coordinates workers, and shared traces and metrics
record the result.

| Adaptation | What engineers implement | When to use it | SDK building blocks |
| --- | --- | --- | --- |
| Admission and backpressure | Limit new accepted work, reduce queue pulls or return a bounded retry hint. Backpressure communicates that the receiver cannot safely accept more work yet. | A consumer is full, downstream capacity disappears, or a replacement temporarily consumes spare capacity. | `FreshnessStrategy`, `PeerAvailabilityStrategy`, resource budget guards and application queue measurements. |
| Neighbor routing and work distribution | Refresh eligible destinations and distribute future assignments among compatible, ready consumers. Retain ownership records for work already accepted. | Replicas appear, drain or change capability; an added edge exposes another usable processing path. | `TopologyStrategy` for changes; `PeerAvailabilityStrategy` for the required usable destinations. |
| Temporary connection lifecycle | Request permission to communicate with another service, wait for approval and readiness, then stop sending new work when permission ends. | A producer wants to send jobs to a newly discovered consumer. | `TopologyStrategy`, `ConnectionPermissionStrategy`, and `AdaptiveService.connect()` / `respond()`. See [the connection walkthrough](#wait-for-permission-to-use-a-new-connection). |
| Concurrency, batch size and pacing | Change in-flight request limits, batch sizes or the rate of queue pulls within application-defined bounds. | Sustained pressure calls for smaller memory footprints, less downstream fan-out or more efficient batches without replacing the worker implementation. | `ResourceStrategy` for graph resource changes; `CallbackStrategy` for selected demand observations; custom local measurements and admission rules. |
| Local worker scaling | Add or retire children within the existing container's budget, keeping accepted work accounted for until completion or checkpoint. | A service owns subprocesses and can use additional local capacity, or should return to a smaller idle footprint. | `ThresholdStrategy` when an observed resource metric selects the profile; `ContainerBudgetStrategy` for overlap capacity; a custom strategy for local queue demand. |
| Rolling worker or capability replacement | Start an application-defined alternative profile, await readiness, direct new work to it, then drain the old workers. Draining means finishing, checkpointing or handing off accepted work before stopping. | A service already supports several implementations or capabilities, such as a low-memory worker and a higher-throughput batch worker. | A profile proposal from `ThresholdStrategy` or a custom `AdaptationStrategy`, budget guards, and the application's worker supervisor. See [the process-tree example](local-soul-searching.md#each-service-changes-its-processing-tree). |
| Coordination with operator decisions | Hold a dependent handoff until the required operator phase is observed, then recheck the resulting topology and application readiness. | A local action relies on Soul searching first applying a graph layout, traffic split or capacity preparation. | `DecisionStrategy` interprets decisions; `DecisionGuardStrategy` assesses an explicitly accepted phase. |
| Loss of observation and recovery | Pause new assignments that depend on an unavailable view, preserve accepted work, recover the subscription and rebuild intent from a fresh baseline. | The event stream disconnects, observations expire, the service drains, or a graph identity changes. | `FreshnessStrategy`, baseline delivery to callback strategies, and the [SDK recovery contract](../../pkg/polyad-sdk/README.md#hooks-recovery-and-explicit-actions). |
| Observation before enabling changes | Record which changes arrive and how often, then use that evidence to select policies and tune thresholds. | Integrating a service for the first time or diagnosing repeated adaptation. | `ObserveStrategy`; application metrics for outcomes, latency and rejected admissions. |

The [local work-sharing example](local-soul-searching.md#work-sharing-through-sdk-strategies)
implements neighbor routing with `WorkSharingStrategy(TopologyStrategy)`.
The strategy updates eligible destinations from SDK topology deltas;
`PeerAvailabilityStrategy` checks connection readiness and advertised capacity
before application code delegates real queued jobs. The receiver rechecks its
shared budget, and the source retains job ownership until a verified result
returns. The demo compares this adaptation against a fixed topology under the
same worker limits.

A **worker profile** is an application-defined set of choices, such as child
count, implementation, batch size and concurrency. The SDK passes profile names;
the application defines what those names do. Every profile must preserve the
service's work contract. A producer that changes destinations still needs stable
work identities; a consumer that changes implementations still owes completion
for accepted work. See [producer and consumer responsibilities](adaptive-microservices.md#producers-and-consumers).

Graph-level replica counts, edge layouts and Istio traffic percentages remain
under their configured operator and scaling controllers. Application strategies
adjust the service's own behavior and can submit authorized requests through the
SDK. Local subprocess growth consumes the container's existing allocation;
Kubernetes replica growth allocates additional workload instances.

## Choose an SDK strategy

The supplied strategies have three roles:

- **Observation and callback strategies** select changes and invoke application
  code. A selector defines when a callback runs; the callback defines the response.
- **Constraint strategies**, or **guards**, report whether a particular action
  passes a condition, such as having enough memory or permission to send work.
  They return results; your application checks them before taking the action.
- **`ThresholdStrategy`** proposes a named profile from two thresholds. The
  supervisor decides whether and when to apply it.

All concrete strategies below implement `AdaptationStrategy` and can be passed
through `AdaptiveService(strategies=...)`. See [composition](#compose-independent-constraints)
for construction requirements and execution order.

### Observation and callback strategies

Callbacks receive `(change, current)`. `change` contains the baseline or deltas
that triggered delivery; `current` is the freshly evaluated environment to use
for an action. A delta reports an added, removed or changed field, retaining its
before and after values. Selectors deliver initial baselines and availability
changes as well as matching deltas, so a component can initialize and withdraw
stale intent.

| Strategy | Definition and selection | When to choose it |
| --- | --- | --- |
| `ObserveStrategy(logger=None)` | Logs baseline availability, candidate count and delta paths through the application's logger. It leaves behavior unchanged and omits payload values. | Establish which signals a service receives, correlate changes with application behavior or add observation beside an active policy. |
| `CallbackStrategy(callback, paths=())` | Adapts an existing function into a strategy. Dot-separated `paths` select delta families; an empty sequence selects every delivered change. | Reuse an existing component or select a narrower signal, such as `decision.demandValue`, for an application-specific demand policy. |
| `TopologyStrategy(callback)` | A callback selector for `topology` and `connections`. | Update destination eligibility, connection intent or an allowed producer set after additions, removals, scale changes and consent updates. |
| `ResourceStrategy(callback)` | A callback selector for `resources`, the containing graph's observed resource metrics. | Implement capacity logic that needs several metrics, multiple profiles, sustained evidence or application-specific cooldowns. |
| `DecisionStrategy(callback)` | A callback selector for `decision`, the containing graph's public Soul searching status and measurements. | Interpret measured demand, distinguish proposed from applied changes, or prepare a handoff that depends on the operator's progress. |

For example, keep route preparation separate from demand-policy evaluation:

```python
from collections.abc import Callable, Mapping
from typing import Any

from polyad_sdk import CallbackStrategy, Change, Environment, TopologyStrategy


def routing_and_demand_strategies(
    remember_candidates: Callable[[tuple[Mapping[str, Any], ...]], None],
    observe_demand: Callable[[Mapping[str, Any] | None], None],
) -> tuple[TopologyStrategy, CallbackStrategy]:
    def routes_changed(change: Change, current: Environment) -> None:
        # The supervisor checks compatibility, readiness and permissions again
        # before using a candidate. An unavailable view withdraws new intent.
        remember_candidates(current.candidates)

    def demand_changed(change: Change, current: Environment) -> None:
        decision = current.decision if current.available else None
        observe_demand(decision)  # None means unknown, not zero demand.

    return (
        TopologyStrategy(routes_changed),
        CallbackStrategy(demand_changed, paths=("decision",)),
    )
```

The application supplies bounded callbacks that store intent or wake its existing
supervisor. `observe_demand` receives the optional decision mapping, including
`demandSignal`, `demandUnit`, `demandValue` and `observedAt`. It checks the configured
signal, unit and freshness before using the value. Missing samples preserve
uncertainty. With several producers or consumers, local reservation and
work-ownership rules govern how new intent is applied.

## Choose checks for common changes

These constraint strategies implement a common `evaluate(current)` method. It
returns a `ConstraintAssessment(name, state, reason)`: **satisfied** means this
check passed, **blocked** means a required condition is unmet, and **unknown**
means information needed for the check is missing or unusable. Your application
requires all checks relevant to an action to pass before acting. Each strategy
also sends its result to your callback on the first snapshot and later changes,
including when a previously blocked condition recovers.

| Strategy | When to use it | Default assessment | Application response |
| --- | --- | --- | --- |
| `FreshnessStrategy` | Any change; topology becomes stale, the node drains or its graph becomes unavailable | Require `current.available`; preserve the SDK's reason when unavailable | Stop new assignments based on that topology; finish or checkpoint accepted work |
| `PeerAvailabilityStrategy` | Scale down, rollout, rewiring or capability replacement leaves too few usable destinations | Require at least `minimum` outgoing logical peers with live executions that pass the required application `usable` predicate | Reduce offered work or pause the affected route until compatible, ready peers recover |
| `ConnectionPermissionStrategy` | A requested connection is awaiting approval, or its permission has ended | Check that the chosen connection request is `Active` and unexpired in `service.view` | Wait before sending new work to that destination; resume once permission and the consumer's health checks pass |
| `ResourceBudgetStrategy` | Scale up or replacement overlaps old and new execution and exceeds capacity | Require measured resource use plus configured `reserve` to fit `maximum`; absent or invalid data is unknown | Defer the change, reduce concurrency or select an approved profile with a smaller footprint |
| `ContainerBudgetStrategy` | Local subprocess growth or rolling replacement runs into the container's CPU or memory allowance | Default capacity from a positive projected resource request; require measured local usage plus overlap to fit | Hold new children or reduce their concurrency while accepted work drains |
| `DecisionGuardStrategy` | A local handoff depends on Soul searching resolving a constrained or unfinished change | Accept `Satisfied`, `BelowDemandThreshold` and `Applied` by default; missing decisions are unknown and other phases block | Hold the dependent action through cooldown, missing capacity, computation limits or an unavailable layout |

Constraint names must be unique within a service. Configure guards per action:
a rollout memory check applies before starting replacements; a connection
permission check applies before sending along that connection. A pending optional
route should not stop unrelated traffic. Install a decision guard only for actions
that depend on a Soul searching decision. Set `allowed=("Applied",)` when that dependency requires
the operator to commit a change.

`PeerAvailabilityStrategy` counts logical peers, not Pods. Ten replicas of one
logical destination count as one peer. Its `usable` callback must check the
application's readiness, protocol compatibility and permission requirements.
Likewise, an approved connection and an `Applied` operator decision each satisfy
one condition; the application still checks the destination's health and opens
the required HTTP, gRPC or other connection before sending work.

`ResourceBudgetStrategy` reads a dot-separated metric below `Environment.resources`,
such as `byKind.Deployment`. Match `maximum` and `reserve` to that metric's unit and graph
boundary. The SDK's resource view describes the containing graph. A local
process supervisor can provide its own observation adapter, as in
[`soul.py`](../../demo/soul.py), or implement a strategy over its local capacity state.
Observed headroom guides admission; atomic reservations prevent concurrent
operations from spending the same headroom twice.

## Wait for permission to use a new connection

Suppose a producer discovers a second consumer and wants to send its jobs to that
consumer through a temporary connection. This connection is a permission managed
by Polyad, with an expiry time. The application still sends its jobs using its
own transport, such as HTTP or gRPC.

1. The producer calls `service.connect(...)`. Polyad returns a **connection
   receipt**, a record that identifies the request and reports its progress.
   Keep its unique ID (`uid`) so the application can track this particular request.
2. The services receive connection events. The required participants approve
   the request; a requester authorized to consent for its own endpoint supplies
   that consent through the request. The operator checks permissions and graph
   rules before marking the connection `Active`.
3. `ConnectionPermissionStrategy` checks that the selected receipt is `Active`
   in `service.view` and its permission has not expired. Before assigning work,
   your application checks this result, the consumer's health and protocol
   compatibility.
4. Permission has a requested lifetime, also called its time to live (TTL).
   If it expires or is revoked, stop assigning new jobs over that
   connection. Arrange completion, retry or handoff for accepted jobs according
   to your application's delivery guarantees.

The strategy returns **satisfied** when this permission check passes, **blocked**
while permission is pending or absent, and **unknown** when the SDK lacks usable
graph information. Pass a `receipt` function that returns the selected record's
ID, or `None` before the request exists. Its `publish` callback receives the
result, so your application can record it or update its scheduling state.

Apply this check to work sent over that particular connection. A producer can
continue using other healthy, permitted consumers while this request is pending.
Check again immediately before sending: permission can expire between events.
See [connection negotiation](adaptive-microservices.md#negotiate-connections-and-drain-work)
for request and approval examples.

## Compose independent constraints

Pass an ordered sequence through `strategies=` on the constructor or
`from_environment()`. **Construction requires at least one strategy by default**;
omitting strategies or supplying an empty collection raises `ValueError` before
the observation lifecycle starts. Each service chooses its categories and can
supply custom implementations. Construct every selected component first; the
service copies and fixes their order when it is instantiated.

An explicit `require_strategies=False` permits an empty set, with **undefined
adaptation behavior for uncovered cases**. This disables only the nonempty
requirement. Component types and unique constraint names are still validated,
and the SDK's observation and authorization contracts continue to apply.

The runtime executes:

```text
authorized baseline or deltas
  -> strategies, in supplied order
  -> subclass adapt(change)
  -> matching on_change hooks
  -> checkpoint and cursor advancement
```

This service checks both observation freshness and usable downstream peers. The
application supplies a bounded readiness predicate backed by its own health and
protocol state. A producer calls `can_assign()` before reserving and sending work.

```python
from collections.abc import Callable, Mapping
from typing import Any

from polyad_sdk import (
    AdaptiveService, Change, ConstraintAssessment, FreshnessStrategy,
    PeerAvailabilityStrategy,
)


def producer_service(
    usable_peer: Callable[[Mapping[str, Any]], bool],
    update_admission: Callable[[bool], None],
) -> AdaptiveService:
    assessments: dict[str, ConstraintAssessment] = {}

    def remember(result: ConstraintAssessment) -> None:
        assessments[result.name] = result

    guards = (
        FreshnessStrategy("fresh-topology", remember),
        PeerAvailabilityStrategy("usable-consumer", remember, usable=usable_peer),
    )

    class Producer(AdaptiveService):
        def can_assign(self) -> bool:
            current = self.view
            return all(guard.evaluate(current).satisfied for guard in guards)

        def adapt(self, change: Change) -> None:
            update_admission(self.can_assign())

    return Producer.from_environment(strategies=guards)
```

The named assessments can feed logs or application metrics. Re-evaluating at
admission also catches expiry between events and changes in local readiness.
Combine every relevant constraint with **all**, so recovery of one constraint
cannot clear another blocker. A blocked or unknown assessment is an ordinary
control result: it does not raise an exception or prevent observing recovery.

Consumers can use the same pattern for accepting work or starting additional
workers. Keep constraints on **new** work separate from the obligation to drain
work already accepted. See [cooperative producers and consumers](adaptive-microservices.md).

## Default local capacity from projected environment

The SDK reads the operator's [projected context](workload-environment.md#sdk-defaults)
from its importable `env` dictionary. `WorkloadContext` exposes graph ancestry,
execution identity, placement, endpoints and container resources without
including credentials. Application settings can override these defaults.

```python
from collections.abc import Callable

from polyad_sdk import ConstraintAssessment, ContainerBudgetStrategy, WorkloadContext


def child_memory_guard(
    read_total_memory_bytes: Callable[[], float | None],
    publish_assessment: Callable[[ConstraintAssessment], None],
) -> ContainerBudgetStrategy:
    context = WorkloadContext.from_environment()
    return ContainerBudgetStrategy(
        "replacement-memory",
        publish_assessment,
        resource="memory",
        resources=context.resources,
        used=read_total_memory_bytes,
        reserve=64 * 1024 * 1024,  # Include the replacement's temporary overlap.
    )
```

`used` measures the relevant process tree or container in bytes. With
`resource="cpu"`, supply CPU usage and reserve in millicores. Local measurements
come from the application's existing sampler; the SDK starts no sampler thread.
The guard can also read selectors directly from `env` when `resources` is omitted.
Explicit `maximum=` overrides the default. A projected positive request supplies
the conservative default budget, capped by a smaller positive projected limit.
Without a positive request, the result remains unknown until an explicit budget
is supplied. Kubernetes limit-selector fallbacks can describe node capacity, so
the SDK does not interpret a limit alone as dedicated service capacity.

Projected values are startup snapshots. Re-evaluate measured usage and reserve
capacity under the local supervisor's lock before starting children. Configure a
new guard after an intentional budget change. Graph-wide resource metrics remain
the input to `ResourceBudgetStrategy`; container allowances are not substituted
for graph-wide ceilings.

## Propose a bounded profile change

`ThresholdStrategy` addresses resource pressure by requesting one of two
application-approved profiles. It reads a configured graph resource metric,
enters the `busy` profile at `high`, and returns to `idle` at `low`. The interval
between them retains the current profile. It reads the **committed** profile on
every evaluation; proposing a change does not claim that it was admitted.

Use it when two approved profiles and one observed numeric resource metric are
enough to express the policy. **Hysteresis** is the gap between the entry and exit
thresholds: moving slightly back below `high` does not immediately undo a change;
the metric must reach `low` to switch back. `low`, `high`, `idle`, `busy`, `active`
and `propose` are explicit configuration inputs.

Choose `ResourceStrategy` or a custom `AdaptationStrategy` when the decision
requires several signals, more than two profiles, sustained demand or a local
queue measurement. `ThresholdStrategy` reads `current.resources`; application
throughput samples arrive under `current.decision`. Use `DecisionStrategy` or a
`CallbackStrategy` selecting `decision` to implement policies over those
measurements. A high value may call for more workers or less concurrency, depending
on what the metric means and which resource is constrained.

For example, a service can lower its own concurrency as the containing graph
acquires more Deployment resources. The example uses the published
`resources.byKind.Deployment` count as a coarse signal; calibrate this policy
against measured application pressure before using it.

```python
from collections.abc import Callable

from polyad_sdk import Change, Environment, ThresholdStrategy


def pressure_strategy(
    read_committed_profile: Callable[[], str],
    remember_proposal: Callable[[str], None],
) -> ThresholdStrategy:
    def propose(profile: str, change: Change, current: Environment) -> None:
        remember_proposal(profile)

    return ThresholdStrategy(
        "byKind.Deployment",
        low=2,
        high=4,
        idle="normal-concurrency",
        busy="conservative-concurrency",
        active=read_committed_profile,
        propose=propose,
    )
```

`read_committed_profile()` returns the active profile name as a string.
`remember_proposal(profile)` accepts the proposed name and returns `None` after
recording intent. The typed `propose` adapter shows the SDK's full callback
signature: profile name, triggering `Change` and current `Environment`.

Supply the resulting component alongside the relevant constraint guards. In
`adapt()`, hand its proposed profile to the application's existing supervisor.
That supervisor rechecks constraints, reserves overlap capacity, starts and
awaits replacement readiness, commits the new profile and drains the old one.
A rejected proposal leaves the committed profile unchanged. The proposal callback
should replace the pending target idempotently; repeated evidence may propose it
again until the supervisor commits it. Missing, expired,
boolean or nonfinite metrics leave the profile unchanged as well. Additional
cooldowns, sustained-demand windows and limits on changes belong in the
supervisor or a custom strategy.

[`soul.py`](../../demo/soul.py) demonstrates those admission, readiness and draining
steps for real child processes. Its application-specific sustained-demand policy
is described in [the local Soul walkthrough](local-soul-searching.md#writing-an-adaptive-application).
The operator's [Soul searching policy](../graphs/soul-searching.md) continues to
own graph-level layout and traffic changes within GraphRules.

## Write a strategy

Use the two ABCs for application-specific behavior:

| Interface | What to implement | When to choose it |
| --- | --- | --- |
| `AdaptationStrategy` | `adapt(change, current)` updates bounded application intent. | A policy needs state across observations, several metrics, application demand, or a coordinated proposal such as changing both worker count and batch size. |
| `ConstraintStrategy` | `evaluate(current)` returns a named `ConstraintAssessment`; the base class publishes it during adaptation. | Admission depends on an application condition such as durable queue space, partition ownership or an available external-system quota. |

For example, a consumer with a durable inbox can implement a queue-space guard
and combine it with `ContainerBudgetStrategy`. A producer holding partition
leases can implement an ownership guard and combine it with
`PeerAvailabilityStrategy`. In either case, recheck the application condition
and reserve the affected capacity when accepting work.

Subclass `AdaptationStrategy` and implement
`adapt(change: Change, current: Environment) -> None`. Use `change.matching(...)`
to select deltas, and use `current` for decisions: it reflects freshness when the
component is called, including during retries. A component can update local
intent, revise routing candidates or wake the application's supervisor.

For a new difficulty, subclass `ConstraintStrategy` and implement
`evaluate(current) -> ConstraintAssessment`. The base publishes assessments
through the callback passed to its constructor. This supports application
constraints such as available durable queue space, an exclusive partition lease
or capability compatibility during a Natural Selection replacement.

Use the [callback helpers](#observation-and-callback-strategies) when an existing
function already implements the policy. Parent additions and removals match
child prefixes, so removing the entire decision also reaches a callback that
selects `decision`.

## Delivery, recovery and admission

The SDK copies the component sequence at construction and performs no network
calls until the observation lifecycle starts. Each strategy receives immutable
views and its own copy of the triggering event. Successful components are tracked
individually: a failure blocks later stages and checkpointing; a retry resumes
unfinished delivery with fresh context. Already successful components are skipped
within the same instance. Callbacks must tolerate partial effects and replay
after a process restart.

Strategies are event-driven. Heartbeats with unchanged meaningful state do not
invoke them, and they start no timers or threads. Before acting, evaluate guards
against `service.view` and check local reservations under the application's own
concurrency control. That also protects an action after a later callback fails
and an earlier successful guard is skipped on retry. If the stream fails, the
supervisor handles recovery and admission; see the
[SDK freshness and recovery contract](../../pkg/polyad-sdk/README.md#identity-permissions-and-freshness).

These application components complement the operator's
[mutation planner and executor](../development/mutations.md). They assess local
responses to observed changes; graph mutations still pass through the operator's
live GraphRules, permissions and resource budgets.
