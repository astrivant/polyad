# Polyad client

A typed Python 3.11–3.14 client for Polyad's composition, activation, event and temporary connection APIs.

`connect(document)`, `connection(namespace, request_id)` and
`disconnect(namespace, request_id)` use the separate connections Service and a
projected service-account token. See the [temporary connections guide](https://github.com/astrivant/polyad/blob/main/docs/temporary-connections.md)
for token rotation, namespace scope, TTL and cleanup semantics.
It uses the shared [polyad-types](https://github.com/astrivant/polyad/blob/main/pkg/polyad-types/README.md) models and
does not install the operator.

Install from a checkout:

```sh
pip install ./pkg/polyad-types ./pkg/client
```

Release CI builds and publishes `polyad-client` separately from `polyad`.
Once that release is available, install it with `pip install polyad-client`.

Optional shared read replicas expose `observe(name, kind="Graph")` on a separate
observer Service. Construct a client with that Service's URL and read credential
to retrieve cluster identity, observation time, topology and execution metrics.
Observers have no execution authority. See
[observer configuration](https://github.com/astrivant/polyad/blob/main/docs/multicluster.md#optional-shared-observers).

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
from an authorized Secret. See [workload environment](https://github.com/astrivant/polyad/blob/main/docs/workload-environment.md)
for ancestry, Pod identity, activation IDs and the full variable contract.
Outside managed Pods, supply the operator URL and graph instance identity yourself.

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
[workload topology events](https://github.com/astrivant/polyad/blob/main/docs/workload-events.md) for startup and recovery.

Handle `reset` by refreshing the snapshot and cursor; reconnect explicitly
after `unavailable`, disconnects or timeouts. HTTP 410 means the cursor expired.
Closing the iterator closes its connection. API tokens remain namespace-scoped;
cross-namespace callers also need the corresponding network and identity grants.

See the repository's [activation guide](https://github.com/astrivant/polyad/blob/main/docs/activation.md) and
[networking guide](https://github.com/astrivant/polyad/blob/main/docs/networking.md) for policies and deployment settings.

See [manual PyPI publishing](https://github.com/astrivant/polyad/blob/main/docs/toolchain.md#manual-pypi-publishing) for Poetry release commands.

With a [root control plane](../../docs/root-control-plane.md), pass `cluster="west"`
to `topology()` and `events()` when reading a registered remote cluster through the
root event endpoint. Replay cursors belong to their selected cluster stream.
