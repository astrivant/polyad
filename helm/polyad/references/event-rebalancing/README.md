# event-rebalancing configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Guide: ../../../../docs/operations/event-rebalancing.md

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/event-rebalancing/values.yaml
```

## Parameters

### Configuration

| Name                                     | Description                                                                                                                     | Value           |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| `operator.terminationGracePeriodSeconds` | **Type: integer.** Covers a 20-second preStop roll, normal shutdown and the sidecar's final drain.                              | `90` |
| `events.enabled`                         | **Type: boolean.** Serve the authorized application streams from the event operator group.                                      | `true` |
| `events.websockets.enabled`              | **Type: boolean.** Enable WebSocket upgrades on the existing event listener.                                                    | `true` |
| `events.rebalance.enabled`               | **Type: boolean.** Advertise membership and send controlled reconnection instructions.                                          | `true` |
| `events.rebalance.routing`               | **Type: string.** Use Service with Istio; choose Direct only for clients able to reach Pod IPs, and disable events.istio below. | `Service` |
| `events.rebalance.refreshSeconds`        | **Type: number.** Poll Kubernetes ready membership every five seconds.                                                          | `5` |
| `events.rebalance.automatic`             | **Type: boolean.** Roll on scale-out, scale-in or replacement; false retains manual annotation triggers.                        | `true` |
| `events.rebalance.batchPercent`          | **Type: integer.** Move up to ten percent of each replica's initial subscribers per batch, rounded up.                          | `10` |
| `events.rebalance.intervalSeconds`       | **Type: number.** Spread ordinary batches by two seconds and jitter client reconnects.                                          | `2` |
| `events.rebalance.cooldownSeconds`       | **Type: number.** Coalesce repeated ordinary triggers for one minute on each replica.                                           | `60` |
| `events.rebalance.drainSeconds`          | **Type: number.** Hold termination for twenty seconds while stopping admission and moving active subscriptions.                 | `20` |
| `events.rebalance.maxConnectionSeconds`  | **Type: number.** Zero disables periodic rotation; choose a value at least cooldownSeconds to bound ordinary connection age.    | `0` |
| `events.istio.enabled`                   | **Type: boolean.** Install a DestinationRule for this event Service; requires mesh.operator.enabled and Service routing.        | `true` |
| `events.istio.loadBalancer`              | **Type: string.** LEAST_REQUEST prefers fewer outstanding requests; ROUND_ROBIN is also available.                              | `LEAST_REQUEST` |
| `events.istio.warmupSeconds`             | **Type: number.** Ramp traffic to newly ready replicas over thirty seconds.                                                     | `30` |
| `mesh.enabled`                           | **Type: boolean.** Enable mesh support; install Istio separately or use mesh.install in your base values.                       | `true` |
| `mesh.operator.enabled`                  | **Type: boolean.** Enable native sidecars and strict mTLS; provide eventPrincipals in your deployment values.                   | `true` |
