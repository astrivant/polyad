# demo configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Demonstration only: all enabled HTTP listeners accept calls without credentials.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/demo/values.yaml
```

## Parameters

### Configuration

| Name                               | Description                                                                                                                                                                                                                                   | Value      |
| ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------- |
| `ha`                               | **Type: boolean.** Run at least two operator replicas and permit split components or remote workers; false runs one dense operator                                                                                                            | `false` |
| `authentication.mode`              | **Type: string.** Required enforces endpoint credentials; Disabled opts into an unauthenticated demo without HTTP request quotas Choices: Required, Disabled.                                                                                 | `Disabled` |
| `authentication.backend`           | **Type: string.** Builtin uses Polyad's Flask hooks; FlaskHTTPAuth loads the adapter included in official operator images Choices: Builtin, FlaskHTTPAuth.                                                                                    | `Builtin` |
| `authentication.services`          | **Type: array.** Service API keys with direction, Secret reference, endpoint scopes, outbound baseUrl and individual rate/concurrency limits                                                                                                  | `[]` |
| `authentication.operators`         | **Type: array.** Peer operator API keys; Inbound, Outbound or Bidirectional, with HA-wide per-key lanes                                                                                                                                       | `[]` |
| `authentication.storage.enabled`   | **Type: boolean.** Persist one-way key verifiers and policy records in PostgreSQL and enforce database revocation; raw tokens stay in Kubernetes Secrets                                                                                      | `false` |
| `api.enabled`                      | **Type: boolean.** Serve authenticated composition requests on port 8090 through a ClusterIP Service                                                                                                                                          | `true` |
| `api.existingSecret`               | **Type: string.** Existing Secret with a token key authorizing namespace-scoped composition submissions and audit reads                                                                                                                       | `""` |
| `api.rateLimit.enabled`            | **Type: boolean.** Enforce shared Redis/Dragonfly composition request quotas on every logical shard                                                                                                                                           | `false` |
| `events.enabled`                   | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091                                                                                                                                             | `true` |
| `events.existingSecret`            | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                                                                                                                                                    | `""` |
| `metrics.enabled`                  | **Type: boolean.** Serve cached queue, write, inventory, component, backend and root-worker metrics on internal port 8092; optional families follow their feature enablement                                                                  | `true` |
| `metrics.authentication.enabled`   | **Type: boolean.** Require a dedicated bearer token on all metrics endpoints                                                                                                                                                                  | `false` |
| `keda.authentication.enabled`      | **Type: boolean.** Create a namespaced TriggerAuthentication referencing the metrics token; requires authenticated metrics                                                                                                                    | `false` |
| `connections.enabled`              | **Type: boolean.** Serve service-account authenticated temporary connections on port 8093 Enable to also demo TTL-bound edges without TokenReview or RBAC checks.                                                                             | `false` |
| `postgresql.enabled`               | **Type: boolean.** Persist graph state and tracked parameters in PostgreSQL                                                                                                                                                                   | `false` |
| `operator.serviceAccess.discovery` | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Cluster` |
