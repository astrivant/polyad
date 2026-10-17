# cheeger configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

GraphPolicy cheegerComputation.reduction.enabled must also be true.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/cheeger/values.yaml
```

## Parameters

### Configuration

| Name                                             | Description                                                                                                         | Value            |
| ------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------- | ---------------- |
| `operator.cheeger.maxVertices`                   | **Type: integer.** Largest original boundary allowed into a calculation.                                            | `20` |
| `operator.cheeger.maxCuts`                       | **Type: integer.** Exact-cut allowance; quotient cuts are additional work.                                          | `524287` |
| `operator.cheeger.timeoutSeconds`                | **Type: number.** Cooperative exact-search deadline, not spectral preemption.                                       | `5` |
| `operator.cheeger.reduction.enabled`             | **Type: boolean.** Administrator opt-in; chart defaults remain false.                                               | `true` |
| `operator.cheeger.reduction.strategy`            | **Type: string.** Preferred AdaptivePID; choose CacheFirst for legacy scheduling.                                   | `AdaptivePID` |
| `operator.cheeger.reduction.targetSeconds`       | **Type: number.** Soft mean calculation-time objective, not a latency guarantee.                                    | `0.0015` |
| `operator.cheeger.reduction.feedback`            | **Type: string.** Prefer certificate-gap feedback; ComputationTime preserves the original time-targeted outer loop. | `CertificateGap` |
| `operator.cheeger.reduction.targetRelativeError` | **Type: number.** Soft certified relative-error objective, not a promised error or a policy threshold.              | `0.25` |
| `operator.cheeger.reduction.maxVertices`         | **Type: integer.** Dense spectral boundary size ceiling.                                                            | `256` |
| `operator.cheeger.reduction.components`          | **Type: integer.** Spectral embedding dimension ceiling.                                                            | `4` |
| `operator.cheeger.reduction.supernodes`          | **Type: integer.** Quotient search size ceiling.                                                                    | `8` |
| `operator.cheeger.reduction.cache`               | **Type: boolean.** Cache partitions and maintain adaptive refresh history.                                          | `true` |
| `operator.cheeger.reduction.cacheEntries`        | **Type: integer.** Independent finite capacities for partitions and PID histories.                                  | `128` |
| `operator.cheeger.reduction.maxEdgeChurn`        | **Type: number.** Churn guard still applies regardless of the requested cache rate.                                 | `0.1` |
