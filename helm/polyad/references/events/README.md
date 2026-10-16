# events configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Overlay on your API-key/discovery configuration; this does not broaden grants.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/events/values.yaml
```

## Parameters

### Configuration

| Name                         | Description                                                                                                                                                                       | Value     |
| ---------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `events.enabled`             | **Type: boolean.** Serve authenticated SSE and event schema/settings endpoints on port 8091.                                                                                      | `true` |
| `events.maxEventBytes`       | **Type: integer.** Serialized UTF-8 event budget, including framing, from 1024 to 16777216 bytes; keep client max_event_bytes at least this large when its memory budget permits. | `1048576` |
| `events.readBatchSize`       | **Type: integer.** Records per replay read from 1 to 256; larger batches cost more memory per subscriber.                                                                         | `64` |
| `events.pollIntervalSeconds` | **Type: number.** Empty-stream delay from 0.05 to 5 seconds; shorter delays reduce latency and increase cache traffic.                                                            | `1` |
| `events.retention`           | **Type: integer.** Retained observation count from 100 to 100000; choose for your reconnect window and storage budget.                                                            | `10000` |
| `events.maxConnections`      | **Type: integer.** Combined active SSE/WebSocket subscriptions per replica from 1 to 128; each occupies an HTTP worker.                                                           | `16` |
| `events.websockets.enabled`  | **Type: boolean.** Enable WebSocket upgrades alongside SSE; keep false if subscribers only use SSE.                                                                               | `false` |
