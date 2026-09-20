# Optional workload connection protocols

The SDK can open HTTP(S), WebSocket, gRPC, AMQP 0-9-1, Redis and raw TCP/TLS
connections between workloads. These are **application connections**, not new
operator API protocols. Installing a client does not deploy a broker, expose a
port, enable an operator endpoint or grant network access. Existing applications
and chart defaults are unchanged.

## Choose an optional install

```sh
pip install 'polyad-sdk[grpc]'       # grpcio; bring your generated application stubs
pip install 'polyad-sdk[amqp]'       # Pika, AMQP 0-9-1 (not AMQP 1.0)
pip install 'polyad-sdk[redis]'      # redis-py, also suitable for compatible servers
pip install 'polyad-sdk[protocols]'  # all three optional clients

# From this checkout:
pip install ./pkg/polyad-types './pkg/polyad-sdk[grpc,amqp]'
```

The base SDK does not depend on grpcio, Pika or Redis. They are imported only
when their transport is opened; a missing library raises an error naming the
appropriate extra. WebSockets already belong to the base SDK's event-stream
dependencies, so workload WebSockets require no additional install. HTTP and
raw TCP/TLS use Python's standard library. None starts at import or construction.

## Connect after permission is active

Service discovery returns a **logical identity**, not a verified application
URL. Supply the address from trusted deployment configuration and associate it
with the discovered peer. Do not turn untrusted message contents into endpoints.

```python
from polyad_sdk import WorkloadEndpoint

# peer is a ServiceEndpoint returned by authorized discovery; service is your
# running AdaptiveService. This Service maps port 50051 to Pod port 50052.
endpoint = WorkloadEndpoint(peer, "grpc://worker.apps.svc:50051", target_port=50052)
receipt = service.connect(
    peer,
    request_id="worker-rpc-1",
    ttl_seconds=300,
    ports=(endpoint.network_port,),
)
transport = service.workload(endpoint, receipt_uid=receipt["uid"], timeout=5)

# The event loop must first observe Active. Pending, stale, expired and revoked
# receipts raise PermissionError; do not catch that error and send anyway.
with transport.grpc_channel() as channel:
    stub = WorkerStub(channel)  # Generated from your application's .proto file.
    transport.check()
    result = stub.Run(request, timeout=2)  # Every RPC needs its own deadline.
```

`service.workload()` checks topology freshness and the current receipt's UID,
Active status, revocation, expiry, exact participant identities, direction and
TCP **Pod port** before and after each handshake. A reverse connection requires
a bidirectional receipt. Atlas identities include cluster and graph UID.
These are admission checks over observed state, not an attestation that a URI
belongs to a peer. Kubernetes policies and Istio identities enforce network
boundaries. Correct deployment-owned identity/address mapping is essential.

For static graph edges or local process demos, explicitly construct
`WorkloadClient(endpoint, authorize=your_admission_check)`. Without a callback,
that constructor performs **no receipt check**; static network policy and
application admission are your responsibility. Loopback endpoints are allowed
for these explicitly configured local demonstrations.

Native clients remain open until their context exits. Call `transport.check()`
before admitting each unit of work, stop on denial, and close the context during
adaptation or shutdown. There is no hidden watcher or per-message interceptor,
and expiry does not forcibly interrupt an already-running RPC or established
socket. Network-policy changes also must not be treated as a portable mechanism
for terminating existing connections. Your observation loop must keep running.

## Application APIs and lifecycle

Each method is a synchronous context manager; it closes its connection on exit,
including when a post-handshake admission check fails. Operator bearer tokens
are never copied into workload headers, broker passwords or RPC metadata.

| Endpoint scheme | Method and native object | Application responsibilities |
| --- | --- | --- |
| `http`, `https` | `http()` → `http.client.HTTPConnection` | Request path, application headers, bounded response reads |
| `ws`, `wss` | `websocket(headers=..., subprotocols=...)` → WebSocket connection | Message schema, authentication, receive timeout, delivery acknowledgment |
| `grpc`, `grpcs` | `grpc_channel()` → `grpc.Channel` | Generated stubs, per-RPC deadlines, application metadata, idempotency |
| `amqp`, `amqps` | `amqp(username=..., password=..., virtual_host=...)` → Pika `BlockingConnection` | Channels, prefetch, publisher confirms, acknowledgments, heartbeats |
| `redis`, `rediss` | `redis(username=..., password=..., database=...)` → `redis.Redis` | Commands, bounded payloads, ACLs, stream delivery semantics |
| `tcp`, `tls` | `tcp()` → socket | Framing, bounded buffers, application protocol and authentication |

HTTP, WebSocket and gRPC proxies are disabled for these workload connections;
HTTP and WebSocket redirects are not followed. Client-level gRPC retries and
Redis command retries are disabled, and AMQP makes one connection attempt.
Native clients can still reconnect at the transport level; this SDK does not
provide exactly-once delivery. Reconnection and ambiguous operation outcomes
need application policy and idempotency keys.

`timeout` bounds socket/connection waits, not an entire business transaction.
AMQP permits up to twice that value for the overall protocol stack handshake.
Use gRPC deadlines and WebSocket `recv(timeout=...)`. OS DNS resolution may have
its own timing. `max_message_bytes` bounds gRPC send/receive messages and incoming
WebSocket messages, not HTTP bodies, AMQP queues or Redis values. The Redis pool
is limited to one connection; it does not implement Sentinel or Cluster discovery.
Use separate clients when a blocking consumer and a producer need independent
connections.

