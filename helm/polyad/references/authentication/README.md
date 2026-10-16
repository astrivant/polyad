# authentication configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Named service and peer-operator credentials; no fixed limit on entry count.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/authentication/values.yaml
```

## Parameters

### Configuration

| Name                                      | Description                                                                                                                                                                                                                                | Value                   |
| ----------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----------------------- |
| `api.enabled`                             | **Type: boolean.** Serve authenticated composition requests on port 8090 through a ClusterIP Service                                                                                                                                       | `true` |
| `events.enabled`                          | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091                                                                                                                                          | `true` |
| `metrics.enabled`                         | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement                                                               | `true` |
| `metrics.authentication.enabled`          | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                                                                               | `true` |
| `metrics.authentication.existingSecret`   | **Type: string.** Existing or ESO-managed Secret containing the metrics token Must match the KEDA key below.                                                                                                                               | `polyad-metrics` |
| `metrics.authentication.secretKey`        | **Type: string.** Key containing the metrics bearer token                                                                                                                                                                                  | `token` |
| `keda.authentication.enabled`             | **Type: boolean.** Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics                                                                                                                 | `true` |
| `authentication.mode`                     | **Type: string.** Required enforces endpoint credentials; Disabled opts into an unauthenticated demo without HTTP request quotas Disabled bypasses all HTTP credential and quota checks for demos. Choices: Required, Disabled.            | `Required` |
| `authentication.backend`                  | **Type: string.** Builtin uses Polyad's Flask hooks; FlaskHTTPAuth loads the adapter included in official operator images Choices: Builtin, FlaskHTTPAuth.                                                                                 | `Builtin` |
| `authentication.storage.enabled`          | **Type: boolean.** Persist one-way key verifiers and policy records in PostgreSQL and enforce database revocation; raw tokens stay in Kubernetes Secrets Optional durable verification records and revocation; raw tokens stay in Secrets. | `false` |
| `authentication.storage.separateDatabase` | **Type: boolean.** Use an isolated authentication database; false reuses the state database and its role, requiring postgresql.enabled False shares PostgreSQL state storage and its application role.                                     | `true` |
| `authentication.storage.managed`          | **Type: boolean.** Provision a separate CloudNativePG Cluster; false uses the DSN from existingSecret False uses existingSecret instead of provisioning a dedicated CNPG Cluster.                                                          | `true` |
| `authentication.storage.existingSecret`   | **Type: string.** Externally managed authentication database connection Secret; used with separateDatabase=true and managed=false External DSN Secret in this namespace; ignored for shared state storage.                                 | `""` |
| `authentication.storage.secretKey`        | **Type: string.** DSN key in the external authentication database Secret                                                                                                                                                                   | `uri` |
| `authentication.storage.database`         | **Type: string.** Database name for a new managed authentication cluster                                                                                                                                                                   | `polyad-authentication` |
| `authentication.storage.username`         | **Type: string.** Sole application login owning the managed authentication database; PostgreSQL administrators retain administrative access Dedicated application owner; PUBLIC database/schema access is revoked.                         | `polyad_authentication` |
| `authentication.storage.size`             | **Type: string.** Persistent storage per managed authentication database instance PVC size per authentication database instance.                                                                                                           | `1Gi` |
| `authentication.storage.storageClass`     | **Type: string.** Empty uses the shared encrypted class when postgresql.encryptionAtRest is enabled, otherwise the cluster default; explicit overrides must match the encrypted selection.                                                 | `""` |
| `authentication.services`                 | **Type: array.** Service API keys with direction, Secret reference, endpoint scopes, outbound baseUrl and individual rate/concurrency limits                                                                                               | `[{"name": "pipeline", "direction": "Inbound", "existingSecret": "pipeline-polyad-key", "secretKey": "token", "endpoints": ["composition", "activations", "throughput", "discovery", "events", "topology"], "home": {"name": "pipeline", "namespace": "apps", "kind": "Graph"}, "graphs": [{"name": "pipeline", "namespace": "apps", "kind": "Graph", "descendants": true}], "workloads": [{"name": "processor", "namespace": "apps", "kind": "Daemon", "env": "POLYAD_API_KEY", "secret": "pipeline-polyad-key", "containers": ["main"]}], "requestsPerMinute": 120, "maxConcurrentRequests": 8}, {"name": "archive", "direction": "Outbound", "existingSecret": "archive-api-key", "secretKey": "token", "baseUrl": "https://archive.example.com/api", "requestsPerMinute": 60, "maxConcurrentRequests": 4}]` |
| `authentication.operators`                | **Type: array.** Peer operator API keys; Inbound, Outbound or Bidirectional, with HA-wide per-key lanes                                                                                                                                    | `[{"name": "west", "direction": "Bidirectional", "existingSecret": "west-peer-key", "secretKey": "token", "endpoints": ["composition", "observations"], "baseUrl": "https://west-polyad.example.com", "requestsPerMinute": 120, "maxConcurrentRequests": 8}, {"name": "keda", "direction": "Inbound", "existingSecret": "polyad-metrics", "secretKey": "token", "endpoints": ["metrics"], "requestsPerMinute": 600, "maxConcurrentRequests": 16}]` |
