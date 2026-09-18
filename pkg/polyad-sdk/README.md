# Polyad SDK

A typed Python 3.11–3.14 SDK for **Service Symbiosis** and Polyad's operator APIs.
Service Symbiosis lets microservices discover compatible peers and adapt their
relationships and work together across Graphs and PolyGraphs.
Applications receive connection, capacity and decision deltas with current context.
The same package includes `Client` for composition, activation, discovery, events
and temporary connections.

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
- [Adaptive services and deltas](#adaptive-services-and-deltas)
  - [Subclass contract](#subclass-contract)
  - [Identity, permissions and freshness](#identity-permissions-and-freshness)
  - [Hooks, recovery and explicit actions](#hooks-recovery-and-explicit-actions)
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

## Adaptive services and deltas

`AdaptiveService` is an abstract base class for Service Symbiosis. Implement
`adapt(change)` in a subclass to respond to authorized neighborhood changes.
The inherited runtime maintains immutable context, constructs **deltas** and
handles stream checkpoints. `Client` remains available for direct API calls and
raw event subscriptions in this same package.

```python
import logging

from polyad_sdk import AdaptiveService, Change, Settings

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
no deltas. A missing measurement remains unknown. A later observation of three
Pods becoming five yields `before=3`, `after=5`, `difference=2`. A newly observed
metric is `added`, with `difference=None`; it did not increase from an assumed
zero. Numeric differences are arithmetic changes, not elapsed-time rates. Compare
units, measurement windows and generations in the surrounding views before using
them for application control.

Paths use stable logical names and execution UIDs. Reordering lists, changing an
event cursor or refreshing observation timestamps does not trigger a change hook.
Replacing an execution UID produces removal and addition, even if its name is the
same. New desired peers may initially have no running execution.

| Change prefix | Data and application use |
| --- | --- |
| `topology.outgoing` / `topology.incoming` | Peer membership and allowed ports; refresh eligible producer/consumer relationships |
| `topology.node.executions` | This node's execution membership, termination and replica-count changes |
| `topology.dependencies` / `topology.dependents` | Changes in prerequisite relationships |
| `resources` | Observed containing-graph resource metrics; assess capacity changes |
| `decision` | Soul searching phase, measured demand, Cheeger values and current/proposed traffic or capacity |
| `observations.<uid>` | Public status and resource metrics for a known neighborhood execution |
| `connections.<uid>` | Negotiation, consent and lifetime changes; expire local grants |
| `available` / `reason` | Whether topology is fresh and admits considering new assignments |

For example, a replica-count delta may have the path
`topology.outgoing.sink.node.executions.uid-sink.replicas`.
`change.matching("decision")` selects decision deltas. Matching also includes an
added/removed containing object, so a filter for `resources.pods` observes the
first resource snapshot and its expiry. Values in `before` and `after` are
recursively read-only; the triggering `event` is an independent callback copy.

`change.after.decision` preserves `Recommended`, `Applied` and other operator
phases, including `currentTraffic` versus `proposedTraffic`. A recommendation is
input for preparation; a usable path also needs admission, ready application
transport and compatible work. `view.candidates` lists desired outgoing peers
with a nonterminating execution and a positive replica count when supplied. The
application checks readiness, protocol compatibility, ownership and its own
admission budgets before assigning work. Use Istio's configured route when Istio
owns traffic percentages.

### Subclass contract

Import the same ABC from `polyad_sdk` or `polyad_sdk.adaptive`. Instantiating
`AdaptiveService` itself raises `TypeError`; concrete subclasses must implement
`adapt(self, change: Change) -> None`. Call `from_environment()` on that subclass
or construct it with explicit identity and clients. Factory and hook-registration
return types preserve the concrete subclass.

The delivery sequence is:

```text
read authorized state -> build baseline or deltas -> subclass adapt(change)
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

If adaptation raises, later hooks and checkpointing wait for a successful retry.
If adaptation succeeds but a later hook or checkpoint fails, retrying the pending
change in the same instance skips the already successful adaptation. Partial
application effects still need idempotency across retries and process restarts.
Construction performs no network calls and never invokes `adapt()`.

The SDK also exposes `EventSource`, `ThroughputReporter` and
`ConnectionNegotiator` ABCs. `Client` implements all three. `AdaptiveService`
accepts them independently through `events`, `api` and `connections`, and
`Subscription` accepts an `EventSource`. Custom event transports inherit the
standard `subscribe()` implementation and its hook/checkpoint behavior. See
[Python extension interfaces](https://github.com/astrivant/polyad/blob/main/docs/development/python-interfaces.md#sdk-observation-and-actions)
for required methods and lifecycle responsibilities.

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
`NeighborhoodService(identity, events, api=api_client, connections=connections_client)` with a `polyad_types.ServiceEndpoint`
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
from polyad_sdk.filters import event_type, field


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

For cooperative producer/consumer behavior, see
[Service Symbiosis: writing adaptive microservices](https://github.com/astrivant/polyad/blob/main/docs/workloads/adaptive-microservices.md#use-the-python-sdk).

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
