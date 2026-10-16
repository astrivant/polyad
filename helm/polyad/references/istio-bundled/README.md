# istio-bundled configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Bundled Istio deployment strategy.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/istio-bundled/values.yaml
```

## Parameters

### Configuration

| Name                                                              | Description                                                                                            | Value     |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------ | --------- |
| `global.istioNamespace`                                           | **Type: string.** Namespace where this Helm release and its bundled Istio control plane are installed. | `default` |
| `mesh.enabled`                                                    | **Type: boolean.** Generate Polyad's mesh routing and security resources.                              | `true` |
| `mesh.install`                                                    | **Type: boolean.** Install the pinned Istio base and istiod dependencies with this release.            | `true` |
| `mesh.operator.enabled`                                           | **Type: boolean.** Add the native Istio sidecar and strict-mTLS policies to operator Pods.             | `true` |
| `mesh.proxyResources.cpu`                                         | **Type: string.** Proxy CPU request and limit; tune from observed proxy saturation.                    | `100m` |
| `mesh.proxyResources.memory`                                      | **Type: string.** Proxy memory request and limit; tune from observed proxy working set.                | `128Mi` |
| `istiod.env.ENABLE_NATIVE_SIDECARS`                               | **Type: string.** Use Kubernetes native sidecars for deterministic startup and shutdown ordering.      | `true` |
| `istiod.meshConfig.enableAutoMtls`                                | **Type: boolean.** Automatically originate mTLS when destinations support it.                          | `true` |
| `istiod.meshConfig.defaultConfig.holdApplicationUntilProxyStarts` | **Type: boolean.** Keep the application from racing the proxy during Pod startup.                      | `true` |
