# istio-existing configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Existing Istio deployment strategy.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/istio-existing/values.yaml
```

## Parameters

### Configuration

| Name                                | Description                                                                                           | Value                |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------- | -------------------- |
| `global.istioNamespace`             | **Type: string.** Namespace of the platform-managed Istio control plane.                              | `istio-system` |
| `mesh.enabled`                      | **Type: boolean.** Generate Polyad routing and security resources for the existing mesh.              | `true` |
| `mesh.install`                      | **Type: boolean.** Keep false so this release does not own Istio base or istiod.                      | `false` |
| `mesh.operator.enabled`             | **Type: boolean.** Require the namespace's existing injection mechanism to provide operator sidecars. | `true` |
| `mesh.ingress.hosts`                | **Type: array.** Public DNS names accepted by the managed HTTPRoute.                                  | `["polyad.example.com"]` |
| `mesh.ingress.tlsSecret`            | **Type: string.** Existing TLS Secret referenced by the Gateway listener.                             | `polyad-ingress-tls` |
| `mesh.ingress.gatewayAPI.enabled`   | **Type: boolean.** Create a Gateway and HTTPRoute through the existing GatewayClass.                  | `true` |
| `mesh.ingress.gatewayAPI.className` | **Type: string.** GatewayClass managed by the platform's Istio installation.                          | `istio` |
