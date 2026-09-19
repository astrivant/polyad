# Advanced Istio integration

Polyad keeps every advanced mesh behavior opt-in. Enabling `mesh.enabled` alone
retains the existing strict-mTLS, graph authorization and weighted-routing
behavior. The controls below add resilience, proxy telemetry, locality, ingress
identity, configuration scoping, explicit egress and policy observation.

## Values reference files

Use the references as composable examples; Helm never loads them automatically:

| Reference | What it offers |
| --- | --- |
| [`values-istio-bundled.reference.yaml`](../../charts/polyad/references/values-istio-bundled.reference.yaml) | A release-owned, version-pinned Istio base and control plane with native operator sidecars. |
| [`values-istio-existing.reference.yaml`](../../charts/polyad/references/values-istio-existing.reference.yaml) | Policies on a platform-owned mesh, demonstrated with Gateway API ingress. |
| [`values-istio-features.reference.yaml`](../../charts/polyad/references/values-istio-features.reference.yaml) | Proxy telemetry, configuration scoping, AUDIT/dry-run policy evaluation and explicit egress. |
| [`values-istio-traffic.reference.yaml`](../../charts/polyad/references/values-istio-traffic.reference.yaml) | Locality-aware routing, endpoint ejection and event connection balancing. |
| [`values-multicluster.reference.yaml`](../../charts/polyad/references/values-multicluster.reference.yaml) | Cross-network identity, peer registration and east-west gateway deployment. |

The generated [Graph values reference](../../charts/polyad-crds/values-graphs.reference.yaml)
documents every `spec.traffic[].resilience` field used for route-level retries,
timeouts, circuit breaking and endpoint ejection. Equivalent generated references
exist for PolyGraph and Rewrite resources.

## Deployment strategies

Choose exactly one control-plane ownership model. With the bundled strategy,
`mesh.install=true` installs the pinned `base` and `istiod` dependencies and
`global.istioNamespace` must equal the Helm release namespace. With the existing
strategy, `mesh.install=false` leaves installation, upgrades, extension providers,
GatewayClass and injection configuration to the platform owner. Both strategies
can consume the feature and traffic overlays above.

Ingress has a separate data-plane choice: `mesh.ingress.enabled` installs the
pinned legacy Istio gateway dependency, while `mesh.ingress.gatewayAPI.enabled`
creates Gateway API resources for an existing GatewayClass. They are mutually
exclusive. Cross-network deployments add the dedicated multicluster reference;
same-network clusters can use direct peers without an east-west gateway.

## Traffic strategy reference values

The traffic reference separates three authorities: Graph route weights remain
administrator- or adaptation-owned, locality policy chooses among healthy
endpoints without rewriting those weights, and event copulses move established
long-lived connections while Istio balances only new connections.

## Feature reference values

The feature reference intentionally enables the options together so their
provider names, policy selectors and egress boundaries are visible in one typed
file. In production, copy only the sections you need. In particular, a
`ServiceEntry` registers a destination and `Sidecar` limits proxy configuration;
neither blocks bypass traffic without NetworkPolicy or external firewall rules.

## Graph route resilience

Each `spec.traffic[]` route may declare `resilience`. Polyad persists the
resulting `DestinationRule` and `VirtualService` with the same owner reference,
generation fence and lifecycle as its weighted subsets.

```yaml
traffic:
  - name: pipeline
    source: producer
    service: pipeline-entry
    port: 8080
    destinations:
      - {target: workers/replica-0, weight: 50}
      - {target: workers/replica-1, weight: 50}
    resilience:
      outlierDetection: true
      consecutive5xxErrors: 5
      intervalSeconds: 10
      baseEjectionSeconds: 30
      maxEjectionPercent: 50
      maxConnections: 1024
      maxPendingRequests: 128
      maxRequests: 1024
      retries: 2
      perTryTimeoutSeconds: 2
      timeoutSeconds: 5
```

Zero connection or request limits retain Istio defaults. Zero retries and zero
total timeout omit those `VirtualService` fields. Retries apply to every request
matched by the route, so enable them only when the application protocol is
idempotent or supplies its own deduplication key. Endpoint ejection changes the
set of usable backends but never rewrites the administrator-approved percentage
weights. Application useful-work measurements remain the authority for adaptive
weight changes.

