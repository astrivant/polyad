# keda configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Add to the root-control-plane reference to install KEDA in the management release.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/keda/values.yaml
```

## Parameters

### Configuration

| Name                                                               | Description                                                                                                                                 | Value  |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| `keda.install`                                                     | **Type: boolean.** True installs the pinned upstream dependency in the release namespace; false uses existing KEDA.                         | `true` |
| `keda.autoscaling.enabled`                                         | **Type: boolean.** Autoscale enabled KEDA metrics-server and webhook components in HA mode; false keeps their upstream replica counts fixed | `true` |
| `keda.autoscaling.metricsServer.enabled`                           | **Type: boolean.** Create an HPA when the bundled metrics server is enabled                                                                 | `true` |
| `keda.autoscaling.metricsServer.minReplicas`                       | **Type: integer.** Minimum metrics-server Pods; at least two for availability                                                               | `2` |
| `keda.autoscaling.metricsServer.maxReplicas`                       | **Type: integer.** Maximum metrics-server Pods; at least minReplicas and at most 32                                                         | `5` |
| `keda.autoscaling.metricsServer.targetCPUUtilizationPercentage`    | **Type: integer.** Target CPU utilization of the KEDA container relative to its CPU request                                                 | `70` |
| `keda.autoscaling.metricsServer.targetMemoryUtilizationPercentage` | **Type: integer or null.** Target memory utilization of the KEDA container relative to its memory request; null disables memory scaling     | `80` |
| `keda.autoscaling.webhooks.enabled`                                | **Type: boolean.** Create an HPA when bundled admission webhooks are enabled                                                                | `true` |
| `keda.autoscaling.webhooks.minReplicas`                            | **Type: integer.** Minimum admission-webhook Pods; at least two for availability                                                            | `2` |
| `keda.autoscaling.webhooks.maxReplicas`                            | **Type: integer.** Maximum admission-webhook Pods; at least minReplicas and at most 32                                                      | `5` |
| `keda.autoscaling.webhooks.targetCPUUtilizationPercentage`         | **Type: integer.** Target CPU utilization of the KEDA container relative to its CPU request                                                 | `70` |
| `keda.autoscaling.webhooks.targetMemoryUtilizationPercentage`      | **Type: integer or null.** Target memory utilization of the KEDA container relative to its memory request; null disables memory scaling     | `80` |
| `keda.autoscaling.behavior.scaleUp.stabilizationWindowSeconds`     | **Type: integer.** Seconds of scale-up recommendations to retain; zero responds immediately                                                 | `0` |
| `keda.autoscaling.behavior.scaleDown.stabilizationWindowSeconds`   | **Type: integer.** Seconds of scale-down recommendations to retain; 300 limits churn after a demand spike                                   | `300` |
| `keda.observation.enabled`                                         | **Type: boolean.** Include existing KEDA in the reserved root Graph; disable for roots without KEDA. Bundled KEDA is always included.       | `true` |
| `keda.observation.namespace`                                       | **Type: string.** Namespace containing existing KEDA; ignored when install is true.                                                         | `keda` |
| `keda.observation.deployments`                                     | **Type: array.** Existing Deployment names; adjust naming overrides and remove disabled components.                                         | `["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]` |
| `keda.observation.services`                                        | **Type: array.** Existing Service names; these are read-only observations, not Services created by Polyad.                                  | `["keda-operator", "keda-operator-metrics-apiserver", "keda-admission-webhooks"]` |
| `kedaOperator.operator.replicaCount`                               | **Type: integer.** Fixed operator Pods provide a leader and standby; at least two are required in HA mode.                                  | `2` |
| `kedaOperator.metricsServer.replicaCount`                          | **Type: integer.** Initial metrics-server Pods; in HA mode the HPA owns subsequent counts.                                                  | `2` |
| `kedaOperator.webhooks.enabled`                                    | **Type: boolean.** Keep admission validation enabled; disabling it also removes its observation targets.                                    | `true` |
| `kedaOperator.webhooks.replicaCount`                               | **Type: integer.** Initial webhook Pods; in HA mode the HPA owns subsequent counts.                                                         | `2` |
