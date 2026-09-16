# Composition API service

The optional composition service accepts graph requests and exposes their status
and resource audit. This guide covers service deployment, authentication, routing,
rate limits and schema discovery.

See [Composition requests](composition-requests.md) for the request format, ID
references, retries and audit semantics. See [Graph rules](graph-rules.md) for the
constraints applied before admission. The HTTP API cannot create or modify rules.
Its bearer credential authorizes workload creation and audit reads in the operator
namespace; it is not a per-user identity or tenant isolation mechanism. Apply
normal workload RBAC, Pod Security and resource quotas as well.

## Enable the service

Create a Secret containing a `token` key in the operator namespace, then install
with `api.enabled=true` and `api.existingSecret=<secret-name>`. The chart exposes
port 8090 through `<release>-polyad-api`, a ClusterIP Service. Keep that endpoint
private; terminate TLS at your ingress or gateway if exposing it beyond the cluster.
The health endpoint remains Kopf's existing port 8080.

The Python process starts Kopf on its existing operator thread. Startup constructs
the Flask application with `APIBuilder`, then hosts it on a dedicated Waitress
thread. Waitress is a [production WSGI server supported by Flask](https://flask.palletsprojects.com/en/stable/deploying/waitress/).
There is no Flask development server and no additional command in the Dockerfile.

Build an application with custom synchronous adapters:

```python
from polyad.api import APIBuilder

app = (
    APIBuilder()
    .with_handlers(submit=submit_receipt, lookup=lookup_receipt)
    .with_bearer_token(namespace_token)
    .with_metadata("Team workload scheduler", "v1alpha1")
    .build()
)
```

`submit_receipt(request)` accepts an attrs `CompositionRequest` and returns a JSON
receipt. `lookup_receipt(request_id, audit)` returns a JSON result or `None`.
The builder is immutable: methods return a new configuration, and each `build()`
creates a separate application. Missing handlers or an empty token fail at build
time. `create_app(...)` is a convenience wrapper around the same builder.
In the operator, adapters bridge to `CompositionStore` on Kopf's event loop.

## Optional Gateway API routing

The chart can expose the composition service through a `gateway.networking.k8s.io/v1`
HTTPRoute. Install the Gateway API CRDs and a compatible Gateway controller first;
the chart uses your controller's GatewayClass. See the upstream
[Gateway setup guide](https://gateway-api.sigs.k8s.io/guides/getting-started/simple-gateway/).

To attach to an existing Gateway:

```yaml
api:
  enabled: true
  existingSecret: polyad-api
  gateway:
    enabled: true
    name: shared-gateway
    namespace: edge
    sectionName: https
    hostnames: [polyad.example.com]
```

The existing listener must allow HTTPRoutes from the release namespace. Its
administrator owns TLS configuration and certificate renewal. For a Gateway
created by this chart, supply an installed GatewayClass and a certificate Secret:

```yaml
api:
  enabled: true
  gateway:
    enabled: true
    create: true
    className: your-gateway-class
    sectionName: https
    hostnames: [polyad.example.com]
    tlsSecret: polyad-tls
```

Created Gateways allow HTTPRoutes from the release namespace only. `tlsSecret`
selects HTTPS on port 443; omitting it selects HTTP on port 80. Secrets and DNS
records are supplied separately. The route forwards `/v1/compositions`, `/v1/activations` and
`/openapi.json` to the API Service on port 8090. Requests still require the bearer
token. Pod health probes keep using their separate internal port.

## Shared shard rate limits

Operator-hosted APIs enable [Flask-Limiter](https://flask-limiter.readthedocs.io/en/stable/)
with the existing Redis/Dragonfly connection (`POLYAD_CACHE_URL`, including Secret
credentials and `rediss://` support). Every composition request maps to the same
one of 32 logical shards used by the scheduler, using its namespace and deterministic
Composition name. Submission, status and resource-audit requests share a budget
of **60 requests per minute per shard**, across all HTTP replicas and request IDs
in that shard. Scaling or transferring shard ownership does not multiply quotas.

```yaml
api:
  rateLimit:
    enabled: true
    requestsPerMinute: 60
```

The budget uses a fixed window starting with the first request; bursts are possible
at window boundaries. Accepted retries and audit reads consume quota too. A `429`
response includes `Retry-After` in seconds and `X-RateLimit-*` headers; preserve the
same `requestId` when retrying. Authenticated schema discovery uses a separate
namespace-wide budget of the same size. Invalid credentials are rejected before
quota access, and shard keys do not depend on forwarded client-IP headers.

Storage errors return `503` before intake or lookup, with no in-process fallback.
Cache loss can reset transient quota counters. These limits bound HTTP intake;
queued reconciliation, Kubernetes write ordering and pod health checks retain their
existing behavior. They are not per-user quotas or limits on an individual graph's
resource consumption.

Outside Helm, configure `POLYAD_API_RATE_LIMIT_ENABLED` and
`POLYAD_API_REQUESTS_PER_MINUTE`. Embedded library services opt in through the builder:

```python
from polyad.api import APIBuilder, RateLimitPolicy

builder = APIBuilder().with_rate_limits(RateLimitPolicy(
    namespace="team",
    storage_uri="redis://dragonfly:6379/0",
    requests_per_minute=60,
))
```

## OpenAPI schema

The service serves an OpenAPI 3.1 document at `GET /openapi.json`, generated with
[`apispec`](https://apispec.readthedocs.io/en/stable/quickstart.html). It uses the same
bearer authentication as the composition routes:

```sh
curl --fail-with-body -H "Authorization: Bearer $POLYAD_API_TOKEN" \
  http://localhost:8090/openapi.json
```

The schema describes ID-based composition nodes, dependencies and connections,
receipt/status/audit responses, error codes and bearer authentication. Native
workload definition payloads remain extensible objects validated by their CRDs
and the operator. Cross-reference resolution and graph policy checks are semantic
constraints beyond OpenAPI's structural request schema.

`APIBuilder.with_metadata(title, version)` sets the document's service identity.
Every built service exposes the endpoint; no additional Helm setting or process is
needed. The test suite checks OpenAPI validity, route coverage, and request/response
conformance using the development `openapi-spec-validator` dependency.

## Workload activation

The same service supports [durable activation requests](activation.md) for workloads,
subgraphs and daemon replica groups. The [standalone Python client](../pkg/client/README.md)
can submit compositions, inspect audit identities, pulse nodes and subscribe to events.
