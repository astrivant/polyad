# Graph networking and event subscriptions

A graph can group workloads that should communicate freely with one another,
while exposing only a few connections to the rest of an application. A subgraph
is a useful boundary for that group. Polyad compiles its traffic contract into
Kubernetes NetworkPolicies and, optionally, Istio authorization policies.

Graph membership is independent of workload kind: Jobs, daemon Deployments and
workloads inside nested graphs receive the same membership labels. Connections
can therefore join a task to a service, two subgraphs, or explicitly selected
workloads in different namespaces.

## Selection, scope and inheritance

Selection and propagation answer different questions:

| Setting | Meaning |
| --- | --- |
| `GraphRule.spec.enforcement: Namespace` | Select this engineer-owned rule at every boundary in the namespace; the default. |
| `GraphRule.spec.enforcement: Referenced` | Select the rule through a graph's `spec.rules`. |
| `GraphRule.spec.scope: Subtree` | Propagate a selected rule through nested boundaries; the default. |
| `GraphRule.spec.scope: Boundary` | Evaluate the rule at the selecting boundary without propagating the reference. |
| `network.scope: Subtree` | Apply a network contract to direct workloads and all descendant workloads; the default. |
| `network.scope: Boundary` | Apply a network contract only to direct workload nodes. |

A namespace rule remains mandatory at every boundary even with `scope: Boundary`:
selection occurs independently at each boundary. Both a rule's scope and its
network contract's scope must permit propagation for an inherited network
restriction to reach descendants. A `PolyGraph` has no direct workload nodes,
so a boundary-only network contract on it selects no pods. Structural limits
such as `expandedNodes` still measure the declared recursive quantity, regardless
of reference propagation.

For each workload, the operator walks refreshed Kubernetes owner references and
intersects all applicable contracts. Each contract contains a union of allowed
connections; every inherited contract must allow a connection. Child contracts
can narrow inherited restrictions. They cannot opt out of a subtree contract.
The compiler caps intersections at 1,024 candidate pairs and 256 effective terms
per direction to bound policy expansion.

