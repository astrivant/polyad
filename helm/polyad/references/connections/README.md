# connections configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Temporary connections require consent from their participating services.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/connections/values.yaml
```

## Parameters

### Configuration

| Name                                                | Description                                                                                                                                                                                                                                   | Value           |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `connections.enabled`                               | **Type: boolean.** Serve service-account authenticated temporary connections on port 8093                                                                                                                                                     | `true` |
| `connections.scope`                                 | **Type: string.** Allowed caller and target namespaces: Cluster, OperatorNamespace or Namespace                                                                                                                                               | `Cluster` |
| `connections.namespace`                             | **Type: string.** Allowed namespace when scope is Namespace; otherwise empty                                                                                                                                                                  | `""` |
| `connections.maxTtlSeconds`                         | **Type: integer.** Maximum connection lifetime from receipt creation, capped at 86400 seconds                                                                                                                                                 | `3600` |
| `connections.retentionSeconds`                      | **Type: integer.** Retain terminal receipts for retry identity and audit before cleanup                                                                                                                                                       | `3600` |
| `connections.pulses.cooldownSeconds`                | **Type: number.** Shared cooldown window in seconds; zero disables additional pulse pacing. Refused work retries from fresh state.                                                                                                            | `10` |
| `connections.pulses.burst`                          | **Type: integer.** Maximum new pulses admitted in one cooldown window; duplicate retries do not extend the window.                                                                                                                            | `2` |
| `events.enabled`                                    | **Type: boolean.** Serve namespace graph observations through a separate SSE Service on port 8091                                                                                                                                             | `true` |
| `events.existingSecret`                             | **Type: string.** Existing Secret containing a token key for read-only event subscriptions                                                                                                                                                    | `polyad-events` |
| `operator.serviceAccess.discovery`                  | **Type: string.** Disabled denies access; SameGraph stays in the home boundary; GraphTree allows related graphs in its cluster; Cluster allows that cluster; Atlas allows registered clusters. Explicit graph grants and consent still apply. | `Cluster` |
| `operator.writeQueue.reconciliationCooldownSeconds` | **Type: number.** Shared cooldown window in seconds; zero disables additional pulse pacing. Refused work retries from fresh state.                                                                                                            | `1` |
| `operator.writeQueue.reconciliationBurst`           | **Type: integer.** Maximum new pulses admitted in one cooldown window; duplicate retries do not extend the window.                                                                                                                            | `2` |
