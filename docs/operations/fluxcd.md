# Flux graph health

Flux can evaluate Polyad custom resources with CEL health expressions. The
integration uses the supported-type registry and the same graph lifecycle and
descendant observations as the [Argo CD integration](argocd.md).

## Configure a Kustomization

Generate the health configuration from a Polyad checkout:

```sh
poetry run python scripts/gitops/flux-health.py > polyad-flux-health.yaml
```

Merge the generated `spec.healthCheckExprs` into the **Flux Kustomization** that
applies your graph manifests. Retain any existing expressions for other kinds.
The output is a spec fragment, not a standalone Kubernetes resource or Helm
values file. It does not select resources or enable waiting on its own.

Enable `spec.wait: true` to check all resources applied by that Kustomization, or
select your root explicitly:

```yaml
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: application
  namespace: flux-system
spec:
  interval: 5m
  timeout: 10m
  path: ./workloads
  prune: true
  sourceRef:
    kind: GitRepository
    name: flux-system
  healthChecks:
    - apiVersion: polyad.astrivant.com/v1alpha1
      kind: PolyGraph
      name: application
      namespace: workloads
  # Merge the generated healthCheckExprs list here.
```

Custom health expressions require Flux 2.5 or later. When `wait: true`, Flux
ignores the explicit `healthChecks` list and checks all applied resources instead.
Choose a timeout that allows your graph's admission gates, capacity provisioning
and startup to finish.<sup>[\[1\]](https://fluxcd.io/flux/components/kustomize/kustomizations/#health-check-expressions)</sup><sup>[\[2\]](https://fluxcd.io/blog/2025/02/flux-v2.5.0/)</sup>

## Semantics

| Observation | Flux result |
| --- | --- |
| Ready graph or completed finite graph | Current |
| Current failed leaf, failed subgraph or invalid graph | Failed |
| Missing/stale observations, admission delays, startup or cleanup | InProgress |
| Suspended or stopped execution, including a suspended descendant | InProgress |
| Reusable definition or `templateOnly` graph | Current; no execution is implied |
| Applied Rewrite | Current |
| Composition receipt | Mirrored root lifecycle |
| Activation receipt | Ready or Completed: Current; Superseded (coalesced into another request): Current; Failed or Rejected: Failed; otherwise InProgress |
| TemporaryConnection | Active, Expired or Revoked: Current; Rejected, Invalid or Failed: Failed; Pending: InProgress |
| OperatorPool or RemoteScale | Ready: Current; Blocked: Failed; Pending: InProgress |
| DragonflyPool | Ready: Current; WaitingForReplication: InProgress |
| Executable ReplicaGroup | Graph health plus a completed scale reconciliation for the observed remote intent |

Flux has no separate suspended health result. Paused execution keeps dependent
Kustomizations waiting. Suspending a Flux Kustomization itself is a different
operation: it stops Flux reconciliation, rather than pausing Polyad workloads.

Failures are checked before success. The `inProgress` expression only handles
deletion, because Flux evaluates it before `failed`; a broad “not ready” expression
would hide failures. Current-generation metrics and descendant completeness are
required before executable graphs report success.

For [ReplicaGroups](../graphs/replication.md), `status.scaleCurrent` must also be
true. The operator records the exact remote intent it reconciled in
`status.observedRemoteScaleIntent`; Flux compares this with the
`polyad.astrivant.com/remote-scale-intent` annotation, treating an absent annotation
as an empty string. A remote request changes metadata without advancing the
group's generation, so checking `observedGeneration` alone would accept an old
Ready result. Adding, replacing or removing that intent now keeps Flux waiting
until local reconciliation observes it. Current failures still take precedence
over pending scaling. Reusable `templateOnly` groups remain definitions and do not
wait for their consumers.

[RemoteScale](../deployment/root-control-plane.md) health reflects the destination
operator's approval and reconciliation. A request blocked by local edits or a
missing grant reports Failed. These expressions read one resource's published
status; the operator must refresh inherited counts, remote observations and
descendant rollups. They do not independently query another cluster or impose a
wall-clock freshness deadline on observations.

Before the first operator status write, Flux's CEL evaluator can report an
unresolved `status` variable. Flux keeps waiting and retries until status appears,
or the health-check timeout expires. Expressions guard missing fields inside
status; they do not use the invalid `has(status)` top-level macro.<sup>[\[1\]](https://fluxcd.io/flux/components/kustomize/kustomizations/#health-check-expressions)</sup>

Native Jobs, Deployments, StatefulSets, Pods and claims retain Flux's built-in health checks.
The root graph carries descendant failures into Flux health even when those
resources were created by Polyad rather than applied by Flux. Installing only the
operator HelmRelease does not monitor every workload graph in the cluster.

## HelmRelease support

Flux helm-controller versions that expose `spec.healthCheckExprs` can use the same
generated list on a HelmRelease whose chart contains Polyad graph resources.
The Helm action must have waiting enabled and use `spec.waitStrategy.name: poller`.
Older HelmRelease CRDs do not provide this interface; use a Kustomization health
check in that case.<sup>[\[3\]](https://fluxcd.io/flux/components/helm/api/v2/)</sup>

Merge these fields into the HelmRelease alongside the generated expressions:

```yaml
spec:
  waitStrategy:
    name: poller
  install:
    disableWait: false
  upgrade:
    disableWait: false
  # Merge the generated healthCheckExprs list here.
```

Regenerate the expressions when upgrading Polyad so newly supported kinds and
status fields are included. Deploy the matching operator version before relying
on the new checks; executable ReplicaGroups wait for its scale observation fields.

## Validation

CI runs the generated expressions through Flux's actual CEL status evaluator,
with parallel cases for all Polyad kinds, stale generations and remote intents,
nested failures, activation receipts, cache replication, deletion and suspension.
The pinned Go dependencies live under
`tests/flux`; they are test tooling, not operator dependencies.

```sh
bash scripts/tooling/install-asdf-tools.sh golang
poetry run python scripts/gitops/flux-health.py --json > /tmp/polyad-flux-checks.json
POLYAD_FLUX_CHECKS=/tmp/polyad-flux-checks.json go -C tests/flux test -mod=readonly -parallel 8 ./...
```
