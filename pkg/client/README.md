# Polyad client

A typed Python 3.11–3.14 client for Polyad's composition, activation, event and temporary connection APIs.

`connect(document)`, `connection(namespace, request_id)` and
`disconnect(namespace, request_id)` use the separate connections Service and a
projected service-account token. See the [temporary connections guide](https://github.com/astrivant/polyad/blob/main/docs/apis/temporary-connections.md)
for token rotation, namespace scope, TTL and cleanup semantics.
It uses the shared [polyad-types](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/README.md) models and
does not install the operator.

## Table of contents

- [Installation](#installation)
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
pip install ./pkg/polyad-types ./pkg/client
```

Release CI builds and publishes `polyad-client` separately from `polyad`.
Once that release is available, install it with `pip install polyad-client`.

## Observations

Optional shared read replicas expose `observe(name, kind="Graph")` on a separate
observer Service. Construct a client with that Service's URL and read credential
to retrieve cluster identity, observation time, topology and execution metrics.
Observers have no execution authority. See
[observer configuration](https://github.com/astrivant/polyad/blob/main/docs/deployment/multicluster.md#optional-shared-observers).

## Activation

```python
import os
from polyad_client import Client

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
`decode_event` for independent documents and `event_schema` for the packaged JSON
Schema, without installing the operator.

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

`polyad-client` includes the `websockets` dependency. SSE remains the default.
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
Reconnection is explicit: reuse the last processed cursor, or refresh topology
after `reset`/HTTP 410. Neither transport automatically approves connections.
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
