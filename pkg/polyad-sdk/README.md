# Polyad SDK

A typed Python 3.11–3.14 SDK for **Service Symbiosis** and Polyad's operator APIs.
Service Symbiosis lets microservices discover compatible peers and adapt their
relationships and work together across Graphs and PolyGraphs.
Applications receive connection, capacity, metric and decision deltas with current
context. `AdaptiveService` combines these observations with modular adaptation
strategies. Managed subprocess plans provide readiness checks, worker replacement
and draining; built-in OpenTelemetry support adds traces and metrics.
The same package includes `Client` for composition, activation, demand reporting,
discovery, SSE/WebSocket events and temporary connections.

`connect(document)`, `connection(namespace, request_id)` and
`disconnect(namespace, request_id)` use the separate connections Service and a
projected service-account token. See the [temporary connections guide](https://github.com/astrivant/polyad/blob/main/docs/apis/temporary-connections.md)
for token rotation, namespace scope, TTL and cleanup semantics.
It uses the shared [polyad-types](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/README.md) models and
does not install the operator.

For application behavior around these APIs, see
[Service Symbiosis: writing adaptive microservices](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptive-microservices.md):
cooperative producers and consumers, graph-level capacity, backpressure and
safe handoffs, using the `AdaptiveService` abstract base class.

## Table of contents

- [Installation](#installation)
- [Package layout](#package-layout)
- [Environment and projected defaults](#environment-and-projected-defaults)
- [Adaptive services and deltas](#adaptive-services-and-deltas)
  - [Snapshots and permission to act](#snapshots-and-permission-to-act)
    - [Environment fields](#environment-fields)
    - [Topology and neighbor fields](#topology-and-neighbor-fields)
    - [Resource reports and decisions](#resource-reports-and-decisions)
    - [Temporary connection fields](#temporary-connection-fields)
    - [Change and Delta fields](#change-and-delta-fields)
    - [Reading a delta in application code](#reading-a-delta-in-application-code)
    - [Freshness and permission checks](#freshness-and-permission-checks)
  - [Subclass contract](#subclass-contract)
  - [Adaptation strategies](#adaptation-strategies)
  - [Identity, permissions and freshness](#identity-permissions-and-freshness)
  - [Hooks, recovery and explicit actions](#hooks-recovery-and-explicit-actions)
- [Telemetry and subprocess plans](#telemetry-and-subprocess-plans)
- [Reachability and symbiosis models](#reachability-and-symbiosis-models)
- [Observations](#observations)
- [Activation](#activation)
- [Composition and request handling](#composition-and-request-handling)
- [Events and topology](#events-and-topology)
- [Event types and size limits](#event-types-and-size-limits)
- [WebSocket subscriptions](#websocket-subscriptions)
- [Discovery and hooks](#discovery-and-hooks)
- [Temporary connection consent](#temporary-connection-consent)
- [Remote clusters](#remote-clusters)
- [Report throughput to Soul searching](#report-throughput-to-soul-searching)
- [Publishing](#publishing)

## Installation

Install from a checkout:

```sh
pip install ./pkg/polyad-types ./pkg/polyad-sdk
```

Release CI builds and publishes `polyad-sdk` separately from `polyad`.
Once that release is available, install it with `pip install polyad-sdk`.

## Package layout

The SDK groups implementation modules by responsibility:

```text
polyad_sdk/
  __init__.py                 Public imports
  symbiosis/                  Application adaptation and cooperation
    models.py                Immutable observations and deltas
    state.py                 Observation reduction, freshness and expiry
    service.py               AdaptiveService lifecycle and delivery
    reachability/            Interaction models, queue envelopes, guard and optional HJ analysis
    strategies/
      base.py                Strategy ABCs and constraint assessments
      callbacks.py           Logging and delta-selected application callbacks
      capacity.py            Graph/container budgets and profile thresholds
      topology.py            Freshness, peers and connection permissions
      decisions.py           Operator decision constraints
  api/
    client.py                Operator API requests and stream entry points
    interfaces.py            Feedback and connection action ABCs
  events/
    source.py                EventSource ABC and subscription factory
    filters.py               Composable observation filters
    subscriptions.py         Hook delivery, replay and checkpointing
  runtime/
    environment.py           Shared import-time environment snapshot
    context.py               Typed workload, Pod and resource context
  observability/
    telemetry.py             Shared traces, metrics and optional OTLP exporters
  processes/
    models.py                Approved worker specifications, plans and results
    process.py               Child process and process-group ownership
    supervisor.py            Admission, readiness, replacement and draining
  transport/
    http.py                  HTTP errors and redirect restrictions
    routing.py               Trusted endpoint selection and TLS identity
    websocket.py             Optional WebSocket stream transport
```

Package-root imports such as `from polyad_sdk import Client, AdaptiveService, env`
remain available. Feature packages expose their own public interfaces:

```python
from polyad_sdk.api import Client, ConnectionNegotiator, ThroughputReporter
from polyad_sdk.events import EventSource, Filter, Subscription
from polyad_sdk.events.filters import event_type, field, graph
from polyad_sdk.runtime import WorkloadContext, env, refresh_environment
from polyad_sdk.symbiosis.strategies import FreshnessStrategy, ResourceBudgetStrategy
```

Implementation imports use these grouped paths. The WebSocket implementation
loads when that transport is selected. The single `py.typed` marker at the SDK
root applies to all subpackages.

## Environment and projected defaults

Import the SDK's complete environment snapshot as `env: dict[str, str]`:

```python
from polyad_sdk import env, refresh_environment, WorkloadContext

application_mode = env.get("APPLICATION_MODE", "worker")
context = WorkloadContext.from_environment()  # Uses env, including operator projections.
memory_budget = context.resources.budget("memory")  # Bytes; None without a positive request.

# Deliberate reload during startup configuration, if the process environment changed:
refresh_environment()
```

`env` is populated from **all** of `os.environ` when the SDK is first imported.
It is also available as `polyad_sdk.runtime.environment.env`. `refresh_environment()`
updates the same dictionary so references imported elsewhere remain valid.
An explicit mapping can replace it with `refresh_environment(mapping)`.
Configure or refresh it before starting observation threads. Existing services
retain their identity and settings; mounted credential files still reload per
request. The complete mapping includes application secrets, so use the typed
public context for logs and diagnostics.

`AdaptiveService.from_environment()` stores the immutable `WorkloadContext` as
`service.context`. It covers the operator's
[projected workload variables](https://github.com/astrivant/polyad/blob/main/docs/workloads/workload-environment.md):
containing/root graph identity, ancestry, logical and runtime node names,
definition and activation receipts, concrete Pod and placement context, resource
requests/limits, and operator endpoint URLs. `ContainerResources` and `PodContext`
are independently importable types. Missing optional context stays empty or
`None`; containing graph and logical node identity remain required.

Pass `environ={**env, "POLYAD_EVENTS_URL": "https://events.example"}` to
`from_environment()` for one service's overrides without changing the shared
snapshot. `WorkloadContext.from_environment(mapping)` and
`ContainerResources.from_environment(mapping)` accept explicit mappings too.
The ordinary service constructor accepts `context=` and explicit clients.

`ContainerBudgetStrategy` uses projected CPU or memory requests as conservative
defaults; explicit `maximum=` takes precedence. Limits alone cannot establish a
default budget because Kubernetes may substitute node allocatable resources when
a limit is omitted. Environment values are startup snapshots. Application usage
measurements and live graph observations remain separate. Physical Pod placement
does not select a remote event authority: pass `cluster=` explicitly when needed.

When optional VPA compatibility targets the workload, `context.vpa` is a
`VPAConstraints` value with the container's validated CPU/memory minimums,
maximums and update mode. `context.vpa.clamp("cpu", proposed_millicores)` helps
keep application concurrency or worker-profile decisions inside that interval.
The bounds and `context.resources` are startup policy/Downward API snapshots.
Use `container_metrics()` for current cgroup-v2 values after an in-place resize:

```python
from polyad_sdk import WorkloadContext, container_metrics

context = WorkloadContext.from_environment()
sample = container_metrics()
cpu_ceiling = sample.cpu_limit_millicores
memory_in_use = sample.memory_usage_bytes
memory_headroom = sample.memory_available_bytes
```

The live sample also exposes cumulative `cpu_usage_usec`. Unbounded or
unavailable cgroup observations are `None`; the helper does not require a
Kubernetes API token. See the
[VPA compatibility guide](https://github.com/astrivant/polyad/blob/main/docs/deployment/vpa.md)
for the manifest and projected environment contract.

## Adaptive services and deltas

`AdaptiveService` is an abstract base class for Service Symbiosis. Implement
`adapt(change)` in a subclass to respond to authorized neighborhood changes.
The inherited runtime maintains immutable context, constructs **deltas** and
handles stream checkpoints. `Client` remains available for direct API calls and
raw event subscriptions in this same package.

A **snapshot** is a read-only copy of what the SDK knows about this service's
surroundings when it builds a view. It contains graph connections and any resource
reports, operator decisions and temporary connection records received so far.
A **delta** identifies what changed between two snapshots. Use these observations
to decide what needs attention; the [action checks below](#snapshots-and-permission-to-act)
determine whether your application can use a connection or make a change.

```python
import logging

from polyad_sdk import AdaptiveService, Change, ObserveStrategy, Settings

log = logging.getLogger(__name__)


class NeighborhoodService(AdaptiveService):
    def adapt(self, change: Change) -> None:
        if change.baseline:
            log.info("Neighborhood baseline: %s candidate peers", len(change.after.candidates))
            return
        for delta in change.deltas:
            log.info(
                "%s %s: %s -> %s (numeric difference: %s)",
                delta.kind, ".".join(delta.path), delta.before, delta.after, delta.difference,
            )


service = NeighborhoodService.from_environment(
    strategies=[ObserveStrategy(log)],
    settings=Settings(
        refresh_seconds=10,
        max_age_seconds=60,
        max_observations=512,
        max_connections=256,
        transport="sse",  # "websocket" when enabled on the operator
    ),
    timeout=45,
)
service.run()  # Blocking: use the application's existing task/thread supervisor.
```

The initial snapshot and an explicit replay reset establish a **baseline** with
no deltas. A missing measurement remains unknown. A requested replica count
changing from three to five yields `before=3`, `after=5`, `difference=2`. A newly observed
metric is `added`, with `difference=None`; it did not increase from an assumed
zero. Numeric differences are arithmetic changes, not elapsed-time rates. Compare
units, measurement windows and generations in the surrounding views before using
them for application control.

Paths use stable logical names and execution UIDs. Reordering lists, changing an
event cursor or refreshing observation timestamps does not trigger a change hook.
Replacing an execution UID produces removal and addition, even if its name is the
same. New desired peers may initially have no running execution.

### Snapshots and permission to act

`Environment` is the SDK's snapshot type. `change.before` and `change.after` hold
the snapshots being compared. `service.view` builds another from the SDK's latest
cached observations, checking their age and connection expiry at the time you
read it. Previously saved snapshots stay unchanged as the cluster evolves.
Reading `service.view` makes no network request; the observation loop and
`service.refresh()` obtain newer topology information.

The snapshot combines observations that can have different timestamps. It
describes the selected graph node and the surroundings this service was allowed
to observe. Metrics and connection records populate as events arrive, so an
initial **baseline** can contain useful topology while measurements remain
unknown. Use the baseline to initialize application state and later deltas to
update it.

#### Environment fields

Import the snapshot and change types from the SDK:

```python
from polyad_sdk.symbiosis import Change, Delta, Environment
```

These are frozen Python dataclasses. Access their fields with attributes, such as
`view.topology`, then use keys for the nested records, such as
`view.topology["graph"]["uid"]`. SDK-produced records are recursively read-only
`Mapping` objects; JSON arrays become tuples. These Python snapshots are built
from the operator's topology response and events. For event payload validation,
use the shared [event types and JSON Schema](https://github.com/astrivant/polyad/blob/main/docs/apis/event-contract.md#schemas-and-validation).
`Environment`, `Change` and `Delta` are SDK objects, not event-envelope variants.

| Field or property | Python shape | Meaning and absence |
| --- | --- | --- |
| `topology` | `Mapping[str, Any]` or `None` | Selected node and its neighbors, described below. `None` before the first successful topology read. The last topology remains present if it becomes unusable; check `available`. |
| `observations` | `Mapping[str, Mapping[str, Any]]` | Resource reports keyed by Kubernetes UID. Includes the containing boundary and known executions of this node and its neighbors/dependencies, subject to observation permissions. Empty until reports arrive. |
| `connections` | `Mapping[str, Mapping[str, Any]]` | Temporary connection receipts involving this endpoint, keyed by receipt UID. Pending receipts can be present; presence alone does not mean approval. |
| `available` | `bool` | Whether topology is fresh, valid, executable, nonterminating and still declares this node as desired, with no outstanding observation failure. This does not establish freshness of every metric or readiness of peers. |
| `reason` | `str` or `None` | Explanation of unavailability, such as `initial snapshot required`, `topology observation is stale` or `topology refresh failed`. `None` when available; treat the text as a diagnostic, not a fixed enum. |
| `resources` (property) | `Mapping[str, Any]` or `None` | `observations[topology["graph"]["uid"]]["resources"]`, when that report exists. `None` before receipt or after expiry; an empty mapping means the report supplied no resource metrics. |
| `decision` (property) | `Mapping[str, Any]` or `None` | The containing boundary's `status.throughput`, when reported. Soul searching measurements and recommendations, described below. |
| `candidates` (property) | `tuple[Mapping[str, Any], ...]` | Outgoing neighbor entries whose node is desired and has at least one nonterminating execution with a positive `replicas` value, or with that field omitted. Empty when unavailable. |

`Environment` describes live observations around the service. The separate
[process environment](#environment-and-projected-defaults) dictionary `env`
contains startup environment variables; `service.context` contains projected
workload identity and container budgets.

#### Topology and neighbor fields

`view.topology` is the **selected-node** response from
[Read current neighbors](https://github.com/astrivant/polyad/blob/main/docs/workloads/workload-events.md#read-current-neighbors).
All fields below are present after a successful read unless marked optional.

| Key | Shape | Meaning |
| --- | --- | --- |
| `graph` | Mapping with string `kind`, `namespace`, `name`, `uid`; optional `cluster` | Exact Graph, PolyGraph or ReplicaGroup instance containing this node. A replacement with the same name has a different UID. |
| `revision` | `str` | Structural digest, including execution membership. |
| `observedAt` | `int` or `float` | Topology publication time in Unix seconds. Used for topology freshness. |
| `cursor` | `str` | Event replay position paired with the topology read. `service.cursor` separately tracks successfully handled events. |
| `valid`, `templateOnly`, `terminating` | `bool` each | Whether topology resolved, whether this is a reusable definition, and whether deletion has begun. |
| `node` | Node mapping | The selected application node. |
| `incoming`, `outgoing` | Tuples of `{node, ports}` mappings | Data-flow neighbors. Each port record has integer `port` and string `protocol` (`TCP`, `UDP` or `SCTP`); ports describe the destination side of that directed edge. |
| `dependencies`, `dependents` | Tuples of `{node, condition}` mappings | Lifecycle prerequisites and the nodes waiting on this node. `condition` is `started`, `ready` or `completed`. |

Each node mapping has:

| Key | Shape | Meaning |
| --- | --- | --- |
| `name` | `str` | Logical name within this boundary, including replica ordinals where applicable. |
| `kind`, `ref` | Optional `str` fields | Declared resource kind and definition reference. They can be absent for a removed node whose execution is still draining. |
| `cluster` | Optional `str` | Declared placement when supplied. |
| `desired` | `bool` | Whether the node remains in the desired topology. A desired node may still have no execution. |
| `requires` | Tuple of `{node, condition}` mappings | Names of prerequisites and their lifecycle conditions. |
| `executions` | Tuple of execution mappings | Resources observed for this node, including terminating resources. Each has string `kind`, `name`, `uid`, `runtimeNode` and boolean `terminating`; remote entries can also have `cluster` and `namespace`. |
| `executions[].replicas` | Optional nonnegative `int` | Requested Deployment/StatefulSet replicas, or a DaemonSet's `desiredNumberScheduled`. This is desired capacity, not a ready-Pod count. Other execution kinds omit it. |

For example, `view.topology["outgoing"][0]["node"]["executions"]` gives the
first outgoing peer's execution records. These records identify Kubernetes
resources; resolve network addresses through Services or discovery and check
application readiness before sending jobs.

#### Resource reports and decisions

`view.observations[uid]` retains a received
[`GraphObservation`](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/polyad_types/events/models.py)
as a mapping. The SDK keeps reports only for identities in the current
neighborhood, and discards older desired-state generations for a known UID.

| Keys | Shape and meaning |
| --- | --- |
| `apiVersion`, `kind`, `namespace`, `name`, `uid`, optional `cluster` | String resource identity fields. |
| `resourceVersion`, `generation` | Opaque Kubernetes revision string and integer desired-state generation. |
| `type` | `observation` or `deleting`. |
| `owners`, `ancestry` | Tuples of identity mappings. Owners have `kind`, `name`, `uid`; ancestors also have `namespace` and optional `cluster`. |
| `audit` | String-to-string mapping of public composition, request and node labels. |
| `status` | Mapping of optional `phase` (string), `ready`, `completed`, `failed` (booleans), `observedGeneration` (integer), `activations` and `throughput` (mappings). |
| `resources` | Extensible metric mapping. Current graph reports contain `total`, `terminating` and `byKind`, whose keys are Kubernetes kinds and whose values are counts. It can be empty. |

For example, `view.resources["byKind"]["Deployment"]` counts directly owned
Deployment resources. It does not count their ready Pods or measure CPU headroom.
See [`ResourceMetrics` and `ResourceCounts`](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/polyad_types/resources/status.py)
for the resource-count fields. A missing metric remains unknown; use `.get()`
and handle `None` instead of substituting zero.

`view.decision` is the containing boundary's Soul searching status. It is an
extensible mapping: fields can be absent or `None` while the operator waits for
measurements or a computation. Common fields are:

| Keys | Meaning |
| --- | --- |
| `mode`, `phase`, `observedGeneration` | Observe/Adapt mode, decision state such as `Stabilizing`, `Recommended` or `Applied`, and the desired generation evaluated. |
| `observedAt`, `offeredPerSecond`, `completedPerSecond` | Accepted application sample's ISO-8601 timestamp and rates in the configured work unit per second. |
| `demandSignal`, `demandUnit`, `demandValue` | Selected application signal, its unit and measured value. |
| `currentCheeger`, `target`, `proposedCheeger`, `recommendedLayout` | Current structural value, selected Cheeger bounds, proposed value and approved layout name. |
| `currentComputation`, `candidateComputations`, `computation` | Computation reports and diagnostics, including incomplete calculations. |
| `currentTraffic`, `targetTraffic`, `proposedTraffic` | Current route configuration, desired route/weight mappings and next bounded route configuration. |
| `currentCapacity`, `targetCapacity`, `proposedCapacity` | Current, desired and next capacity preparation settings (`lookaheadStages`, `maxPods`), when configured. |

Use the [Soul searching policy guide](https://github.com/astrivant/polyad/blob/main/docs/graphs/soul-searching.md#bounds-observations-and-scalability)
to interpret phases and stabilization. Recommendations describe preparation;
check the current applied configuration before using a route. Use Istio's
configured route when Istio owns traffic percentages.

#### Temporary connection fields

`view.connections[receipt_uid]` contains the public
[`ConnectionReceipt`](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/polyad_types/events/models.py):

| Keys | Shape and meaning |
| --- | --- |
| `requestId`, `name`, `namespace`, `uid` | String proposal idempotency key and receipt identity. Approval calls use the receipt UID. |
| `expiresAt` | ISO-8601 deadline with timezone. |
| `target` | Mapping with string `kind`, `graph`, `graphUid`, `source`, `target`; tuple `ports` of `{port, protocol}` records; boolean `bidirectional`. |
| `status` | Admission and negotiation mapping, including `phase`. Only an unexpired `Active` receipt satisfies the SDK connection-permission guard. |
| `consent` | Endpoint decisions, with values `Approve` or `Reject`. |
| `peers` | For atlas negotiation, source/target mappings containing `cluster`, `namespace`, `kind`, `graph`, `graphUid`, `node`. Empty for local receipts. |
| `revokeRequested` | Boolean revocation flag. A received revocation removes the receipt from the SDK view. |

Expired receipts and receipts reported as `Expired`, `Revoked`, `Rejected` or
`Failed` are excluded. Pending negotiation can remain visible until expiry.
See [temporary connection consent](#temporary-connection-consent) for requests
and approvals. Removing a receipt from the view does not by itself close an
application socket; your connection strategy handles stopping new work and draining.

#### Change and Delta fields

`adapt(change)` and registered hooks receive a `Change`:

| Field or method | Python shape | Meaning |
| --- | --- | --- |
| `before` | `Environment` | Previously published view. On the first baseline this is the initial unavailable view; it is never `None`. |
| `after` | `Environment` | View built for this delivery. It stays fixed during retries even if information later expires. |
| `deltas` | `tuple[Delta, ...]` | Differences in the compared fields below, in deterministic path order. |
| `baseline` | `bool` | True for initialization or explicit replay reset. `deltas` is empty; initialize from `after` instead of treating missing history as removals. |
| `event` | `Event` or `None` | Triggering envelope with `id` (cursor), `event` (type) and `data` (payload). Refresh/reset baselines have no triggering event. Each callback gets an independent event copy; `event.typed()` decodes its shared event model. |
| `matching(prefix)` | Returns `tuple[Delta, ...]` | Selects a dot-separated field prefix, including additions/removals of a containing object. |

Every `Delta` describes one changed field or a whole added/removed object:

| Field or property | Python shape | Meaning |
| --- | --- | --- |
| `path` | `tuple[str, ...]` | Stable comparison path. Logical names, execution UIDs and route targets replace list offsets. |
| `kind` | `added`, `removed` or `changed` | Field appeared, disappeared or changed value/type. |
| `before`, `after` | Read-only value of any supported JSON shape | Previous and new comparison values. `added` uses `before=None`; `removed` uses `after=None`. Use `kind` to distinguish absence from an actual JSON null. |
| `difference` (property) | `float` or `None` | `after - before` for a `changed` field with finite numeric values. Additions, removals, booleans and nonnumeric/nonfinite values have no numeric difference. |

The comparison projection has these roots:

| Change prefix | Compared data |
| --- | --- |
| `topology` | `valid`, `templateOnly`, `terminating`, `node`, and the four neighbor collections. Neighbor collections are keyed by logical node name; `executions` by UID and `requires` by prerequisite name. |
| `resources` | Containing-boundary resource metrics. |
| `decision` | Containing-boundary Soul searching status. |
| `observations.<uid>` | `type`, `generation`, `status` and `resources` for the reported resource. |
| `connections.<uid>` | Retained receipt fields, including consent, phase and expiry. |
| `available`, `reason` | Availability and its explanation. |

These are **comparison paths**, not literal indexes into `Environment`. Snapshot
neighbor/execution collections remain tuples. Named record lists are compared
by identity, and port lists become sorted `(port, protocol)` tuples. Cursor,
topology revision, resource version and observation timestamp changes alone do
not produce deltas. Unkeyed sequences retain their ordering. The same graph
measurement can appear under both `resources` and `observations.<graph-uid>.resources`;
those are two views of one observation, not two independent samples.

#### Reading a delta in application code

When an existing `sink` execution changes from two requested replicas to three,
the SDK emits:

```python
Delta(
    path=("topology", "outgoing", "sink", "node", "executions", "uid-sink", "replicas"),
    kind="changed",
    before=2,
    after=3,
)
# delta.difference == 1.0
```

If the entire `sink` neighbor first appears, the delta instead has
`path=("topology", "outgoing", "sink")`, `kind="added"` and its full normalized
neighbor record in `after`. `change.matching("topology.outgoing.sink")` handles
both cases. Likewise, `change.matching("resources.byKind.Deployment")` includes
an initial addition or expiry of the whole `resources` object.

A callback can read the delta for context, then inspect the current snapshot:

```python
def inspect_change(change: Change, current: Environment) -> None:
    if change.baseline:
        print("Initialize from the baseline:", len(change.after.candidates), "candidate peers")
        return
    for delta in change.matching("resources.byKind.Deployment"):
        print(delta.kind, delta.path, delta.before, delta.after, delta.difference)

    if not current.available:
        print("Pause new assignments:", current.reason)
        return
    resources = current.resources
    count = None if resources is None else resources.get("byKind", {}).get("Deployment")
    print("Current observed Deployment count:", count)  # None means unknown.


# Register before starting the observation loop; service is your AdaptiveService.
service.on_change(lambda change: inspect_change(change, service.view))
```

For example, counts `2 -> 3` produce a numeric `changed` delta. The first report
produces `added`; expiry produces `removed`. Neither absence means zero. Hooks
run for baselines and meaningful changes when the observation loop or an explicit
refresh processes them; reading `service.view` alone does not invoke hooks.

#### Freshness and permission checks

Topology freshness uses its publication timestamp. Resource reports expire
`Settings.max_age_seconds` after local receipt; the SDK does not expose that
receipt time as a snapshot field. A recently received report can still describe
an older application sample, so check the decision's own timestamp and generation
where relevant. Temporary receipts follow their `expiresAt` deadline and received
revocations, independently of the resource-report lifetime.

An unavailable view retains its last topology for diagnosis but returns no
`candidates`. Expired resource reports disappear from `observations`; derived
`resources` and `decision` then become `None`. `service.refresh()` fetches
topology; resource reports and receipt updates arrive through events. No single
snapshot timestamp certifies that all these inputs were observed together.

Having a snapshot lets your code inspect possible destinations, compare resource
reports and prepare an adjustment. Authority to act comes from the service's
credentials, operator policies and the application's own checks:

| What the snapshot shows | What your application can do with it | What must pass before acting |
| --- | --- | --- |
| A new outgoing consumer | Consider it for future jobs; resolve its address through configured Services or discovery. | Current connection permission, consumer readiness, protocol compatibility and capacity. |
| An `Active` temporary connection | Use its record to check whether the requested permission is still valid. | An unexpired permission for that connection, plus the consumer's application checks. |
| More available resources | Prepare a worker or concurrency change within the application's budget. | A current measurement and a reservation that prevents concurrent operations spending the same capacity. |
| A proposed operator change | Prepare for the reported layout or traffic change. | The required operator phase and application readiness; graph mutation requests use separately authorized APIs. |

`view.available=True` means the SDK considers the graph information recent and
usable for evaluating destinations. It does not grant new API capabilities,
approve a pending connection, reserve resources or prove a consumer is healthy.
Before assigning work, read `service.view` again and evaluate the relevant
strategies together with your application's checks. Handle failures and retries
through your normal delivery protocol, since another service can change after
the check.

For example, a snapshot listing consumer B gives a producer a reason to check B
as a destination. The producer starts sending only after B's connection is
permitted, B is ready and compatible, and capacity is available. If the SDK's
graph information becomes stale, pause new assignments that depend on it while
preserving the obligations for jobs already accepted.

See [identity, permissions and freshness](#identity-permissions-and-freshness)
for credential scope and expiry settings, and the
[topology endpoint contract](https://github.com/astrivant/polyad/blob/main/docs/workloads/workload-events.md#read-current-neighbors)
for the operator-published topology snapshot, its revision and replay cursor.

### Subclass contract

Import the same ABC from `polyad_sdk` or `polyad_sdk.symbiosis`. Instantiating
`AdaptiveService` itself raises `TypeError`; concrete subclasses must implement
`adapt(self, change: Change) -> None`. Call `from_environment()` on that subclass
or construct it with explicit identity and clients. Factory and hook-registration
return types preserve the concrete subclass.

The delivery sequence is:

```text
read authorized state -> build baseline or deltas -> injected strategies in order
    -> subclass adapt(change)
    -> matching on_change hooks -> persist checkpoint -> advance cursor
```

`adapt()` is called automatically for the initial baseline, explicit replay
resets and meaningful changes. Heartbeats or reordered fields that produce no
deltas skip adaptation. Use `change.matching("topology")`, `"decision"` or other
prefixes inside the method to select the changes your application can handle.
Additional `on_change()` callbacks run after `adapt()` and can have their own
path and event filters.

Keep `adapt()` short: update routing/admission policy or notify the application's
worker supervisor. That supervisor owns readiness, bounded work queues, worker
replacement and draining, as illustrated in the
[local Soul example](https://github.com/astrivant/polyad/blob/main/docs/workloads/local-soul-searching.md#writing-an-adaptive-application).
The SDK owns the observation loop and starts no application processes or servers.
The script's concrete `AdaptiveService` extends this ABC, delivering local
observations through `refresh()` and `dispatch()` to its `adapt(change)` hook.
Its local snapshot adapter replaces the HTTP events client for the demonstration.

If a strategy or adaptation raises, later components, hooks and checkpointing
wait for a successful retry. Each successful component is tracked separately;
retrying the pending change in the same instance skips completed components.
Partial application effects still need idempotency across retries and restarts.
Construction performs no network calls and never invokes `adapt()`.

The SDK also exposes `EventSource`, `ThroughputReporter` and
`ConnectionNegotiator` ABCs. `Client` implements all three. `AdaptiveService`
accepts them independently through `events`, `api` and `connections`, and
`Subscription` accepts an `EventSource`. Custom event transports inherit the
standard `subscribe()` implementation and its hook/checkpoint behavior. See
[Python extension interfaces](https://github.com/astrivant/polyad/blob/main/docs/development/python-interfaces.md#sdk-observation-and-actions)
for required methods and lifecycle responsibilities.

### Adaptation strategies

An adaptation strategy handles one part of your service's response to change.
For example, it can refresh the list of consumers a producer sends to, or check
available memory before starting another worker. The SDK reports changes; your
application decides how to respond using its existing routing and processing code.
The [producer and consumer introduction](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptation-strategies.md#start-with-a-producer-and-consumer)
defines the terms used below with a familiar HTTP service example.

Supply modular application policies with `strategies=[...]` on the constructor
or `from_environment()`. Each implements the `AdaptationStrategy` ABC's
`adapt(change, current)` method. The sequence is copied at construction; strategies
run in that order before the service's required `adapt(change)` method.

**A nonempty strategy set is required by default.** Define the components before
constructing the service; omitted or empty strategies raise `ValueError` before
observation startup. Each service chooses the categories it handles. Custom
strategies are accepted through the same ABC, and one strategy is sufficient.

Set `require_strategies=False` explicitly to permit an empty set. This opt-out
results in **undefined adaptation behavior for uncovered cases**. It disables
only the nonempty requirement; component types, unique constraint names,
identity, authorization and observation validation remain enforced. The flag is
a Python Boolean and is never inferred from the environment.

The SDK includes constraint strategies for stale topology (`FreshnessStrategy`),
lost or unready peers (`PeerAvailabilityStrategy`), temporary connection consent
and expiry (`ConnectionPermissionStrategy`), rollout overlap or scaling capacity
(`ResourceBudgetStrategy`), local subprocess capacity (`ContainerBudgetStrategy`), and unresolved Soul searching decisions
(`DecisionGuardStrategy`). Each publishes an independently named
`ConstraintAssessment`: satisfied means the check passed, blocked means a
condition is unmet, and unknown means information is missing or unusable. These
checks are also called guards. Your application requires every relevant check
to pass before accepting or sending new work. Evaluate them against `service.view`
immediately before acting, then reserve any needed capacity in your scheduling code.
For temporary connections, the [permission walkthrough](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptation-strategies.md#wait-for-permission-to-use-a-new-connection)
explains the request record, approval, expiry and the application's health checks.

`ThresholdStrategy` proposes approved application profiles using resource
thresholds with hysteresis. The service retains readiness, cooldown and draining
responsibilities. `ObserveStrategy` logs change paths; `CallbackStrategy` and its
`TopologyStrategy`, `ResourceStrategy` and `DecisionStrategy` selectors compose
existing application callbacks. All are available from `polyad_sdk` and
`polyad_sdk.symbiosis`.

See [adaptation strategies for application constraints](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptation-strategies.md)
for checks to use during common changes, a service example, tuning and the
`ConstraintStrategy.evaluate(current)` extension contract.
Start with [choosing an application adaptation](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptation-strategies.md#choose-an-application-adaptation)
for backpressure, rerouting, concurrency changes, local worker scaling and rolling
replacement, then use the [strategy catalog](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptation-strategies.md#choose-an-sdk-strategy)
to select the callbacks, guards and profile policy that implement it.

### Identity, permissions and freshness

`from_environment()` reads `POLYAD_GRAPH_NAMESPACE`, `POLYAD_GRAPH_KIND`,
`POLYAD_GRAPH_NAME`, `POLYAD_GRAPH_UID` and `POLYAD_NODE_NAME`, plus
`POLYAD_EVENTS_URL`. Supply the application's events credential through
`POLYAD_EVENTS_TOKEN` or `POLYAD_EVENTS_TOKEN_FILE`. Pass `cluster="west"` when
selecting a registered cluster through the root events Service. Each instance
tracks one graph-node identity and one cluster's replay cursor.

Optional API and connections clients read their respective `POLYAD_API_*` and
`POLYAD_CONNECTIONS_*` URL, token and token-file settings. For connections, the
SDK also recognizes the projected `/var/run/polyad-connections/token` file.
Token files are reread for each request. API keys, service-account tokens and
operator access modes keep their existing permissions; subscribing grants no
additional rights. `allow_unauthenticated=True` supports administrator-enabled
authentication-free demos for endpoints that permit them.

Outside a managed workload, construct your concrete subclass, for example
`NeighborhoodService(identity, events, strategies=[ObserveStrategy()], api=api_client, connections=connections_client)` with a `polyad_types.ServiceEndpoint`
and separately authorized `Client` instances. Discovery elsewhere in the atlas
remains available through `Client.discover()` and `Client.services()`.

`service.view` evaluates freshness on every read. Stale or invalid topology
makes `available=False` and `candidates=()`, while retaining the last observed
structure for diagnostics and draining. Metrics remain `None` until received,
and cached resource observations expire independently. Their cache age measures
time since receipt; application measurement timestamps remain in the underlying
status and must also be checked, especially after replay.

Topology refreshes occur when its events arrive or when an event/heartbeat arrives
after `refresh_seconds`. Configure the client's read timeout above the operator's
heartbeat interval; choose `max_age_seconds` to accommodate both heartbeat and
operator snapshot publication intervals. Intervals accept 0.1–300 seconds with
refresh no longer than maximum age. Both inventories accept 1–4096 entries;
exceeding a limit fails explicitly. `max_event_bytes` is also configurable on
`from_environment()` or `Client`; the default is 1 MiB. These application settings
can narrow the [operator event budgets](https://github.com/astrivant/polyad/blob/main/docs/apis/event-contract.md).

### Hooks, recovery and explicit actions

Register additional hooks before running. They follow the required `adapt()`
method. `paths` selects delta prefixes; `match` accepts the existing composable
event filters, including exact and regex field matches:

```python
from polyad_sdk.events.filters import event_type, field


def audit_decision(change: Change) -> None:
    log.info("Decision deltas: %s", change.matching("decision"))


service.on_change(
    audit_decision,
    paths=("decision",),
    match=event_type("graph") & field("name", regex=r"^pipeline"),
)
```

A raw event filter skips a snapshot baseline, which has no triggering event. Hooks
run serially on the calling thread; keep them bounded and hand business work to
the application's own workers. A failed adaptation or hook stops consumption without advancing
`service.cursor`. Retrying `run()` on the same instance finishes that pending
change, skips its already successful hooks, refreshes topology and resumes after
the last successful cursor. The optional `checkpoint` callback runs after all
matching hooks succeed; persistence failures also retain the pending change.
Application side effects need their own durable idempotency contract across
process restarts. A new SDK instance starts from a fresh topology baseline; its
in-memory receipts and measurement cache are populated by subsequent events.

When an `AdaptationReporter` is configured (the API client created by
`from_environment()` supplies it), the SDK brackets every injected strategy with
`Running` and `Succeeded` or `Failed` reports. The operator persists active calls
on the projected Workload or Daemon definition under `status.adaptation` and sets
its generic `status.progressing` flag. Generated Argo CD and Flux health checks
therefore report that definition as Progressing while application adaptation is
executing, without changing the containing Graph's health. Reports are fenced to
both the graph and definition UIDs and use a stable invocation identity across an
in-process retry.

Transport errors and stream controls propagate to the application's supervisor.
After a reset or HTTP 410, explicitly call `refresh(reset=True)` before resuming;
it clears incomplete cached measurements and receipts and establishes a new
baseline. Resolve any failed pending hook first. Recover active connection receipts
from application-owned durable request IDs using `Client.connection()` as needed.
Ordinary refreshes preserve the old replay cursor so they do not skip events.
`Settings(rebalance=True)` enables the existing managed subscription reconnection
protocol. `stop()` requests shutdown; an active read ends on data/heartbeat or its
configured timeout. The SDK starts no web server or background threads.

A `Change` describes the observation being handled, including on a retry. Read
`service.view` again before issuing new work or acting after a delay: it evaluates
current freshness and grant expiry, while a saved `change.after` remains the
immutable historical context for that observation.

Actions remain explicit:

- `service.connect(target, request_id=..., ttl_seconds=..., ports=...)` proposes
  a connection from the configured identity through the connections client.
- `service.respond(receipt_uid, "Approve" | "Reject")` answers an observed,
  unexpired receipt after the application checks its policy. Hooks never approve
  automatically, and the operator still enforces consent and GraphRules.
- `service.report_throughput(sample)` submits an application-measured
  `polyad_types.ThroughputSample` for the configured boundary through the API
  client. The designated reporter supplies aggregation, generation, unit and
  measurement window.
- `service.report_service_level(report)` submits a typed `ServiceLevelReport`
  for the projected Daemon definition. The SDK checks its graph and definition
  identity before sending; the operator repeats UID and generation fences and
  rejects stale, replayed or overlapping observation windows. Availability,
  latency, throughput and required capabilities remain independent from the
  definition's `Progressing` adaptation state.

For cooperative producer/consumer behavior, see
[Service Symbiosis: writing adaptive microservices](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptive-microservices.md#use-the-python-sdk).
The [service-level objective guide](../../docs/workloads/service-level-objectives.md)
defines accounting and status semantics for adaptive Daemons.

## Telemetry and subprocess plans

`Telemetry` shares OpenTelemetry traces and metrics across the SDK and application
code. Pass `telemetry=` to `AdaptiveService`, `Client` and `ProcessSupervisor`.
The SDK records operation counts/durations, adaptation stages and owned worker
counts. Application metrics use `telemetry.meter`; spans use
`telemetry.operation(...)` or `telemetry.tracer`. `Telemetry()` reuses existing
providers; explicit `Telemetry.otlp(...)` starts HTTP exporters. Call `close()`
after all work stops to flush providers created by the SDK.

`ProcessSpec` defines a command or Python module with readiness and drain
callbacks. Named `ProcessPlan` objects define approved worker profiles and guards.
An adaptation strategy calls `ProcessSupervisor.propose(profile)`; the application's
supervisor thread calls `reconcile()` to check current constraints, start ready
replacements, commit routing and drain retired children. Duplicate proposals
coalesce and unchanged workers are reused. Trace context follows the proposal
into reconciliation and newly constructed children.

See [SDK telemetry and subprocess plans](https://github.com/astrivant/polyad/blob/main/docs/workloads/sdk-runtime.md)
for configuration, lifecycle limits and integration examples, or run the
[finite worker-plan demo](https://github.com/astrivant/polyad/blob/main/examples/sdk-process-plans.py).

## Reachability and symbiosis models

`polyad_sdk.symbiosis.reachability` provides `QueueModel`, `Interaction`,
`Relationship`, `Envelope`, `compile_envelope`, `Observation` and
`ReachabilityStrategy`. Model the capacity effects of mutualism, parasitism,
competition and other relationships, then check a proposed routing split against
an explicit finite queue contract. The runtime guard extends `ConstraintStrategy`
and uses the standard library; it reads a compact prepared envelope and current
application measurements.

Install `pip install 'polyad-sdk[reachability]'` for the optional
`polyad_sdk.symbiosis.reachability.hj.analyze` backend. Numerical calculations run
in a spawned process with grid/workspace preflight limits and a wall deadline.
The SDK's normal imports and guard checks do not load JAX. See the
[modeling and resource guide](https://github.com/astrivant/polyad/blob/main/docs/workloads/reachability.md)
and [repeatable studies](https://github.com/astrivant/polyad/blob/main/studies/README.md).

The [Kubernetes adaptation guide](https://github.com/astrivant/polyad/blob/main/docs/workloads/kubernetes-adaptation.md)
maps provisioning delays, transport failures, rollouts and resource pressure to
the strategy ABCs, guards and application-owned mutations.

## Observations

Optional shared read replicas expose `observe(name, kind="Graph")` on a separate
observer Service. Construct a client with that Service's URL and read credential
to retrieve cluster identity, observation time, topology and execution metrics.
Observers have no execution authority. See
[observer configuration](https://github.com/astrivant/polyad/blob/main/docs/deployment/multicluster.md#optional-shared-observers).

## Activation

```python
import os
from polyad_sdk import Client

client = Client(
    os.environ["POLYAD_API_URL"],
    os.environ["POLYAD_API_TOKEN"],
)
receipt = client.activate(
    request_id="batch-42",
    graph=os.environ["POLYAD_GRAPH_NAME"],
    graph_uid=os.environ["POLYAD_GRAPH_UID"],
    kind=os.environ["POLYAD_GRAPH_KIND"],
    node="process-batch",
)
status = client.activation("batch-42")
# Explicitly stop a long-running activation when its service is no longer needed:
client.stop("batch-42")
```

Inside a managed workload, Polyad injects the graph instance identity and enabled
operator endpoint URLs into every declared application and init container.
`process-batch` is the downstream target in that graph; `POLYAD_NODE_NAME`
identifies the calling workload's own node. Supply `POLYAD_API_TOKEN` explicitly
from an authorized Secret. See [workload environment](https://github.com/astrivant/polyad/blob/main/docs/workloads/workload-environment.md)
for ancestry, Pod identity, activation IDs and the full variable contract.
Outside managed Pods, supply the operator URL and graph instance identity yourself.

## Composition and request handling

`compose(document)` submits ID-addressed graph definitions and accepts either a
`polyad_types.CompositionRequest` or a dictionary. `connect(document)` likewise
accepts `polyad_types.ConnectionRequest` or a dictionary. Requests and streamed
`Event` values use the same classes as the operator. See the
[shared model examples](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/README.md).
`composition(request_id, resources=True)` returns generated resource names and UIDs.
Use those instance identities when activating nodes. `openapi()` reads the service
schema. HTTP failures raise `APIError` with `status` and `body`; transport failures
raise standard-library network exceptions. Requests have a finite configurable
timeout and no automatic retries. Retry uncertain submissions with the **same
request ID and content**. Redirects are rejected to keep bearer credentials at
the configured endpoint. Use HTTPS when connecting through an external gateway.

## Events and topology

For events, use a separate client with the events Service URL and events token:

```python
events = Client(
    "http://polyad-polyad-events.orchestration.svc.cluster.local:8091",
    os.environ["POLYAD_EVENTS_TOKEN"],
    timeout=60,
)
for event in events.events(last_event_id="0-0"):
    print(event.event, event.data)
    # Persist event.id after processing; reuse it when reconnecting.
```

The event feed is at least once. Deduplicate graph observations by resource UID
and resource version. `topology()` reads current graph neighbors and returns a
cursor for `events(last_event_id=...)`. Use the events Service and its token for
both methods. For a managed workload:

```python
view = events.topology(
    kind=os.environ["POLYAD_GRAPH_KIND"],
    graph=os.environ["POLYAD_GRAPH_NAME"],
    graph_uid=os.environ["POLYAD_GRAPH_UID"],
    node=os.environ["POLYAD_NODE_NAME"],
)
print(view["incoming"], view["outgoing"])
```

Topology notifications include ReplicaGroup scaling, connection edits and changes
to observed execution membership. Fetch the latest snapshot on a `topology`
event for the relevant graph UID; compare its revision to the last snapshot applied
by your application. Topology events can share a graph resource version. See
[workload topology events](https://github.com/astrivant/polyad/blob/main/docs/workloads/workload-events.md) for startup and recovery.

Handle `reset` by refreshing the snapshot and cursor; reconnect explicitly
after `unavailable`, disconnects or timeouts. HTTP 410 means the cursor expired.
Closing the iterator closes its connection. API tokens remain namespace-scoped;
cross-namespace callers also need the corresponding network and identity grants.

See the repository's [activation guide](https://github.com/astrivant/polyad/blob/main/docs/workloads/activation.md) and
[networking guide](https://github.com/astrivant/polyad/blob/main/docs/deployment/networking.md) for policies and deployment settings.

## Event types and size limits

`Event.typed()` returns a validated `GraphEvent`, `TopologyEvent`,
`ConnectionEvent`, `ControlEvent` or `HeartbeatEvent` from `polyad_types`.
Existing callbacks can continue using the raw `data` dictionary. Import
`decode_event` from `polyad_types` for independent documents. For JSON Schema,
install `polyad-schemas` and import `event_schema` from `polyad_schemas.events`;
this does not install the operator.

Choose a maximum complete event size for either transport:

```python
events = Client("http://polyad-polyad-events:8091", token, max_event_bytes=2 * 1024 * 1024)
settings = events.event_settings()
assert settings.maxEventBytes <= events.max_event_bytes
```

The default is 1 MiB, with an allowed range of 1 KiB–16 MiB. The limit counts UTF-8
bytes including the event envelope/framing. `EventTooLarge` is a `ValueError`
subclass and closes the stream without checkpointing an oversized observation.
Reading server settings never raises the client limit automatically. See the
[event ASTs, schemas and Helm tuning](https://github.com/astrivant/polyad/blob/main/docs/apis/event-contract.md)
for supported payloads, validation, replay tuning and recovery.

## WebSocket subscriptions

`polyad-sdk` includes the `websockets` dependency. SSE remains the default.
When the operator enables `events.websockets.enabled`, select WebSocket transport
on the same events Service:

```python
for event in events.events(transport="websocket", last_event_id="0-0"):
    print(event.event, event.data)
    # Persist event.id only after successful processing.

# The same transport works with filtered callbacks:
subscription = events.subscribe(transport="websocket", cursor="0-0")
```

Use an HTTP(S) base URL as usual; the client selects WS(S) for subscriptions.
Credentials and replay cursors use handshake headers. Heartbeats are handled
internally; events, filters, callbacks and checkpoint behavior match SSE.
Reconnection is explicit by default: reuse the last processed cursor, or refresh
topology after `reset`/HTTP 410. If the operator enables event rebalancing, use
`events.subscribe(transport="websocket", rebalance=True)` to rediscover ready
replicas and resume automatically after copulses or transient transport errors.
Call `subscription.stop()` during application shutdown. See
[copulses, Istio and direct client routing](https://github.com/astrivant/polyad/blob/main/docs/operations/event-rebalancing.md).
Neither transport automatically approves connections.
Both share the operator's subscriber ceiling and API-key concurrency lanes.
See [WebSocket enablement and protocol](https://github.com/astrivant/polyad/blob/main/docs/workloads/workload-events.md#websocket-subscriptions).

## Discovery and hooks

`discover()` reads permitted live graph services and replay cursors. `services()`
walks their authorized child graphs. `subscribe().on(filter, callback)` dispatches
observations on the caller's thread; `event_type`, `graph`, `phase`, `field` and
`connection_pending` compose with `&`, `|` and `~`. Field filters support trusted
application regex patterns and collection traversal. Callbacks checkpoint only
after success; applications own reconnection and durable idempotency.

Use `connect_services(ServiceConnectionRequest(...))` to negotiate exact discovered
endpoints at their common application boundary, including across clusters when
administrator modes allow it. Set `identity_cluster` and a rotating
`token_provider` for projected-token authentication at the root. A request counts
as the source service's consent; the target responds explicitly.

See [Atlas discovery and service connections](https://github.com/astrivant/polyad/blob/main/docs/apis/discovery.md)
for runnable hook patterns, every access mode, error handling and mesh requirements.

## Temporary connection consent

Subscribe to the relevant graph's events before proposing a connection. A
`connection` event includes `event.data["connection"]`, a public receipt with
its server-assigned name, UID, endpoints, deadline and consent summary. The
requester's verified proposal counts as its own consent. The other endpoint
must decide whether it can accept the connection and explicitly respond:

```python
from pathlib import Path
from polyad_types import ConnectionResponse

proposal = event.data["connection"]
# Refresh the responding workload's projected token before the operation.
connections = Client(
    os.environ["POLYAD_CONNECTIONS_URL"],
    Path("/var/run/polyad-connections/token").read_text().strip(),
)
connections.respond_connection(
    proposal["namespace"], proposal["name"],
    ConnectionResponse(uid=proposal["uid"], decision="Approve"),
)
```

Use `Reject` to refuse. The application chooses the decision; the client does
not automatically approve events. The responder needs graph-specific `approve`
permission and a live Pod belonging to the proposed endpoint. An events API key
cannot stand in for this identity. Both services must respond if a third party
made the request. Missing consent expires at the proposal's original deadline.
See [service consent and administrator limits](https://github.com/astrivant/polyad/blob/main/docs/apis/temporary-connections.md#service-consent)
for RBAC, retries, HTTP 429 cooldowns and graph-layer semantics.

## Remote clusters

With a [root control plane](../../docs/deployment/root-control-plane.md), pass `cluster="west"`
to `topology()` and `events()` when reading a registered remote cluster through the
root event endpoint. Replay cursors belong to their selected cluster stream.

## Report throughput to Soul searching

The client also exposes `report_throughput(ThroughputSample(...))` for
[Soul searching](../../docs/graphs/soul-searching.md#report-measurements). Its key needs
the `throughput` capability and an explicit grant to the measured graph tree.

## Publishing

See [manual PyPI publishing](https://github.com/astrivant/polyad/blob/main/docs/development/toolchain.md#manual-pypi-publishing) for Poetry release commands.
