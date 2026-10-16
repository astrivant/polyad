# worker configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Install this release in the downstream cluster, namespace workloads.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/worker/values.yaml
```

## Parameters

### Configuration

| Name                                 | Description                                                                                                                                    | Value                                    |
| ------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------- |
| `worker.enabled`                     | **Type: boolean.** Run as a root-connected executor; never elect a root or expose application APIs here.                                       | `true` |
| `worker.rootNamespace`               | **Type: string.** Match the management release namespace, not this release's namespace.                                                        | `polyad` |
| `worker.rootClusterName`             | **Type: string.** Match the root's global.multiCluster.clusterName.                                                                            | `management` |
| `worker.rootDeployment`              | **Type: string.** Match the management Deployment name, normally ROOT_RELEASE-polyad.                                                          | `polyad-polyad` |
| `worker.rootGraph`                   | **Type: string.** Match the management reserved PolyGraph, normally ROOT_RELEASE-atlas.                                                        | `polyad-atlas` |
| `worker.poolName`                    | **Type: string.** Only this root-namespace OperatorPool may attach the Deployment.                                                             | `west-workers` |
| `worker.scalingAuthority`            | **Type: string.** Root delegates replicas to the matching pool/KEDA. Local keeps replicaCount and optional HPA here; match the pool's setting. | `Root` |
| `global.multiCluster.clusterName`    | **Type: string.** Hosting cluster identity; keep distinct from the root identity.                                                              | `west` |
| `rootControlPlane.enabled`           | **Type: boolean.** This release is a worker, so it does not provision pools or become a root.                                                  | `false` |
| `rootControlPlane.kubeconfigSecret`  | **Type: string.** Existing DOWNSTREAM namespace Secret, key config, granting access to the root API and namespace.                             | `root-access` |
| `rootControlPlane.endpoints.api`     | **Type: string.** Reachable root composition route advertised to workloads; match root configuration.                                          | `https://polyad.example.com/composition` |
| `rootControlPlane.endpoints.events`  | **Type: string.** Reachable root event route advertised to workloads.                                                                          | `https://polyad.example.com/events` |
| `rootControlPlane.endpoints.metrics` | **Type: string.** Reachable root metrics route advertised to workloads and KEDA.                                                               | `https://polyad.example.com/metrics` |
| `federation.enabled`                 | **Type: boolean.** Use the same complete cluster registry as the root; replicas can execute any assigned graph family.                         | `true` |
| `federation.clusters`                | **Type: array.** Secret names are local to this downstream release; contents must identify the root's registered destinations.                 | `[{"name": "west", "namespace": "workloads", "kubeconfigSecret": "west-access"}]` |
| `dragonfly.enabled`                  | **Type: boolean.** Reuse root queues and observations; never install an independent worker cache.                                              | `false` |
| `dragonfly.existingSecret`           | **Type: string.** Existing DOWNSTREAM Secret, key url, pointing at the same reachable authenticated cache as the root.                         | `root-cache` |