Kubernetes policies themselves are additive. Polyad emits a separate effective
policy selecting each direct workload's pods, rather than overlapping parent and
child policies. Only trusted policy administrators should modify NetworkPolicies,
Istio policies, membership labels, service accounts or injection configuration.
Other administrators' policies can widen the allowances. This is not isolation
against a Kubernetes principal that can change the enforcement infrastructure.
See [Kubernetes NetworkPolicy semantics](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

## Isolating a subgraph

Attach `network` to `Graph`, `EphemeralGraph`, `PolyGraph`, a Feedback epoch's
`spec.graph`, or a `GraphRule`. A rewrite can replace it through `spec.topology`.
The public Python types are `NetworkAccess`, `NetworkPeer`, `NetworkPort` and
`TrafficRule`; their attrs fields generate the CRD schemas.

```yaml
network:
  scope: Subtree
  isolateIngress: true
  isolateEgress: true
  allowWithin: true
  allowDNS: true
```

This permits communication inside the selected graph subtree and DNS to
`kube-system` pods labeled `k8s-app=kube-dns`, while isolating other pod traffic.
Set `allowWithin: false` to allow only explicit connections. Set `allowDNS: false`
and add an explicit egress peer if your DNS deployment uses other labels or a
namespace other than `kube-system`.

Ports are destination ports, with `TCP`, `UDP` or `SCTP` protocols. An empty port
list allows all transport ports. A connection without ports remains a data-flow
declaration and does not create a network allowance:

```yaml
connections:
  - source: producers
    target: consumers
    ports:
      - port: 8080
        protocol: TCP
```

When this boundary has a network contract, the connection adds matching producer
egress and consumer ingress allowances. A node may be a workload or an entire
subgraph. Other inherited or local contracts must also permit that traffic. For
example, an isolated consumer subgraph must explicitly allow the producer peer.
Admission dependencies and readiness gates remain separate from connectivity.
Polyad does not create a Service or DNS address from a connection; use a Resource
node with a Service manifest when the destination needs a stable address.

## Cross-namespace peers and HTTP authorization

A peer with no namespace selects pods in the declaring graph's namespace.
`graph` names a **persisted graph instance**, not its reusable definition. Add
`kind` and optionally `node` to select a graph or one node's descendants. Additional
`podLabels` are combined with that selection. An empty peer matches all pods in
that namespace; it never implicitly means every namespace.

Example: allow a client identity to read an HTTP endpoint on a receiving graph:

```yaml
network:
  scope: Subtree
  mesh: true
  allowWithin: false
  ingress:
    - peer:
        namespace: consumers
        graph: reporting
        kind: Graph
      ports:
        - port: 8080
      principals:
        - cluster.local/ns/consumers/sa/report-reader
      methods: [GET]
      paths: [/status]
```

The namespace and pod selectors form one NetworkPolicy peer, so they are combined
with AND. Istio separately checks the mTLS source identity, HTTP method and exact
path. Use the `serviceAccountName` in the client's pod template to select its
identity. Principal identities omit the `spiffe://` prefix. HTTP constraints and
principals require `mesh: true`; unsupported egress HTTP/source-identity rules
are rejected because authorization belongs at the receiving proxy. L4 peer
selection and L7 identities are independent checks; Istio does not turn pod-label
selectors into service identities.

`allowWithin: true` and connection port grants are additional unrestricted
allowances. Set `allowWithin: false` and avoid unrestricted ingress grants where
all callers must satisfy a method, path or identity restriction. Paths in graph
contracts are exact, absolute paths; wildcard patterns are rejected.

The receiver needs ingress permission and an isolated sender needs egress
permission. Cross-namespace peers only select traffic; Polyad does not create or
modify resources in another namespace. Install an operator for that namespace or
manage its receiving policy separately. Reusing a graph instance name reuses its
peer address, while ownership of the generated policy remains UID-fenced.

## Enforcement and lifecycle

NetworkPolicy requires a CNI that enforces it. Istio is additionally required for
HTTP and service-identity checks. With `mesh: true`, Polyad requests native sidecar
injection, emits strict `PeerAuthentication` and an `AuthorizationPolicy`, and
verifies injection with a dry-run Pod admission before creating a Job or Deployment.
Native sidecars allow finite Jobs to complete. Mesh application containers must
run as non-root, avoid Istio's reserved UID 1337, and work with dropped capabilities.
Host namespaces, hostPath volumes, elevated capabilities and user-supplied mesh
interception overrides are rejected inside these contracts.

A mesh-isolated workload receives a narrow infrastructure egress allowance to
`app=istiod` in `global.istioNamespace` on TCP 15012. Configure an existing mesh's
injection webhook, native sidecars, trust domain, DNS and CNI for the target
namespace. These settings are not inferred from arbitrary existing mesh topology.
See [Istio authentication](https://istio.io/latest/docs/tasks/security/authentication/authn-policy/)
and [authorization](https://istio.io/latest/docs/reference/config/security/authorization-policy/).

Policy writes use the same serialized, lease-fenced Kubernetes adapter as workload
writes. New admission waits for a fresh read of the persisted policies. Policies
are updated in place with resource versions, and stay in place while removed
workloads finish deletion and custom finalizers. Graph status inventories include
NetworkPolicy, AuthorizationPolicy and PeerAuthentication counts.

An API acknowledgement does **not** acknowledge CNI or Envoy configuration
propagation. Policy changes are eventually enforced, and existing connections can
outlive a policy update depending on the CNI. Adding isolation to running work
also requires replacement of pods that lack the generated membership labels.
For a planned isolation cutover, drain the graph before changing and restarting it.
Policies are not an application-level transactional traffic switch.

## Optional chart networking

All chart-level networking is optional. `networkPolicy.enabled` isolates the
operator pods. Explicit `apiServerCIDRs` and `apiServerPort` must match how your CNI
observes the Kubernetes API connection; Kubernetes Service translation varies.
The chart allows bundled Dragonfly on TCP 6379, DNS, and optional Istiod access.
An external cache requires `networkPolicy.extraEgress`. A second policy restricts
bundled Dragonfly to operator/controller clients and its replication peers.

Use `networkPolicy.compositionPeers`, `eventPeers` and `healthPeers` to grant
NetworkPolicy peer selectors access to ports 8090, 8091 and 8080 respectively.
An empty peer list grants no pod ingress to that endpoint. These chart policies
do not select workload pods; graph contracts control those separately.

For an existing Istio installation:

```yaml
mesh:
  enabled: true
  operator:
    enabled: true
    eventPrincipals:
      - cluster.local/ns/consumers/sa/report-reader
global:
  istioNamespace: istio-system
```

`mesh.operator.enabled` injects operator pods and enables endpoint authorization.
`compositionPrincipals` and `eventPrincipals` authorize their separate ports.
Health remains probe-accessible. Bearer authentication is still required.
When NetworkPolicies are also enabled, grant the corresponding peers there too.

`mesh.install: true` installs the upstream Istio base and istiod charts, pinned
to 1.30.4. Set `global.istioNamespace` to the release namespace so
all upstream charts agree. Install once per control plane; releases using an
existing mesh keep `install: false`. Wait for the control plane and injection
webhook before submitting mesh workloads. To install an optional Istio ingress
gateway, set `mesh.ingress.enabled`, `hosts`, and `tlsSecret`. Authorize that
gateway's service account in the endpoint principal lists when operator mesh
authorization is enabled. `/v1/events` streams without a route timeout;
`/events/openapi.json` exposes its schema. The existing Gateway API option remains
available for the composition service. See [Istio Helm installation](https://istio.io/latest/docs/setup/install/helm/).

## Event subscriptions

Enable `events.enabled` for a separate ClusterIP Service,
`<release>-polyad-events:8091`. `GET /v1/events` serves Server-Sent Events, and
`GET /openapi.json` serves its authenticated schema. This is a one-way observation
feed; composition requests still use port 8090.

```bash
curl --no-buffer \
  -H "Authorization: Bearer $POLYAD_EVENTS_TOKEN" \
  -H 'Last-Event-ID: 1750000000000-0' \
  http://polyad-polyad-events:8091/v1/events
```

Omit `Last-Event-ID` for live observations, or use `0-0` to read retained history.
Each `graph` event has a Redis stream ID and JSON containing graph kind, namespace,
name, UID, resource version, owner references, audit labels, lifecycle fields and
resource counts. It omits workload specs and credentials. Streams come from
refreshed owning-shard observations, not every underlying Kubernetes watch event.
Deletion may appear as a `deleting` observation; this is not a complete deletion
ledger. Use the Kubernetes/API status and composition audit endpoints as the source
of truth.

Replicas share a bounded Dragonfly/Redis stream. The default retention is 10,000
observations, configurable with `events.retention`. Delivery is at least once:
deduplicate by resource UID and resource version. Cache failover can lose recent
observations. A missing or expired reconnect cursor returns HTTP 410; a live
subscriber falling behind receives a `reset` event and must refresh its snapshot.
Cache failures return 503 before streaming, or an `unavailable` event followed by
closure during a stream. Reconnect to any healthy replica with the last received ID.

There are 16 concurrent subscriber slots per replica by default, configurable
with `events.maxConnections`. A full replica returns 503. The shared event
connection-request quota uses `api.rateLimit.requestsPerMinute` in a separate
namespace budget; active streams do not consume a request per event. Proxies must
preserve streaming, avoid response buffering and support long-lived connections.
See [Flask streaming](https://flask.palletsprojects.com/en/stable/patterns/streaming/).

## Credentials, health and process signals

Both APIs accept a Secret reference (`api.existingSecret`, `events.existingSecret`)
containing a `token` key. Alternatively, set `api.key` or `events.key` and clear the
corresponding `existingSecret`; Helm creates the Secret. The two credentials are
independent. Inline values become Helm release data, so existing Secrets are
preferable when credentials are managed outside Helm.

Secrets are projected as read-only volumes, without `subPath`. A replica loads its
token at startup and checks a fingerprint every five seconds. When Kubernetes
projects a changed or missing token, the health endpoint reports that replacement
is required. The replica stops taking new graph duties and rejects new API work;
the liveness probe causes a container restart with current credentials. Secret
projection and probe intervals make rotation asynchronous. Inline Secret changes
also alter the pod-template checksum during Helm upgrades.

`SIGHUP` requests replacement through the same unhealthy state. `SIGTERM` and
`SIGINT` mark the replica as draining and forward cooperative shutdown to the Kopf
thread. Shutdown joins outstanding writes and releases local resources; another
replica acquires duties after the old leases expire. No credential polling code
reads Secrets through the Kubernetes API or patches Deployments. Kubernetes owns
container restart and pod replacement. Running outside Kubernetes requires a
supervisor that acts on the health failure, or an explicit termination signal.
