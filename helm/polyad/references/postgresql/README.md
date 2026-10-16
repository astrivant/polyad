# postgresql configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Optional durable graph state and tracked parameters for any operator layout.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/postgresql/values.yaml
```

## Parameters

### Configuration

| Name                                            | Description                                                                                                                                                                                                         | Value                    |
| ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------ |
| `postgresql.enabled`                            | **Type: boolean.** Persist graph state and tracked parameters in PostgreSQL                                                                                                                                         | `true` |
| `postgresql.managed`                            | **Type: boolean.** Create a CloudNativePG Cluster; install its operator first Creates a CloudNativePG Cluster in the Helm release namespace.                                                                        | `true` |
| `postgresql.existingSecret`                     | **Type: string.** External database Secret containing the connection DSN when managed is false Managed databases use the generated <release>-state-app Secret.                                                      | `""` |
| `postgresql.secretKey`                          | **Type: string.** DSN key in the external database Secret                                                                                                                                                           | `uri` |
| `postgresql.database`                           | **Type: string.** Application database for graph state and optional event history; set only when bootstrapping a new managed cluster                                                                                | `polyad` |
| `postgresql.username`                           | **Type: string.** Dedicated application owner; PUBLIC database access is revoked and superuser access stays disabled                                                                                                | `polyad` |
| `postgresql.events.enabled`                     | **Type: boolean.** Archive approved application events in PostgreSQL; bounded live replay still uses Dragonfly Archives approved public observations before live delivery.                                          | `true` |
| `postgresql.events.retentionDays`               | **Type: integer.** Retain durable event history for this many days; graph snapshots retain current state independently Opportunistic bounded pruning during writes; not a backup policy. Minimum: 1. Maximum: 3650. | `30` |
| `postgresql.scope`                              | **Type: string.** State identity within the database; empty uses the release namespace and name Empty uses <release-namespace>/<release-name> as the state identity.                                                | `""` |
| `postgresql.maxConnections`                     | **Type: integer.** Maximum connections per managed database instance, including administration Per database instance, including administration. Minimum: 20. Maximum: 10000.                                        | `100` |
| `postgresql.storage.size`                       | **Type: string.** Persistent storage per PostgreSQL instance Persistent volume size per instance.                                                                                                                   | `20Gi` |
| `postgresql.storage.storageClass`               | **Type: string.** Empty uses the shared encrypted class when encryptionAtRest is enabled, otherwise the cluster default; explicit overrides must match the encrypted selection.                                     | `""` |
| `postgresql.encryptionAtRest.enabled`           | **Type: boolean.** Set true and choose an encrypted class before creating database PVCs; existing disks require migration.                                                                                          | `false` |
| `postgresql.encryptionAtRest.provider`          | **Type: string.** ExistingStorageClass uses an administrator-verified class; GKE provisions a class with a Cloud KMS key reference.                                                                                 | `ExistingStorageClass` |
| `postgresql.encryptionAtRest.storageClass`      | **Type: string.** Name of the verified encrypted class; required with ExistingStorageClass when enabled.                                                                                                            | `""` |
| `postgresql.encryptionAtRest.kmsKeyName`        | **Type: string.** GKE-only symmetric CryptoKey name; leave empty with ExistingStorageClass.                                                                                                                         | `""` |
| `postgresql.encryptionAtRest.diskType`          | **Type: string.** GKE-only disk type; choose one supported by your node machine family and CSI driver.                                                                                                              | `pd-balanced` |
| `postgresql.ha.enabled`                         | **Type: boolean.** Enable primary plus standby instances and synchronous replication Synchronous replication; independent of operator HA.                                                                           | `true` |
| `postgresql.ha.instances`                       | **Type: integer.** Total instances when HA is enabled and autoscaling is disabled Used when autoscaling is disabled; otherwise minInstances starts it. Minimum: 3. Maximum: 32.                                     | `3` |
| `postgresql.ha.topologyKey`                     | **Type: string.** Failure-domain label for required database pod anti-affinity Requires distinct nodes for database Pods.                                                                                           | `kubernetes.io/hostname` |
| `postgresql.resources.requests.cpu`             | **Type: string.** Kubernetes CPU quantity; requests reserve scheduling capacity and limits cap usage.                                                                                                               | `250m` |
| `postgresql.resources.requests.memory`          | **Type: string.** Kubernetes memory quantity; requests reserve capacity and limits bound container memory.                                                                                                          | `512Mi` |
| `postgresql.resources.limits.memory`            | **Type: string.** Kubernetes memory quantity; requests reserve capacity and limits bound container memory.                                                                                                          | `1Gi` |
| `postgresql.autoscaling.enabled`                | **Type: boolean.** Create a KEDA ScaledObject for the managed Cluster using operator connection counts Optional; set false for a fixed database instance count.                                                     | `true` |
| `postgresql.autoscaling.minInstances`           | **Type: integer.** Minimum instances; at least 3 with HA, never zero HA requires at least three; provide nodes/storage for the maximum. Minimum: 1. Maximum: 32.                                                    | `3` |
| `postgresql.autoscaling.maxInstances`           | **Type: integer.** Maximum database instances Minimum: 1. Maximum: 32.                                                                                                                                              | `6` |
| `postgresql.autoscaling.connectionsPerInstance` | **Type: integer.** Operator connections per desired database instance; does not add primary write capacity Operator connections, measured by the operator API. Minimum: 1.                                          | `20` |
| `metrics.enabled`                               | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement                                        | `true` |
| `metrics.authentication.enabled`                | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                                                        | `true` |
| `metrics.authentication.existingSecret`         | **Type: string.** Existing or ESO-managed Secret containing the metrics token Create this token Secret in the release namespace.                                                                                    | `polyad-metrics` |
| `keda.authentication.enabled`                   | **Type: boolean.** Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics KEDA scrapes operator metrics, never the database directly.                              | `true` |