## Proxy telemetry

`mesh.telemetry.enabled` creates one workload-scoped Istio `Telemetry` resource
for Polyad operator Pods. Proxy metrics use `metricsProviders`; filtered access
logs default to server errors and requests taking at least one second. Proxy
traces are independently enabled and sampled:

```yaml
mesh:
  telemetry:
    enabled: true
    metricsProviders: [prometheus]
    accessLogging:
      enabled: true
      providers: [envoy]
      filter: "response.code >= 500 || response.duration >= duration('1s')"
    tracing:
      enabled: true
      providers: [otel]
      randomSamplingPercentage: 1
```

Provider names must exist in Istio's mesh configuration. This proxy telemetry
complements Polyad's OpenTelemetry spans and useful-work metrics; it does not
replace either one.

## Local-first multicluster traffic

`mesh.multicluster.routing.enabled` adds outlier detection and locality-aware
load balancing to every enabled Polyad API Service. The event Service receives
the same policy inside its existing least-request `DestinationRule`.

- `LocalFirst` uses Istio's region, zone and subzone priorities.
- `Failover` additionally requires one or more `{from, to}` region mappings.
- `Distributed` requires one or more `{from, to}` locality-weight mappings.

```yaml
mesh:
  multicluster:
    enabled: true
    routing:
      enabled: true
      mode: Failover
      failover:
        - {from: us-east-1, to: us-west-2}
```

The policy prefers or distributes healthy endpoints; it does not alter graph
traffic weights. Ensure nodes carry Kubernetes region and zone labels and all
clusters use consistent locality names.

## Ingress JWT and Gateway API

JWT validation is an optional layer in front of Polyad's existing API-key
authorization. It requires an exact issuer and may restrict audiences:

```yaml
mesh:
  ingress:
    jwt:
      enabled: true
      issuer: https://identity.example.com/
      audiences: [polyad-api]
```

The legacy `mesh.ingress.enabled` path retains the managed Istio
`Gateway`/`VirtualService` and its upstream gateway Deployment. To use the
standards-based replacement, leave that field false and enable:

```yaml
mesh:
  enabled: true
  ingress:
    hosts: [polyad.example.com]
    tlsSecret: polyad-ingress-tls
    gatewayAPI:
      enabled: true
      className: istio
```

This creates a Kubernetes `Gateway` and `HTTPRoute`. Istio provisions the data
plane through the selected GatewayClass. Do not also enable `api.gateway`; the
mesh route already exposes the composition and event APIs.

## Proxy configuration scoping

`mesh.sidecar.enabled` creates a workload-scoped Istio `Sidecar` for operator
Pods. Its `egressHosts` list reduces the configuration delivered to their
proxies. The default `./*` retains Services in the release namespace; explicitly
add the telemetry collector, shared infrastructure namespaces and any external
registry entries the operator calls.

Sidecar configuration scoping is not an outbound firewall. Use NetworkPolicy and
the egress controls below when traffic must be enforced.

## Explicit external destinations and egress gateway

Direct registration creates namespace-private `ServiceEntry` resources:

```yaml
mesh:
  egress:
    enabled: true
    destinations:
      - {name: payments, host: payments.example.com, port: 443, protocol: TLS}
```

Set `mesh.egress.gateway.enabled: true` to install a dedicated gateway and route
the declared TLS/443 destinations through it. The chart deliberately limits the
managed passthrough path to TLS on port 443. Combine it with NetworkPolicy or
infrastructure firewall rules that allow workloads to reach the gateway and
prevent direct external connections; a `ServiceEntry` alone is a registry entry,
not an enforcement boundary.

## Authorization observation

AUDIT and dry-run DENY policies let operators measure a policy before changing
enforcement:

```yaml
mesh:
  authorization:
    audit:
      enabled: true
      methods: [POST]
      paths: [/v1/*]
    dryRunDeny:
      enabled: true
      methods: [POST]
      paths: [/v1/compositions]
```

Both policies require at least one path and select only this release's operator
Pods. Dry-run results are diagnostic Istio logs, metrics and trace attributes;
they are not used as a Polyad correctness or admission signal.