```python
from polyad_sdk import WorkloadClient, WorkloadEndpoint

transport = WorkloadClient(
    WorkloadEndpoint(peer, "ws://worker.apps.svc:8080/jobs"),
    authorize=your_admission_check,
    timeout=5,
)
with transport.websocket(subprotocols=("jobs.v1",)) as socket:
    transport.check()
    socket.send("application-owned payload")
    reply = socket.recv(timeout=2)
```

HTTP/WebSocket URLs may contain a path. Native HTTP requests still supply their
own path to `request()`. Credentials, query strings and fragments are rejected;
pass application credentials as headers or method arguments instead. AMQP
virtual hosts and Redis database numbers are method arguments, not URL paths.
Raw TCP/TLS needs an explicit port. Other defaults are HTTP/WS 80, HTTPS/WSS 443,
gRPC/gRPC-TLS 50051, AMQP 5672, AMQP-TLS 5671 and Redis/Redis-TLS 6379.

## Declare the protocol to Istio

All supported transports use Kubernetes `protocol: TCP`, but that field does
**not** tell Istio which application protocol to inspect. Declare `appProtocol`
and a matching port-name prefix. AMQP and Redis use explicit opaque TCP here;
no experimental Redis filter is enabled. This also avoids protocol-detection
ambiguity for broker traffic. See [Istio protocol selection](https://istio.io/latest/docs/ops/configuration/traffic-management/protocol-selection/).

| Application | `appProtocol` / name prefix | What the mesh can inspect |
| --- | --- | --- |
| HTTP, plaintext WebSocket | `http` | HTTP requests / upgrade handshake, not individual WebSocket messages |
| Plaintext gRPC | `grpc` | HTTP/2 RPC traffic |
| Application HTTPS, WSS | `https` | Opaque encrypted connection in a sidecar |
| Application gRPC-TLS, AMQP-TLS, Redis-TLS, raw TLS | `tls` | Opaque encrypted connection |
| AMQP, Redis, raw TCP | `tcp` | Connection-level traffic |

Python's `endpoint.service_port("worker")` produces a Service port entry.
Helm charts with the Polyad named helpers in scope can use the equivalent helper:

```yaml
spec:
  ports:
    - {{ include "polyad.workloadServicePort" (dict "protocol" "grpc" "name" "worker" "port" 50051 "targetPort" 50052) | nindent 6 }}
```

The helper's arguments are `protocol` (required scheme), `port` (required),
`targetPort` (defaults to port; numeric only), and `name` (defaults to workload).
It emits no resources unless called. If your application chart does not include
Polyad's templates, write the same standard Service fields directly; do not
install an additional operator just to obtain this helper. Keep endpoints and
graph connection ports aligned with the actual Service and Pod manifests.

[Reference values](../../charts/polyad/references/values-workload-protocols.reference.yaml)
list the existing operator switches for optional mesh installation, consent and
events. These transports introduce no new global enable switch or CRD fields.
Static graph network access can be used without temporary connections.

Istio mTLS and application TLS are separate. Plaintext `grpc://` behind injected
sidecars can travel over STRICT mesh mTLS while retaining HTTP/2 visibility.
Use `grpcs://` when the application itself terminates TLS. The SDK verifies
certificates and hostnames and offers no insecure TLS bypass. Custom trust and
client certificates use SSL contexts (HTTP/WS/AMQP/TCP), PEM bytes (gRPC), or file
paths (Redis). Supply secrets through your deployment's existing secret mechanism.

Polyad's existing connection policies already compile explicit TCP ports to
NetworkPolicy and, where enabled, Istio identity/port rules. Do not attach HTTP
method/path restrictions to opaque broker/TLS traffic. Mesh HTTP routing,
HTTP error handling and request-level metrics cannot be assumed for opaque TCP.
In particular, this feature does not extend Polyad's HTTP traffic-weight strategy
to per-message AMQP/Redis balancing. Long-lived streams can keep using their
original destination after a weight change; draining remains application-owned.

Cross-cluster connections still need registered clusters, consent, compatible
mesh trust, endpoint DNS/Service export and east-west routing. An SDK extra does
not provision those prerequisites or translate application protocols. See
[multicluster deployment](../deployment/multicluster.md).

MQTT, Kafka and database-specific clients may use separately managed native
libraries and TCP policy declarations, but they are not first-class SDK adapters
in this release. Kafka metadata can introduce additional broker destinations;
one granted bootstrap endpoint is not sufficient. UDP/QUIC is not covered.

## Adaptation and service-level measurements

Network permission and channel readiness are not successful work. Report gRPC
completion, confirmed/acknowledged AMQP work, and completed stream messages
through the existing throughput and service-level APIs. Account for connection
establishment, reconnection, draining, abandoned messages and duplicate work in
your application's latency and availability windows. A protocol change alone
does not improve the SLA; measure the capability and quality delivered.

Library contracts: [gRPC channels](https://grpc.github.io/grpc/python/grpc.html),
[Pika connection parameters](https://pika.readthedocs.io/en/stable/modules/parameters.html),
[Redis connections](https://redis.readthedocs.io/en/stable/connections.html), and
[synchronous WebSockets](https://websockets.readthedocs.io/en/stable/reference/sync/client.html).
