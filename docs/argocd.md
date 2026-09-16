# Argo CD graph health

Polyad supplies Argo CD health checks for executable graphs, nested graphs,
composition receipts and reusable definitions. Each graph's health includes its
observed descendants, so a failed leaf can degrade the root application.

Argo CD evaluates custom health checks against one resource at a time. Polyad's
[status rollups](operator.md#graph-instance-status) carry the descendant observations
that make this possible.<sup>[\[1\]](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/#health-checks)</sup>

## Install into an existing Argo CD release

Run these commands from a Polyad checkout with its Python dependencies installed.
For an Argo CD installation managed through the upstream `argo-cd` Helm chart,
generate an additional values file:

```sh
poetry run python scripts/argocd-health.py --format helm > polyad-argocd-values.yaml
```

Include that file alongside your existing values in the release that deploys
**Argo CD**. It adds `configs.cm.resource.customizations.health` entries for each
Polyad kind. Keep the generated file in your GitOps repository and regenerate it
when upgrading Polyad. These values belong to the Argo CD chart, not the Polyad
operator chart.

For an existing `argocd-cm` managed directly:

```sh
poetry run python scripts/argocd-health.py > /tmp/polyad-argocd-patch.yaml
kubectl -n argocd patch configmap argocd-cm --type merge \
    --patch-file /tmp/polyad-argocd-patch.yaml
```

The merge patch changes only Polyad's health customization keys. If GitOps or an
Argo CD operator owns this ConfigMap, put the same customizations in its source
configuration so reconciliation retains them. No additional Lua library access,
network access or operator RBAC is required.<sup>[\[2\]](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/#way-1-define-a-custom-health-check-in-argocd-cm-configmap)</sup>

## What the health checks report

| Resource or observation | Argo CD health |
| --- | --- |
| Current `Ready` graph or successfully `Completed` finite graph | Healthy |
| Admission, gates, delays, pending capacity, startup or cleanup | Progressing |
| Missing status, stale generation or incomplete descendant observations | Progressing |
| Failed leaf, failed subgraph or invalid graph | Degraded |
| Suspended or stopped graph, including an observed suspended descendant | Suspended |
| Reusable Workload, Daemon, Resource, Gate, ShutdownPolicy, GraphRule or `templateOnly` graph | Healthy, with a reusable-definition message |
| Applied Rewrite | Healthy; execution belongs to the target graph |
| Composition receipt | Its mirrored root lifecycle: Healthy, Progressing or Degraded |

Graph messages include observed graph and leaf counts, pending leaves, ready
leaves, completed leaves and failures. A ready parent cannot hide a failing
subgraph. Both lifecycle and metrics must match the current generation before a
graph can report success. Suspension is reported after cleanup, rather than as
soon as `spec.suspend` is requested.

Health reflects the latest operator observation; it does not actively probe
services or replace the application's readiness checks. A Deployment that exceeds
its rollout progress deadline, or a lost PersistentVolumeClaim, also contributes
a failure to the graph's node status and rollup.

## Finding each leaf

Graph nodes become Kubernetes Jobs, Deployments, StatefulSets, Services, claims and nested graph
instances. Argo CD uses their owner references to show the resource hierarchy.
Jobs, Deployments, StatefulSets, Pods and claims retain Argo CD's built-in health checks; the
integration only registers checks for `polyad.astrivant.com` resources.

A reusable Workload definition can serve many executions, so its health does not
represent any one Job. Inspect each instance under its graph. Nodes waiting for
admission do not yet have Kubernetes resources: inspect the graph's `spec.nodes`,
`status.nodes` and `status.metrics` for their intent and observations.

Include the root Graph, PolyGraph, ReplicaGroup or Composition in the
Argo Application's desired manifests. Installing only the operator Helm chart
tracks the operator installation; it does not automatically add every graph in
the cluster to that Application. The Argo application controller needs read
access to the workload namespaces and resource kinds, and those resources must
not be excluded from its cluster cache.

Argo application health aggregates its directly managed resources. Polyad's root
health explicitly folds graph descendants; ownership alone does not propagate
child health into a custom resource.<sup>[\[1\]](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/#health-checks)</sup>

## Validate locally

The health checks are tested with the real Argo CD Lua interpreter, including
missing and stale status, nested failures, suspension, recurrence and templates.
CI installs the pinned CLI and runs these tests with the parallel Python suite.

```sh
bash scripts/install-asdf-tools.sh argocd
poetry run pytest tests/test_argocd_health.py
poetry run python scripts/argocd-health.py --format configmap > /tmp/polyad-argocd-cm.yaml
kubectl -n workloads get polygraph application -o yaml > /tmp/polyad-graph.yaml
argocd admin settings resource-overrides health /tmp/polyad-graph.yaml \
    --argocd-cm-path /tmp/polyad-argocd-cm.yaml
```

The final command evaluates local files and does not modify the cluster.
