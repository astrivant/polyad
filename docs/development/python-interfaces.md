# Python extension interfaces

Polyad exposes abstract base classes for application behavior, scheduling and
infrastructure adapters. Subclass the relevant contract and implement its required
methods. Python rejects incomplete subclasses before they can start workers or
open connections. Consumers use these contracts where implementations can be
substituted; the shipped backends retain their existing configuration and behavior.

## Table of contents

- [Public contracts](#public-contracts)
- [Local execution and scheduling](#local-execution-and-scheduling)
- [SDK observation and actions](#sdk-observation-and-actions)
- [Operator adapters](#operator-adapters)
- [Imports and configuration](#imports-and-configuration)

## Public contracts

| Import | Implement | Shipped implementation or consumer |
| --- | --- | --- |
| `polyad.graph.Workload` | `work` property and `run(control, checkpoint)` | `polyad.balance.Graph`, heartbeat example, `Scheduler` |
| `polyad.graph.ProcessOwner` | `run(...)`, `stop()` | Application-supplied owners passed to `OperationQueue(owner_factory=...)` |
| `polyad.balance.SchedulingPolicy` | `rank(...)`, `preempt(...)`; optionally `priorities(...)` | `ShortestRemaining`, `FIFO`, `BreadthFirst`, `DepthFirst` |
| `polyad_sdk.AdaptiveService` | `adapt(change)` | Application subclasses and [`soul.py`](../../soul.py) |
| `polyad_sdk.EventSource` | `topology(...)`, `events(...)`, `event_endpoints(...)`; set `url` | `Client`, `Subscription`, `AdaptiveService` |
| `polyad_sdk.ThroughputReporter` | `report_throughput(sample)` | `Client`, `AdaptiveService(api=...)` |
| `polyad_sdk.ConnectionNegotiator` | `connect_services(request)`, `respond_connection(...)` | `Client`, `AdaptiveService(connections=...)` |
| `polyad.operator.adapters.ResourceAPI` | `request(...)`, `get(...)`, `owned(...)`, `delete(...)` | Kubernetes `API`, `CompositionStore` |
| `polyad.operator.adapters.StateBackend` | `start()`, `begin()`, `save(...)`, `record_event(...)`, `connections()`, `close()` | PostgreSQL `StateStore`, root control plane |
| `polyad.cache.CacheBackend` | `get(key)`, `set(key, value, ttl_seconds=...)`, `close()` | Redis/Dragonfly `Cache` |

Methods on the operator resource, state and cache interfaces are awaited. SDK
transports are synchronous and run on the application's chosen observation thread.
The shared resource models in `polyad_types` describe serializable data consumed
by these behavioral interfaces.

## Local execution and scheduling

A `Workload` exposes an immutable `Work` description and implements cooperative
execution. Implement `work` as a property or a concrete class attribute. A field
assigned only inside `__init__`, including a generated dataclass constructor,
does not fulfill an abstract property. The
[heartbeat example](../../examples/heartbeat.py) demonstrates the property form;
the [scheduling guide](../../pkg/polyad/balance/README.md#cooperative-execution)
shows a constant description.

`run()` retains ownership of accepted work and any child processes until it
finishes or returns a recoverable checkpoint. `ProcessOwner` applies the same
ownership rule to external commands: `stop()` prevents new children, stops and
joins the owned process tree, and is safe to call repeatedly. The operation queue
holds an owner through failures until cleanup succeeds.

Custom policies extend `SchedulingPolicy` directly. For example, this policy
prefers the most recently submitted ready work and lets active work finish:

```python
from polyad.balance import SchedulingPolicy
from polyad.graph import Estimate


class NewestReady(SchedulingPolicy):
    def rank(self, estimate: Estimate, waiting: float, order: int) -> tuple[float, float, int]:
        return (0, 0, -order)

    def preempt(self, running: Estimate, waiting: Estimate, elapsed: float, waited: float) -> bool:
        return False
```

Pass `policy=NewestReady()` to `Scheduler` or a nested `Graph`. The default
`priorities()` supplies insertion-order positions. The scheduler still checks
dependencies, available slots, memory and checkpoint eligibility. Policy choices
change ordering within those constraints. See
[graph traversal ordering](../../pkg/polyad/balance/README.md#graph-traversal-ordering).

## SDK observation and actions

`AdaptiveService` accepts `EventSource`, `ThroughputReporter` and
`ConnectionNegotiator` independently. The ordinary `Client` implements all three;
applications can keep separate credentials and endpoints for each capability.

An `EventSource` supplies neighborhood snapshots and replayable events. Its `url`
identifies the configured authority used to scope subscription idempotency keys.
Implementations preserve cursor ordering, graph incarnation checks, bounded reads
and cancellation. Endpoint discovery comes from that configured authority.
`subscribe()` is provided by the ABC and returns the shared `Subscription`, so a
custom transport uses the same hooks, retry bookkeeping and checkpoints.

The SDK continues to validate events, calculate deltas and invoke `adapt(change)`
before additional callbacks and checkpoint advancement. See the
[SDK subclass contract](../../pkg/polyad-sdk/README.md#subclass-contract) and
[local adaptation walkthrough](../workloads/local-soul-searching.md#writing-an-adaptive-application).
Throughput reports and connection requests retain their existing authorization,
consent and admission rules when a different transport implements these contracts.

## Operator adapters

`ResourceAPI` describes refreshed resource reads and guarded writes. A missing
object returns `None` from `get()`. Ownership queries use persisted UIDs, and
deletion requests retain identity fences while later observations confirm cleanup.
Conflicts and uncertain results propagate for reconciliation. The concrete
Kubernetes adapter retains its
[dependency-aware write pipeline](write-pipeline.md).

`StateBackend.begin()` returns an ordering ticket before a complete scan starts.
`save()` atomically replaces the cluster/namespace inventory, including removals,
and rejects scans superseded by a newer committed scan. `record_event()` preserves
deduplication and configured retention. `connections()` reports connection pressure
and state freshness for cached telemetry. Root control-plane consumers accept this
interface; PostgreSQL remains the configured durable backend.

`CacheBackend` describes transient JSON values with explicit expiry. Concrete
Redis stream users continue to use their Redis-specific APIs for queue and replay
operations. Durable state and credentials keep their separate storage paths.

## Imports and configuration

Importing `polyad.operator.adapters.ResourceAPI`, `StateBackend`, or
`polyad.cache.CacheBackend` does not load Kubernetes, PostgreSQL or Redis drivers.
The cache package loads its Redis implementation when `Cache` is requested. SDK
interfaces ship in `polyad-sdk` and require no operator installation.

These are Python extension points. Existing Helm values still select the shipped
services and optional capabilities. Construct custom implementations explicitly
where the relevant constructor accepts a contract. ABCs enforce required methods;
implementation tests verify lifecycle, freshness and concurrency behavior.
