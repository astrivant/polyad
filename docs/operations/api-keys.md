# API keys and request lanes

Configure any number of keys in two groups: `authentication.services` for
application services and `authentication.operators` for peer operators and
infrastructure callers such as KEDA. There is no application-level key-count
limit; Kubernetes Secret and Pod projection limits still apply.

| Direction | Calls into Polyad | Calls from Polyad |
| --- | --- | --- |
| `Inbound` | Allowed on the key's `endpoints` | Denied |
| `Outbound` | Denied | Allowed beneath the key's `baseUrl` |
| `Bidirectional` | Allowed on `endpoints` | Allowed beneath `baseUrl` |

Every key has its own `requestsPerMinute` and `maxConcurrentRequests` lane.
Both budgets are shared across all replicas using the same root namespace and
cache. A bidirectional key uses **one combined budget** for both directions,
including calls handled by different listeners. Group and key name determine
the lane; equal names in different groups are independent. Group membership
alone grants no additional API permissions.

## Table of contents

- [Configuration](#configuration)
- [Shared admission and rotation](#shared-admission-and-rotation)
- [Outbound calls](#outbound-calls)
- [KEDA and metrics](#keda-and-metrics)
- [Graph access and workload assignments](#graph-access-and-workload-assignments)
- [Optional authentication database](#optional-authentication-database)
- [Demonstrations without authentication](#demonstrations-without-authentication)
- [Optional Flask authentication adapter](#optional-flask-authentication-adapter)

## Configuration

Use [the complete reference values](../../charts/polyad/values-authentication.reference.yaml)
alongside a deployment profile. Tokens stay in administrator-managed Secrets;
the chart projects them with a ConfigMap containing only policy and Secret
references. [External Secrets Operator](authentication.md) can populate them.

```yaml
api:
  enabled: true
authentication:
  services:
    - name: pipeline
      direction: Inbound
      existingSecret: pipeline-polyad-key
      secretKey: token
      endpoints: [composition]
      requestsPerMinute: 120
      maxConcurrentRequests: 8
    - name: archive
      direction: Outbound
      existingSecret: archive-api-key
      baseUrl: https://archive.example.com/api
      requestsPerMinute: 60
      maxConcurrentRequests: 4
  operators:
    - name: west
      direction: Bidirectional
      existingSecret: west-peer-key
      endpoints: [composition, observations]
      baseUrl: https://west-polyad.example.com
      requestsPerMinute: 120
      maxConcurrentRequests: 8
```

Create each referenced Secret before installation, with a distinct token under
`secretKey` (default `token`). Helm requires `existingSecret`, `name`, `direction`
and both limits. Names must be unique within each group. Duplicate token values
across entries are rejected because they make identity and quota selection
ambiguous. Both limits must be positive.

Inbound scopes are independently selectable: `composition`, `activations`,
`throughput`, `events`, `topology`, `metrics`, and `observations` (the optional
read-only observer). Sharing a listener does not grant another endpoint scope. Enable each API separately. Keys authorize its existing namespace
and operations; they do not grant Kubernetes RBAC or bypass GraphRules.
Temporary connections retain Kubernetes service-account authentication and
namespace checks.

When named inbound keys cover an endpoint, they replace its single shared
token and the old token Secret is no longer mounted. Endpoints without named
keys retain their existing configuration. Clients send
`Authorization: Bearer <token>`; group identity comes from the registered key.
The existing Python client's bearer-token option accepts these keys.

## Shared admission and rotation

Dragonfly atomically checks the key's fixed sixty-second request window and
concurrency leases before dispatch. Invalid credentials return 401 without
consuming quota; valid credentials with the wrong direction or API scope return
403. Exhausted lanes return 429 with `Retry-After`. Unavailable credential files
or quota storage return 503, with no per-process fallback. Named keys use their
individual quotas in place of the older shared shard request quota.

Normal HTTP requests release concurrency after the handler finishes. Event
streams retain a permit until closure. Leases renew every ten seconds; streams
stop if renewal fails. Abandoned permits expire after 120 seconds. These are
HTTP limits, separate from graph execution concurrency, KEDA replica limits and
the listeners' overall worker/connection caps. Lanes do not reserve worker
threads or reorder durable graph work.
Quota state is transient: cache data loss can reset rate windows and outstanding
leases, even when optional PostgreSQL persists graph state.

Projected token changes apply to the next request without resetting counters.
Already admitted nonstreaming calls retain their permits until completion.
Event streams revalidate their token, policy and optional database revocation on
each chunk or heartbeat, and stop after removal or rotation. Keep group and key names stable to
preserve quota identity. Removing an entry revokes new calls with that token.
Helm policy changes update a Pod-template checksum and roll affected listeners.
Existing credential rollouts and optional ESO restart annotations still apply.

Dense and split deployments use the same registry. Read-only observers receive
it when observation keys are configured and share the cache for admission.
Root-managed remote execution Pods retain their existing execution credentials
and receive only the nonsecret workload-assignment registry, not HTTP API-key
Secrets or the authentication database credential.

## Outbound calls

Operator integrations can use the configured transport directly:

```python
from polyad.auth.outbound import OutboundClient

client = OutboundClient.from_environment()
try:
    response = await client.arequest(
        "services", "archive", "POST", "records",
        body=b'{"graph":"pipeline"}',
        headers={"Content-Type": "application/json"},
    )
finally:
    client.close()
```

Paths are relative to `baseUrl`. Callers cannot override Authorization or Host,
escape the path prefix, or follow redirects with the credential. HTTPS verifies
certificates. Calls check a request deadline between response reads, use socket
waits of at most five seconds, limit responses to 8 MiB and never retry
automatically. The async method
keeps network I/O off the operator loop and joins outstanding dispatch on
cancellation.

Configuring an outbound key does not initiate application calls or create graph
actions. This transport is the explicit integration point for service and peer
HTTP calls. Kubernetes federation continues to use registered kubeconfigs.
NetworkPolicy and Istio egress must separately allow the destination; configure
`networkPolicy.extraEgress` where needed.

## KEDA and metrics

Give KEDA an inbound `operators` key with `endpoints: [metrics]` and enough
budget for every scaler. Point `metrics.authentication.existingSecret` and
`secretKey` at that key's Secret, and enable `metrics.authentication.enabled`
and `keda.authentication.enabled`. The chart rejects a KEDA Secret that does not
match a named inbound metrics key when metrics uses the registry.

Measurements remain cached, but named-key authentication adds shared-cache
admission to every scrape. Quota exhaustion and cache failure make that scrape
unavailable. Separate callers should use separate keys for independent lanes.

`polyad-types` exposes `APIKey`, `Authentication` and `KeyDirection` for validating
this configuration without installing the operator.

## Graph access and workload assignments

Discovery, event and topology permissions also require `graphs` grants and a
fixed `home` graph. The [inherited discovery ceiling](../apis/discovery.md#access-modes-and-inherited-ceilings)
limits those grants relative to that home; HTTP callers cannot change it. Each grant has
`kind` (`Graph`, `PolyGraph` or `ReplicaGroup`), `name`, `namespace`, optional
`cluster` and `uid`, and `descendants` (default true). Empty grants reveal no graph
events or snapshots. The operator verifies local ownership and, at the root,
registered cross-cluster ancestry. A grant with `descendants: false` permits only
that graph. An optional UID pins the incarnation instead of authorizing a reused
name. Internal operator graphs are always excluded.

`throughput` uses the same graph grants for measurement intake. Other endpoint
capabilities retain their documented namespace scope; a graph grant is not a
blanket Kubernetes permission. In particular, composition creation remains a
separate explicit capability.

Use `workloads` to inject a key into assigned definitions:

```yaml
authentication:
  services:
    - name: pipeline
      direction: Inbound
      existingSecret: pipeline-key
      secretKey: token
      endpoints: [throughput, discovery, events, topology]
      home: {kind: Graph, name: pipeline, namespace: apps}
      requestsPerMinute: 120
      maxConcurrentRequests: 8
      graphs:
        - {kind: Graph, name: pipeline, namespace: apps, descendants: true}
      workloads:
        - kind: Daemon
          name: processor
          namespace: apps
          env: POLYAD_API_KEY
          secret: pipeline-key
          containers: [main]
```

The compiler adds `env.valueFrom.secretKeyRef`. It never resolves a token into a
literal Pod environment value. Assignments match a `Daemon` or `Workload` definition
by kind, name, namespace and optional cluster. Empty `containers` selects all regular
containers; conflicting environment names or unknown containers are rejected.
`secret` overrides the referenced name in the workload namespace; otherwise
`existingSecret` is reused. Provision that Secret in each destination namespace,
for example through ESO. Polyad does not copy application tokens across namespaces
or clusters. Changing a Secret-backed environment value requires a workload restart;
use [opt-in ESO restarts](authentication.md#restart-consumers-after-rotation).

## Optional authentication database

Set `authentication.storage.enabled: true` with named API keys to persist verification
records, policy revisions and per-key revocation. Raw bearer tokens remain in
Kubernetes Secrets. Dragonfly holds only lane identifiers, counters and expiring
permits; neither plaintext tokens nor PostgreSQL credentials are stored there.

The default storage choice is a separate managed CloudNativePG Cluster with database
`polyad-authentication` and the dedicated application owner `polyad_authentication`.
The chart revokes PUBLIC access to the database and public schema, and the operator
revokes PUBLIC access to its authentication tables. Database administrators retain
administrative access. Install CloudNativePG before enabling managed storage.

Use the shared [PostgreSQL encryption-at-rest settings](../deployment/postgresql.md#encryption-at-rest)
to select encrypted volumes for this database and its standbys, as well as managed
state storage. Choose an existing encrypted StorageClass or provision a GKE class
with a Cloud KMS key reference. Existing PVCs require a migration; external
databases and backups use their provider's encryption configuration.

Optional [record encryption](../deployment/record-encryption.md) also encrypts
policy JSON before the operator sends it to either authentication database.
One-way token fingerprints, policy digests and revocation flags remain queryable.

| Setting | Choice |
| --- | --- |
| `storage.separateDatabase: true`, `storage.managed: true` | Provision a dedicated Cluster and application login; configure `database`, `username`, `size`, `storageClass` |
| `storage.separateDatabase: true`, `storage.managed: false` | Mount `existingSecret` / `secretKey` as an external PostgreSQL DSN; administrator supplies database privileges and storage |
| `storage.separateDatabase: false` | Reuse enabled `postgresql` state storage and its login; separate-role isolation is not provided |

Verification records contain a SHA-256 token fingerprint and nonsecret policy
metadata. Use randomly generated, high-entropy API tokens.
Mounted Secrets and policy still authorize the current token: the database does
not recover a missing Secret or authenticate an old revision after rotation.
Set `polyad_auth_lanes.disabled = true` for the appropriate scope, key group and
key name to revoke a lane durably. A database outage returns 503 for protected
requests; no permissive fallback is used. Initialization is lazy, so an unavailable
authentication database does not prevent the operator from starting its graph
controllers. Immutable policy revisions are retained for audit; administer their
retention separately from graph-event retention.

## Demonstrations without authentication

Use [values-demo.reference.yaml](../../charts/polyad/values-demo.reference.yaml), or
set `authentication.mode: Disabled`. Enabled HTTP listeners then omit credential
checks and configured request/concurrency quotas, including named-key lanes and
Flask-Limiter. Event stream slots still reserve shared HTTP workers for other APIs;
these slots bound transport capacity across callers. If temporary connections are enabled,
TokenReview and SubjectAccessReview are bypassed using one demonstration identity.
Namespace scope, graph rules, TTL bounds, body-size bounds and finite server
capacity still apply. This option does not make an infinite-capacity server or
remove workload safety constraints. The Python client accepts `token=None` for
this explicit public mode. `Required` remains the production default.

## Optional Flask authentication adapter

`authentication.backend: Builtin` is the default. To use
[Flask-HTTPAuth](https://flask-httpauth.readthedocs.io/en/latest/getting-started.html)
for bearer parsing and authentication challenges, choose `FlaskHTTPAuth`. Official
production and development images include every runtime extra, including this
adapter. It is imported only for an authenticated endpoint using this backend.
For Python installations outside those images, install `polyad[flask-auth]`
(or `poetry install --extras flask-auth`). Startup fails clearly if a custom image
or installation omits the selected adapter. Endpoint
permissions, graph grants, durable revocation and HA-wide rate/concurrency lanes
remain Polyad's responsibility with either backend.
