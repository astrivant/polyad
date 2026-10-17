# websockets configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Overlay on your authentication/discovery configuration; this does not widen grants.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/websockets/values.yaml
```

## Parameters

### Configuration

| Name                        | Description                                                                                                                                  | Value  |
| --------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `events.enabled`            | **Type: boolean.** Enable the events Service; configure its bearer Secret or named API keys in your base values.                             | `true` |
| `events.websockets.enabled` | **Type: boolean.** Allow service clients to select WebSocket subscriptions; keep false when only SSE is needed.                              | `true` |
| `events.maxConnections`     | **Type: integer.** Combined SSE and WebSocket subscriber ceiling per replica; named-key concurrency limits remain shared across HA replicas. | `16` |
