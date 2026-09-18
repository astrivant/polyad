# Temporary connections

Services can request a directed connection between existing nodes of a `Graph`,
`PolyGraph`, or `ReplicaGroup` for a bounded lifetime. Polyad records each request,
obtains the participating services' consent through [events](../workloads/workload-events.md),
checks the live graph family's [GraphRules](../graphs/graph-rules.md), adds admitted edges
to the instance's effective topology, and removes their grants after expiry.
The graph's reusable specification is unchanged. For endpoints in different graph
or cluster boundaries, see [Atlas discovery and negotiation](discovery.md). It adds
exact service identities, inherited operator access modes and per-participant events.

## Table of contents

- [Enable the endpoint and choose its scope](#enable-the-endpoint-and-choose-its-scope)
- [Authenticate workloads](#authenticate-workloads)
- [Submit, observe and revoke](#submit-observe-and-revoke)
- [Service consent](#service-consent)
- [Administrator pulse limits](#administrator-pulse-limits)
- [Meaning at each graph layer](#meaning-at-each-graph-layer)
- [Deadline, retries and cleanup](#deadline-retries-and-cleanup)
- [Implementation boundaries](#implementation-boundaries)

## Enable the endpoint and choose its scope

The listener is disabled by default. Enable it in Helm:

```yaml
operator:
  serviceAccess:
    discovery: Cluster # Namespace event token; use named home-graph keys for narrower modes.
connections:
  enabled: true
  scope: Cluster
  namespace: ''
  maxTtlSeconds: 3600
  retentionSeconds: 3600
events:
  enabled: true
  existingSecret: polyad-events
```

Provision the event credential and subscribe the participating services before
requesting connections. The [connection reference values](../../charts/polyad/values-connections.reference.yaml)
include separate proposal and reconciliation pulse controls. For named API keys,
assign a fixed `home` graph and grant `events` access to the target graph tree; these credentials control event
visibility, while projected service-account tokens identify connection participants.

The internal Service is `<release>-polyad-connections.<operator-namespace>.svc:8093`.
Managed workloads receive its address as `POLYAD_CONNECTIONS_URL`. Outside Helm,
configure `POLYAD_CONNECTIONS_ENABLED`, `POLYAD_CONNECTIONS_SCOPE`,
`POLYAD_CONNECTIONS_NAMESPACE`, `POLYAD_CONNECTIONS_MAX_TTL`, and
`POLYAD_CONNECTIONS_RETENTION`; discovery uses `POLYAD_WORKLOAD_CONNECTIONS_URL`.

| `connections.scope` | Allowed callers **and** target graphs | `connections.namespace` |
| --- | --- | --- |
| `Cluster` (default) | Any namespace in this cluster, subject to graph-specific authorization | Empty |
| `OperatorNamespace` | Only the namespace hosting this operator | Empty |
| `Namespace` | Only the named namespace, even when the API runs elsewhere | Required namespace name |

For example, select `scope: Namespace` and `namespace: analytics` to allow only
service accounts from `analytics` to request connections on graphs in `analytics`.
Both endpoints belong to one target boundary; cluster scope does not introduce
cross-namespace graph edges or arbitrary Kubernetes Service connections.

**Each target namespace needs a running Polyad operator with
`connections.enabled: true`.** Polyad's reconciliation workers remain namespaced.
A cluster-scoped listener can accept requests for those operators, but cannot
reconcile their graphs itself. An absent target operator leaves the receipt
Pending; a disabled target operator rejects new requests. Its scope and TTL
limits also apply. Deploy the CRDs when upgrading before enabling this feature.

When `networkPolicy.enabled` is set, the chart admits port 8093 from namespaces
matching this scope. Caller graph policies must also allow egress to the listener.
With operator mesh authorization enabled, add the exact caller identity to
`mesh.operator.connectionPrincipals`. This listener has no public Gateway route.
See [networking configuration](../deployment/networking.md#optional-chart-networking).

## Authenticate workloads

The API verifies projected Kubernetes service-account tokens with audience
`polyad-connections`. It checks the audience returned by
[TokenReview](https://kubernetes.io/docs/reference/kubernetes-api/definitions/token-review-v1-authentication/)
and uses the verified identity in a
[SubjectAccessReview](https://kubernetes.io/docs/reference/kubernetes-api/definitions/subject-access-review-v1-authorization/).
Composition and events tokens do not authorize connection requests.

Give a caller the `connect` verb on the target graph. This verb is checked by
Polyad; it does not grant permission to patch graphs or create receipts directly.
For a Graph named `pipeline` in namespace `analytics`:

```yaml
apiVersion: v1
kind: ServiceAccount
metadata:
  name: pipeline-worker
  namespace: analytics
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: connect-pipeline
  namespace: analytics
rules:
  - apiGroups: [polyad.astrivant.com]
    resources: [graphs]
    resourceNames: [pipeline]
    verbs: [connect]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: connect-pipeline
  namespace: analytics
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: connect-pipeline
subjects:
  - kind: ServiceAccount
    name: pipeline-worker
    namespace: analytics
```

Use `polygraphs` or `replicagroups` for those boundary types. `resourceNames`
identifies persisted instances, including generated names obtained from
composition audit or [workload identity](../workloads/workload-environment.md). Permission is
for the entire target boundary: authorized callers can select any two nodes in
it, subject to GraphRules. Callers can read or revoke only their own service
account incarnation's requests.

Add this to the workload's Pod template `spec`, mounting the projected token in
each container that calls the API:

```yaml
serviceAccountName: pipeline-worker
containers:
  - name: main
    image: your-registry/worker:1.0
    volumeMounts:
      - name: connection-identity
        mountPath: /var/run/polyad-connections
        readOnly: true
volumes:
  - name: connection-identity
    projected:
      sources:
        - serviceAccountToken:
            path: token
            audience: polyad-connections
            expirationSeconds: 3600
```

Read the file again before subsequent requests so Kubernetes token rotation takes
effect. The connection deadline and the authentication token's lifetime are
independent. The chart grants the operator review privileges and scoped receipt
intake rights; do not grant application service accounts direct write access to
`TemporaryConnection` objects or the operator-owned connection annotation.

## Submit, observe and revoke

Send a request using the **instance** name and UID. For this example, the graph
must already contain nodes `producer` and `consumer`:

```http
POST /v1/connections
Authorization: Bearer <projected-service-account-token>
Content-Type: application/json

{
  "requestId": "handoff-42",
  "namespace": "analytics",
  "kind": "Graph",
  "graph": "pipeline",
  "graphUid": "<persisted-graph-uid>",
  "source": "producer",
  "target": "consumer",
  "ttlSeconds": 300,
  "ports": [{"port": 8080, "protocol": "TCP"}],
  "bidirectional": false
}
```

| Field | Constraint |
| --- | --- |
| `requestId` | 1–128 letters, digits, dots, underscores, colons or hyphens; stable across retries |
| `namespace`, `kind`, `graph`, `graphUid` | Exact persisted boundary incarnation; supported kinds are Graph, PolyGraph and ReplicaGroup |
| `source`, `target` | Distinct existing logical node names in that boundary |
| `ttlSeconds` | Required integer, 1 through the configured maximum; maximum configuration is 86400 seconds |
| `ports` | Optional, at most 32 destination ports from 1–65535; protocol TCP (default), UDP or SCTP |
| `bidirectional` | Optional boolean, default false; true adds the reverse edge with the same port grants |

HTTP 202 acknowledges a durable receipt. Poll
`GET /v1/connections/analytics/handoff-42` for `status.phase`: `Pending`, `Active`,
`Rejected`, `Expired` or `Revoked`. The response includes `expiresAt`, graph identity,
endpoint details, and `revokeRequested`. `Active` means the requested policies
have been observed in Kubernetes. Applications check readiness and network
convergence before exchanging work. Rejections include `status.message`.

`DELETE /v1/connections/analytics/handoff-42` requests early revocation and returns
202. Poll until `Revoked` or `Expired`; an accepted delete is not synchronous
network teardown. Requests outside caller/target scope or without `connect`
permission return 403. Invalid authentication returns 401; conflicting request
identity returns 409. The authenticated schema is at `/openapi.json`.

The [standalone Python SDK](../../pkg/polyad-sdk/README.md) supports these
operations without installing the operator package:

```python
import os
from pathlib import Path
from polyad_sdk import Client


def connections():
    # Refresh the projected credential on each operation.
    token = Path("/var/run/polyad-connections/token").read_text().strip()
    return Client(os.environ["POLYAD_CONNECTIONS_URL"], token)


namespace = os.environ["POLYAD_GRAPH_NAMESPACE"]
receipt = connections().connect({
    "requestId": "handoff-42",
    "namespace": namespace,
    "kind": os.environ["POLYAD_GRAPH_KIND"],
    "graph": os.environ["POLYAD_GRAPH_NAME"],
    "graphUid": os.environ["POLYAD_GRAPH_UID"],
    "source": os.environ["POLYAD_NODE_NAME"],
    "target": "consumer",
    "ttlSeconds": 300,
    "ports": [{"port": 8080}],
})
status = connections().connection(namespace, "handoff-42")
# When the application no longer needs the connection:
connections().disconnect(namespace, "handoff-42")
```

## Service consent

The requesting service's authenticated proposal counts as its consent. It does
not need to approve again. The operator resolves its Pod to a logical endpoint
through current controller ownership, checking the UID at every step. If the
requester represents neither endpoint, both services must respond explicitly.
A service account alone cannot claim a node: consent requires a
[Pod-bound projected token](https://kubernetes.io/docs/reference/access-authn-authz/service-accounts-admin/#verifying-and-inspecting-private-claims).

The existing event stream emits `event: connection` when a receipt changes. Its
`data.graph` identifies the target boundary and `data.connection` contains the
public receipt, including its name, UID, endpoints, original deadline, consent
summary and status. Services subscribe to this graph tree and resume with
`Last-Event-ID` after interruptions. Reserved operator graphs retain their event
isolation. A Pending receipt creates no neighbor or network permission.

Give responding services the separate custom `approve` verb on the exact
boundary, using a Role and RoleBinding like the requester example above:

```yaml
rules:
  - apiGroups: [polyad.astrivant.com]
    resources: [graphs]
    resourceNames: [pipeline]
    verbs: [approve]
```

Respond using the **server-assigned receipt name** from the event, its UID, and
the responding service's projected token:

```http
POST /v1/connections/analytics/connection-<server-generated-hash>/response
Authorization: Bearer <responding-service-projected-token>
Content-Type: application/json

{"uid": "<receipt-uid>", "decision": "Approve"}
```

`decision` is `Approve` or `Reject`. The caller cannot select the endpoint it
approves for; the operator derives it from its live Pod ownership. A named API
key that can read events does not grant approval authority. The standalone SDK
provides the same operation:

```python
from polyad_types import ConnectionResponse

# Within the application's event handler, after checking the proposed peer,
# ports, deadline and its own ability to accept work:
proposal = event.data["connection"]
connections().respond_connection(
    proposal["namespace"],
    proposal["name"],
    ConnectionResponse(uid=proposal["uid"], decision="Approve"),
)
```

Applications decide whether to approve; the client never automatically consents.
The operator rechecks participant identity and permissions before admission,
then evaluates GraphRules and observes the network policies. Missing permission,
an absent service or a lost notification leaves the proposal Pending until the
original TTL expires. There is no timeout bypass or anonymous consent, including
in demonstration mode. Replayed responses do not extend the deadline.
The stream uses bounded replay; a missed event does not imply consent.

Either endpoint can reject with `approve` permission, including after activation;
this removes the grant. A rejected proposal needs a new request ID. Ordinary Pod
replacement after activation does not revoke an agreed logical connection.
Before activation, replacement invalidates that Pod's consent; its replacement
must respond (or the requester must replay its proposal).

Ownership resolution runs in the receipt's cluster. A workload in another
cluster cannot claim a local endpoint through remote-parent annotations; use
the owning cluster's operator and locally verifiable participants.

## Administrator pulse limits

These optional budgets limit new decisions independently of HTTP rate limits,
writer concurrency and the existing [validation windows](../development/write-pipeline.md#configuration):

```yaml
connections:
  pulses:
    cooldownSeconds: 10
    burst: 2
operator:
  writeQueue:
    reconciliationCooldownSeconds: 1
    reconciliationBurst: 2
```

| Value | Type and range | Scope |
| --- | --- | --- |
| `connections.pulses.cooldownSeconds` | number, 0–300; default 0 disables | Fixed window for new connection proposals per graph and positive responses per graph endpoint |
| `connections.pulses.burst` | integer, 1–128; default 1 | Accepted new pulses in each window |
| `operator.writeQueue.reconciliationCooldownSeconds` | number, 0–300; default 0 disables | Fixed window for queued reconciliation attempts per resource and cluster |
| `operator.writeQueue.reconciliationBurst` | integer, 1–128; default 1 | Reconciliation starts allowed per window |

The first admitted pulse starts the window. Redis/Dragonfly atomically tracks
the budget across HA replicas. Intake returns HTTP 429 with `Retry-After` when
the connection budget is exhausted; retry the same request or response from
fresh state. Identical receipt/consent replays do not consume another pulse.
Rejected operations during outages never fall back to a local budget.
Rejection, early revocation and expiry bypass the negotiation budget.

The broader budget retains queue notifications for a later attempt. It does not
delay dependency invalidation, validation, active writes or lease renewals.
TemporaryConnection reconciliation has a separate path so this cooldown cannot
hold up consent or cleanup. Connection pulses are disabled in public demo mode;
leave the independent reconciliation cooldown at zero for unrestricted demos.
Outside Helm, use `POLYAD_CONNECTION_PULSE_COOLDOWN_SECONDS`,
`POLYAD_CONNECTION_PULSE_BURST`, `POLYAD_RECONCILIATION_COOLDOWN_SECONDS` and
`POLYAD_RECONCILIATION_BURST`.

## Meaning at each graph layer

Temporary edges have the same meaning as declarative
[connections and transport grants](../deployment/networking.md#isolating-a-subgraph):

| Target boundary | Endpoints | Effect |
| --- | --- | --- |
| Graph | Logical workload or nested graph nodes | Connect those vertices; a nested node denotes its subtree |
| PolyGraph | Graph or replica-group nodes | Connect the selected component subtrees at their enclosing boundary |
| ReplicaGroup | `replica-0`, `replica-1`, etc. | Connect copies of its template, whether those copies are daemons, graphs, PolyGraphs or nested replica groups |

The [replication diagrams](../graphs/replication.md#connections-between-copies) show how
replica vertices expand into workloads. These temporary edges supplement the
chosen Independent, Chain, Ring, Star, FullMesh or Custom layout on one instance.
They do not change its `spec.connectivity` or every use of its template.

Ports omitted or empty means a **topology-only** edge. Providing ports grants
matching producer egress and consumer ingress within each applicable network
contract. Existing ancestor/descendant contracts are intersected, so a grant
cannot bypass another layer's restrictions. To rely on expiry for network
isolation, configure an isolating network contract with `allowWithin: false` and
avoid other policies that independently permit the same traffic. A temporary
edge does not create a listener, Service, DNS name or tunnel, nor does it turn an
otherwise unrestricted network into a restricted one.

```mermaid
sequenceDiagram
    participant W as Workload
    participant S as Peer service
    participant A as Connections API
    participant K as Kubernetes receipts
    participant O as Target namespace operator
    participant N as Graph topology and policies
    W->>A: POST nodes, ports and TTL
    A->>K: Verify service account and connect permission
    A->>K: Persist proposal and requester consent
    A-->>W: 202 with expiresAt
    O-->>W: Connection event through subscribed stream
    O-->>S: Connection event through subscribed stream
    S->>A: Approve receipt UID using Pod identity
    A->>K: Check approve permission and record consent
    O->>K: Refresh intent under owning graph-family lease
    O->>O: Recheck consent identities and permissions
    O->>O: Check live graph-family GraphRules
    O->>N: Admit edge and reconcile policies
    O->>K: Observe policies and mark Active
    Note over O,N: TTL deadline, endpoint removal or early revocation
    O->>N: Remove this grant and reconcile remaining policies
    O->>K: Mark Expired or Revoked after observing cleanup
```

Admission recomputes live Cheeger constants and the other selected constraints
through the owning hierarchy. Effective edges also enter subsequent graph and
autoscaling admission checks, graph metrics and
[topology events](../workloads/workload-events.md). Creating a Pending request alone does not
add a neighbor. Admission and expiry change topology revisions when they change
effective connections; overlapping identical grants need not change neighbors.

Endpoints are logical nodes, not Pod UIDs. Multiple Pods and activation runs of
the same node share its network identity. Pod restarts, readiness failures and
workloads waiting to be created do not revoke a connection while both logical
nodes remain declared. Removing a node or scaling in a replica ordinal removes
its effective edges; reconciliation then revokes the affected grants. Once
revocation starts, returning that node or ordinal does not restore the connection:
submit a new request to connect it again. Replacing the entire target graph cannot
transfer a grant to the replacement because `graphUid` pins the graph incarnation.

## Deadline, retries and cleanup

TTL starts at the API server's receipt creation timestamp, including time spent
Pending. Replaying an identical request from the same service-account UID and
namespace returns the same receipt and deadline. Changing its TTL, graph or edges
under the same request ID returns 409. There is no renewal operation; submit a
new request ID for a new lifetime. Kubernetes receipts survive process restarts
and operator failover; in-memory timers are not the source of truth.

At the deadline, graph calculations exclude the expired edge. The enabled
operator scans connection receipts every five seconds, in addition to watches
and ordinary rescans, and reconciles remaining network/mesh policies. Cleanup
removes only this receipt's contribution: static edges and other live grants
remain. Expiry and revocation proceed even if the resulting graph violates a
minimum Cheeger or connectivity bound; that violation can block subsequent graph
or scaling actions, but cannot extend an expired grant.

Graph reconciliation also checks tracked connections before admitting new work.
It revokes grants whose logical endpoints have disappeared and removes orphaned
grants whose tracking receipts no longer exist. Cleanup refreshes the affected
NetworkPolicies and Istio authorization policies, including those on descendant
workloads, and emits a decision log with its reason. Changes to effective edges
appear through the existing topology events. Static connections and overlapping
live grants remain under their existing ownership.

Revocation intent and pending policy cleanup are persisted before removing a
grant, so a failed policy write or operator restart cannot silently restore it.
Cleanup retries even when graph rules block normal admission. Static connections
remain declarative: update their specification when removing their endpoints;
reconciliation does not rewrite reusable graph definitions or treat a temporary
workload outage as a request to change the topology.

**Network enforcement is asynchronous.** Queue backlog, unavailable operators,
Kubernetes API outages and CNI/mesh propagation can delay actual traffic removal.
Existing sessions follow the network implementation's policy-update behavior.
TTL starts policy cleanup; the network implementation determines when existing
sockets close. Disabling intake still permits ordinary
reconciliation to clean up existing receipts; keep the operator running until
cleanup completes.

Finalizers keep receipts until policy cleanup is observed. Terminal receipts are
then retained for `retentionSeconds` (default 3600, configurable 0–604800) before
deletion. After deletion the same request ID can create a new receipt; retain IDs
in your application if it needs a longer deduplication window. Intake bodies are
limited to 64 KiB, and a graph admits at most 128 live temporary connections within
a 128 KiB annotation budget. The endpoint uses the chart's shared
`api.rateLimit` settings with its own connection-intake quota.

## Implementation boundaries

The HTTP routes and authenticated receipt store live in `polyad.api.connections`.
Composition, connection, event and metrics blueprints share one Flask application,
one Waitress runtime and one shutdown lifecycle in `polyad.api.http.server`. Listener
ports retain independent enablement and authentication policies. See the
[HTTP runtime](composition-api.md) for worker reservations and socket-based routing. `polyad.graph.temporary` defines the
request and effective topology overlay. `polyad.operator.policies.connections` performs
admission and expiry under the existing graph-family lease.
