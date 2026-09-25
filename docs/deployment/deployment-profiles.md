# Helm deployment profiles

<!-- toc:start -->
**Table of contents**

- [Combine reference values](#combine-reference-values)
- [One dense operator](#one-dense-operator)
- [HA in one cluster](#ha-in-one-cluster)
- [HA with a management cluster](#ha-with-a-management-cluster)
- [Helm-installed downstream operator workers](#helm-installed-downstream-operator-workers)
- [Source layout and switching profiles](#source-layout-and-switching-profiles)
<!-- toc:end -->

Set the chart's top-level `ha` Boolean: `false` (the default) runs one dense
operator; `true` runs at least two replicas. `architecture.mode` and
`rootControlPlane.enabled` configure optional layouts within HA.

| Profile | Operator resources in the Helm release cluster | Optional extensions |
| --- | --- | --- |
| `ha: false` (singular) | One dense operator replica and enabled endpoint Services | Workload APIs, networking, observers and optional PostgreSQL |
| `ha: true` | At least two dense operator replicas sharing queues and leases | Split components, root-managed execution pools, and the same optional integrations |

`operator.replicaCount: null` selects one replica for singular and two for HA.
An explicit count must be one for singular or at least two for HA. Singular
rejects operator autoscaling, split components and root-managed execution.
The HA operator CPU/memory HPA must keep at least two replicas. Distributed component
groups likewise require at least two copies and a minimum of two when scaled.

The profile controls operator replication. Configure failure-domain placement,
cache HA and database HA separately for the availability required by the cluster.
PostgreSQL stays optional, and `dragonfly.ha.enabled` and `postgresql.ha.enabled`
retain their own meanings. KEDA can use an existing installation or the
[optional chart dependency](local-services.md#install-keda-with-the-chart), enabled
separately with `keda.install: true`. Other infrastructure prerequisites remain
listed in their feature guides.

## Combine reference values

The chart includes [commented reference values](../../charts/polyad/README.md#reference-values)
for both profiles, split components, federation, root management, mesh transport,
observers and optional PostgreSQL. Each file highlights the related fields,
required credentials, prerequisites and cluster placement. The small examples
below remain useful for minimal installations; the reference files provide more
configuration detail. Each reference includes an editor schema directive and
typed `@param` descriptions, including choices and prerequisites. Helm validates
the merged values against `values.schema.json`. See also the
[authentication reference](../../charts/polyad/references/values-authentication.reference.yaml)
and [unauthenticated demo reference](../../charts/polyad/references/values-demo.reference.yaml).

Defaults, references and example values all declare their editor schema. Nested
resources, label selectors, ports and tolerations are validated too: use Boolean
`true`/`false` for flags, integers for counts, and strings for labels, environment
values and resource quantities. Quote whole CPU quantities such as `cpu: '1'`.
The partial overlay schema allows omitted settings; Helm checks the complete
configuration after merging defaults. Upstream chart override namespaces remain
extensible, while the upstream settings shipped in these files have explicit types.

Run `poetry run python scripts/validation/check-values.py` to validate every shipped values
file and detect untyped nested fields or duplicate YAML keys. Pre-commit and CI
run this check automatically. The CloudNativePG operator example uses its own
schema because it configures a separate infrastructure release.

For federation with independent execution operators, prepare the remote
credentials and operators described in [placement and ownership](multicluster.md#placement-and-ownership),
then combine HA with the federation registry in the source cluster:

```bash
helm upgrade --install polyad charts/polyad --kube-context east \
  --namespace polyad --create-namespace \
  --values charts/polyad/references/values-ha.reference.yaml \
  --values charts/polyad/references/values-federation.reference.yaml
```

For root-owned execution, use the root reference, which includes its own HA
selection and cluster registry. This example also splits the root into components:

```bash
helm upgrade --install polyad charts/polyad --kube-context management \
  --namespace polyad --create-namespace \
  --values charts/polyad/references/values-root-control-plane.reference.yaml \
  --values charts/polyad/references/values-components.reference.yaml
```

Prepare the [root credentials and shared cache](root-control-plane.md#install-and-register-clusters)
and [component prerequisites](components.md#install) first. The reference files
use the same `polyad-api`, `polyad-events` and `polyad-metrics` Secret names so
combining them preserves endpoint credentials. Add
[`values-postgresql.reference.yaml`](../../charts/polyad/references/values-postgresql.reference.yaml)
last to enable optional durable state, database HA and KEDA instance scaling;
install CloudNativePG and provide KEDA through an existing installation or the
[KEDA values reference](../../charts/polyad/references/values-keda.reference.yaml).

Helm merges files in order, with later values taking precedence. Lists replace
earlier lists entirely, including cluster registries, execution pools and network
peers. Put environment-specific overrides last. The observer and mesh references
use the local cluster name `east`; change it to `management` when combining them
with the root example, and adjust network names and peer registrations to match.
The mesh reference configures transport separately from federation placement.

These are explicit opt-in overrides, not new defaults or additional deployment
profiles. The HA reference enables [cache replication and KEDA scaling](dragonfly.md),
so provide KEDA separately or add the KEDA values reference; PostgreSQL is enabled only by
its separate reference. All native Helm resources still target the selected
Kubernetes context. Root-owned OperatorPools are declarations in the management
namespace; the root creates their execution Deployments in registered clusters.

## One dense operator

For local development, the [Minikube integration](../../integrations/minikube/README.md)
installs this non-HA pattern into a three-node cluster, including image builds,
single-instance Dragonfly, storage setup, and a Graph smoke test. Three nodes
do not imply three operator replicas. Follow its prerequisite and activation
steps instead of the manual Helm command below when you want that local setup.

```bash
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --values examples/deployment-profiles/singular.yaml
```

The [singular values](../../examples/deployment-profiles/singular.yaml) explicitly
select singular and one replica. Enable API, events, metrics and temporary
connections as needed. Their Services select the same dense operator Pods.

## HA in one cluster

```bash
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --values examples/deployment-profiles/ha.yaml
```

The [HA values](../../examples/deployment-profiles/ha.yaml) select two dense replicas.
To split responsibilities, use the [component values](../../examples/components/values.yaml)
instead. They also select HA and set `architecture.mode: Distributed`:

```bash
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  --values examples/components/values.yaml
```

Helm installs the bootstrap Deployment and declares the component Graph. The
operator creates gateway, executor and telemetry workloads in that same cluster
and namespace. Composition, event and temporary-connection Services select
gateway Pods; the metrics Service selects telemetry Pods. The bootstrap retains
its own health endpoint. See [component deployment prerequisites](components.md#install).

## HA with a management cluster

Select HA and enable `rootControlPlane.enabled`. The root may use either Dense
or Distributed mode. Prepare credentials and destination access using the
[root installation guide](root-control-plane.md#install-and-register-clusters),
then install the management release:

```bash
helm upgrade --install polyad charts/polyad --kube-context management \
  --namespace polyad --create-namespace \
  --values examples/root-control-plane/values.yaml
```

To split the root components, add `--values examples/components/values.yaml`.
Supply the endpoint Secrets named by the final merged values. Every native
resource rendered by this release is for the management cluster; changing `ha`
does not redirect Helm to a different Kubernetes context.

Declare remote execution capacity through `rootControlPlane.pools`:

```yaml
rootControlPlane:
  enabled: true
  kubeconfigSecret: root-access
  pools:
    - name: west-workers
      cluster: west
      replicas: 2
      nodeSelector:
        pool: execution
```

This is a fragment of the complete root values. `west` must be registered under
`federation.clusters`, which supplies its namespace and kubeconfig Secret.
The chart rejects duplicate pool names, multiple pools for one destination and
unregistered destinations. Each pool can specify resources, node selectors and
tolerations. Replicas may be zero to drain remote capacity while the root stays up.

| Resource | Created by | Location |
| --- | --- | --- |
| Dense root or bootstrap Deployment | Helm | Management cluster, release namespace |
| Component Graph and reusable definitions, when Distributed | Helm | Management cluster, release namespace |
| Gateway, executor and telemetry workloads, when Distributed | Root operator | Management cluster, release namespace |
| Enabled API, events, metrics and connection Services | Helm | Management cluster, release namespace |
| OperatorPool requests | Helm when listed in `rootControlPlane.pools` | Management cluster, release namespace |
| Execution Deployments and their projected credentials | Root operator | Each pool's registered workload cluster and namespace |
| Application Graphs and workloads | Executing operators under root authority | Declared graph destinations |

Remote pools run execution replicas, not another root planner or duplicate API
Services. They report through the root coordination path. Cluster infrastructure
and application source definitions still follow the root guide's prerequisites.
Do not install an independent operator over the same root-managed graph families.

Leave `pools: []` when managing OperatorPool resources separately. The standalone
[pool manifest](../../examples/root-control-plane/pool.yaml) illustrates that path;
do not also apply it over a Helm-owned pool with the same name. KEDA still targets
the root's OperatorPool `/scale`. Coordinate Helm/GitOps ownership of replica
counts with that autoscaler so configuration updates do not reset its intent.

## Helm-installed downstream operator workers

Use `worker.enabled: true` to install a downstream executor connected to the
root through the same chart. The
[worker reference](../../charts/polyad/references/values-worker.reference.yaml) separates root
identity from the hosting cluster and documents required local Secrets. Follow
the [attachment guide](helm-workers.md) to register its existing Deployment in
the root's reserved PolyGraph.

Choose `worker.scalingAuthority: Root` for root/KEDA replica management, with
no Helm replica count or local HPA. Choose `Local` to retain downstream scaling;
then `ha` and the normal operator replica/HPA settings apply locally. The root
pool's authority must match. Helm owns installation and upgrades in both cases.
Workers share root coordination, queues and endpoints and expose only health.

## Source layout and switching profiles

Templates live under `singular/`, `ha/`, `ha/distributed/`, `worker/`, `shared/` and
`multicluster/`. Shared Services and access controls use the same resolved profile
as the Deployment. The selected profile appears in Helm notes and the operator's
`polyad.astrivant.com/deployment-profile` label.

Set `ha: true` or `ha: false` when switching profiles. Bundled dependencies
retain their own feature flags; `ha` does not redirect resources across clusters.
A switch to singular also needs Dense mode, disabled
root management, empty pool declarations and disabled component autoscaling.
Removing a managed Graph or pool is a lifecycle operation that drains its owned
resources; do not treat a switch from those HA extensions as a replica-only edit.
