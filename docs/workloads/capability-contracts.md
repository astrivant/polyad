# TTL-bound capability contracts

<!-- toc:start -->
**Table of contents**

- [Capacity, willingness and the shared pool](#capacity-willingness-and-the-shared-pool)
- [Administrator setup](#administrator-setup)
- [Publish, refresh and withdraw](#publish-refresh-and-withdraw)
- [Discover matching peers](#discover-matching-peers)
- [Resources, VPA and adaptation](#resources-vpa-and-adaptation)
- [Lifetime, limits and guarantees](#lifetime-limits-and-guarantees)
<!-- toc:end -->

Services can advertise "I can accept these kinds of work, and this is how much
I am willing and able to share." Each Ready Pod publishes a short-lived contract
for its exact logical service. Peers discover contracts by work type and group
labels within their existing graph discovery permissions.

This is opt-in cooperation, not automatic scheduling. Publication does not create
connections, reserve resources, change replicas or prove SLA compliance.

## Capacity, willingness and the shared pool

Each `CapabilityOffer` has a work `name`, a `unit`, `availablePerSecond` and
`sharePerSecond`. Availability is the application's estimate of **additional
sustainable work**, after its own load. Willingness is the application's sharing
ceiling. The SDK exposes:

```text
offered_per_second = min(availablePerSecond, sharePerSecond)
offered_concurrency = min(availableConcurrency, shareConcurrency)
```

Concurrency is optional, but both concurrency fields must be supplied together.
Zero rate or zero slots means no new work of that type should be accepted.
Unknown concurrency is `None`, not an unlimited slot promise. Compare rates only
when both the capability name and its unit agree between applications.

For example, a worker might have room for 20 images/second but offer only 12 to
peers, with four additional concurrent jobs. It can also advertise a video decode
capability, but **both offers draw on the same Pod's provider pool**. Their maxima
are not necessarily achievable simultaneously. A queue, scheduler or admission
guard inside the provider must arbitrate that shared budget across all peers and
work types. There is one complete contract per Pod UID, not one per container;
choose one publisher in multi-container Pods.

Labels such as `team=media` or `pool=interactive` are self-declared application
selectors. They are neither Kubernetes placement constraints nor trusted tenant
or authorization assertions. Readers need actual graph grants regardless of labels.

## Administrator setup

Enable the existing connections listener for publication and the events listener
for discovery. See the [reference values](../../charts/polyad/references/values-capabilities.reference.yaml).
No new Python extra is required, and nothing advertises automatically.

Publication uses a Pod-bound projected service-account token with audience
`polyad-connections`, just like [service connection consent](../apis/temporary-connections.md#authenticate-workloads).
Polyad verifies the Pod, service account, controller ownership chain, logical node
and graph UID. It additionally requires the **separate `advertise` RBAC verb** on
the containing graph. `connect` or `approve` alone does not grant publication.

For an existing service account `pipeline-worker`, this grants publication only
for graph `pipeline` in namespace `analytics`:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: advertise-pipeline
  namespace: analytics
rules:
  - apiGroups: [polyad.astrivant.com]
    resources: [graphs]
    resourceNames: [pipeline]
    verbs: [advertise]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: advertise-pipeline
  namespace: analytics
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: advertise-pipeline
subjects:
  - kind: ServiceAccount
    name: pipeline-worker
    namespace: analytics
```

Use `polygraphs` or `replicagroups` for those boundary kinds. Mount the projected
token using the linked connection guide and allow traffic to port 8093 through
the existing network and Istio policies. Permission to advertise does not grant
permission to read peer contracts or connect to them.

Discovery uses a **different named API key**, with endpoint `discovery`, a fixed
`home` graph and explicit `graphs` grants. Existing inherited discovery ceilings
still apply. Use [named credentials](../../charts/polyad/references/values-authentication.reference.yaml)
and the [discovery guide](../apis/discovery.md); a namespace events bearer token
alone does not authorize directory reads.

## Publish, refresh and withdraw

```python
from pathlib import Path

from polyad_sdk import (
    CapabilityAdvertisement,
    CapabilityOffer,
    Client,
    WorkloadContext,
    env,
    resource_availability,
)

context = WorkloadContext.from_environment()
publisher = Client(
    context.connections_url,
    None,
    token_provider=lambda: Path("/var/run/polyad-connections/token").read_text().strip(),
)

# Supply fresh measurements from your worker's own admission/queue accounting.
offer = CapabilityAdvertisement(
    endpoint=context.identity,
    capabilities=(
        CapabilityOffer("image.resize", "images", 20, 12, 6, 4),
        CapabilityOffer("video.decode", "frames", 60, 15, 2, 1),
    ),
    labels={"team": "media", "pool": "interactive"},
    resources=resource_availability(context),  # Optional disclosure.
    ttlSeconds=30,
)
contract = publisher.advertise_capabilities(offer)
print(contract.podUid, contract.observedAt, contract.expiresAt)

# During graceful drain, stop making offers before dropping readiness.
publisher.withdraw_capabilities(context.identity)
```

For an `AdaptiveService`, the same operations are available as:

```python
contract = service.advertise_capabilities(
    offers, labels={"team": "media"}, ttl_seconds=30, include_resources=True
)
service.withdraw_capabilities()
```

Pass `advertisements=publisher`, or use a connections `Client` that supports the
advertiser interface. `AdaptiveService.from_environment()` reuses its configured
connections client. Resource disclosure is **off by default** in this helper.

The application owns refreshing. Recompute capacity and republish, for example,
every 10 seconds for a 30-second TTL, with bounded jitter across replicas. Also
refresh after an adaptation, load change or observed resize. Serialise publication
and withdrawal: concurrent requests are ordered by arrival, not business intent.
Each refresh replaces the whole contract, including labels and work types.
There are no implicit mutation retries or background SDK renewal threads. A lost
acknowledgement is uncertain; a subsequent explicit publication is a new refresh.

## Discover matching peers

```python
reader = Client(context.events_url, env["POLYAD_EVENTS_TOKEN"])
for contract in reader.offers(
    capabilities=("image.resize",), labels={"team": "media", "pool": "interactive"}
):
    provider = contract.advertisement
    resize = next(item for item in provider.capabilities if item.name == "image.resize")
    if resize.unit == "images":
        print(provider.endpoint, contract.podUid, resize.offered_per_second)
```

Requested capability names and labels are conjunctive: all must match. With no
capability selector, any positive-capacity work type is sufficient. By default,
zero-rate or zero-slot offers are excluded; `available_only=False` includes them
for diagnostics, but never includes expired contracts. The SDK checks expiry
again as it yields records. Keep clocks synchronized and recheck `expiresAt`
before using a retained result. No live snapshot can prevent a provider changing
or failing immediately after discovery.

Raw `services()` records expose a `contracts` list per logical node. Contracts
are **poll-based**, not events: topology revisions and event cursors do not track
contract refresh or expiry. Poll according to the shortest TTL you rely on.
Every read rechecks Pod readiness, UID-fenced ownership and publication RBAC.
Expiry during these checks is filtered out before returning the snapshot.

Resolve actual protocol addresses through the application's existing endpoint
configuration and [connection consent](../apis/discovery.md). A contract is not
a URL, network grant or routing change. In particular, a load-balanced Service
address may choose a different Pod than the one that advertised. Use the offer
as a discovery hint unless the receiving provider can verify the selected
`podUid` and `observedAt` and admit the requested work against its current budget.

## Resources, VPA and adaptation

`resource_availability(context)` reads the SDK's live cgroup v2 metrics on each
call. It exposes CPU quota, cumulative CPU use, memory limit/use and computed
memory headroom, alongside separately named startup requests and VPA min/max
bounds. Missing or unlimited readings remain `None`. It never treats cumulative
CPU time as utilization, CPU quota as spare CPU, or VPA's maximum as the current
allocation. Projected requests and VPA policy values may lag later changes.

Applications estimate work-specific rates from their own measurements. CPU and
memory cannot by themselves establish images/second or SLA compliance. A resize
or adaptation may increase available throughput while the willing share remains
unchanged, or lower either number. Recompute offers with the new observations.
Keep [SLA reporting](service-level-computations.md) separate from
capacity estimates: an advertised rate is not evidence that work completed.

## Lifetime, limits and guarantees

The server uses Redis time for receipt and expiry. TTLs range from 5 to 300 seconds,
defaulting to 30. Each graph has at most 128 active Pod contracts, each at most
16 KiB including its internal verified identity, with at most 32 unique work
types and 16 labels. Exhausted registry capacity or backend failure returns 503;
the server does not evict another provider to accept a new one. Existing HTTP
request quotas apply. Connection TTL and pulse settings do not control offer TTLs.

Contracts are transient shared-cache state, not CRDs or durable business commitments.
Cache loss removes them until providers refresh. Each discovery authority owns
its own registry. Publish to the same operator authority peers query; leaf
registries are not automatically copied to an atlas root. Registered remote
providers can explicitly publish to a root, using its registered cluster identity
and transports, but requests are never silently forwarded or broadcast. Set both
the endpoint's cluster and `Client(..., identity_cluster="west")` to the registered
token-issuing cluster when explicitly addressing a root from that cluster.

KEDA/HPA replicas advertise independently when they become Ready. Deleting,
replacing or unready Pods are omitted before their TTL expires. Local process
demos can construct and filter these models with injected clients and resource
samples; production publication intentionally requires real Pod-bound identity.

These are **TTL-bound offer contracts**, not exclusive reservations. The provider
must authenticate work requests, enforce its shared rate/concurrency budget,
and accept or reject work atomically. Two peers can discover the same offer.
TTL expiry closes discovery of new work; it does not cancel already accepted
jobs. Drain or complete those jobs according to the application's contract.
No automatic Istio weights, KEDA scaling, Cheeger policy or SLA defaults change.
