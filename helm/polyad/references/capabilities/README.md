# capabilities configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Opt-in TTL-bound sharing contracts reuse the connections and events listeners.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/capabilities/values.yaml
```

## Parameters

### Configuration

| Name                               | Description                                                                     | Value               |
| ---------------------------------- | ------------------------------------------------------------------------------- | ------------------- |
| `connections.enabled`              | **Type: boolean.** Enable projected-token intake on port 8093.                  | `true` |
| `connections.scope`                | **Type: string.** Limit publishers and target graphs to the operator namespace. | `OperatorNamespace` |
| `events.enabled`                   | **Type: boolean.** Enable scoped directory reads on port 8091.                  | `true` |
| `operator.serviceAccess.discovery` | **Type: string.** Allow granted services in related graph branches.             | `GraphTree` |
