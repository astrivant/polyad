# multicluster configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Istio sidecar transport: east and west reside on separate networks.

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/multicluster/values.yaml
```

## Parameters

### Configuration

| Name                                                              | Description                                                                                                                                                                                                        | Value              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------ |
| `global.istioNamespace`                                           | **Type: string.** Istio control-plane namespace; must equal the release namespace when mesh.install is enabled                                                                                                     | `istio-system` |
| `global.meshID`                                                   | **Type: string.** Shared mesh identity across participating clusters Shared across participating clusters.                                                                                                         | `application-mesh` |
| `global.network`                                                  | **Type: string.** Network containing this cluster; different networks use east-west gateways The local cluster's network.                                                                                          | `east-network` |
| `global.multiCluster.clusterName`                                 | **Type: string.** Unique, stable cluster identity used by Istio and Polyad Must agree with this operator's federation identity.                                                                                    | `east` |
| `mesh.enabled`                                                    | **Type: boolean.** Allow graph HTTP and service-identity authorization and generate Istio security resources                                                                                                       | `true` |
| `mesh.install`                                                    | **Type: boolean.** Install the pinned upstream Istio base and istiod dependencies; requires mesh.enabled                                                                                                           | `false` |
| `mesh.multicluster.enabled`                                       | **Type: boolean.** Enable the existing or bundled sidecar mesh's multicluster configuration                                                                                                                        | `true` |
| `mesh.multicluster.eastWest.enabled`                              | **Type: boolean.** Install a dedicated east-west gateway for separate networks Installs the bundled gateway, even with mesh.install=false.                                                                         | `true` |
| `mesh.multicluster.eastWest.portName`                             | **Type: string.** Istio Gateway listener name; protocol TLS and AUTO_PASSTHROUGH mode are required by this integration                                                                                             | `tls` |
| `mesh.multicluster.eastWest.hosts`                                | **Type: array.** Service SNI suffixes exposed through AUTO_PASSTHROUGH Service SNI suffixes exposed through the gateway.                                                                                           | `["*.local"]` |
| `mesh.multicluster.peers`                                         | **Type: array.** Remote transport registrations; see docs/deployment/multicluster.md for same-network and gateway configurations                                                                                   | `[{"name": "west", "mode": "Gateway", "cidrs": ["192.0.2.20/32"], "gatewayNamespace": "polyad"}]` |
| `istioEastWestGateway.name`                                       | **Type: string.** Stable identity within this configuration; choose a distinct name.                                                                                                                               | `polyad-eastwest` |
| `istioEastWestGateway.networkGateway`                             | **Type: string or null.** Null retains the pinned upstream Istio profile default. Must match global.network.                                                                                                       | `east-network` |
| `istioEastWestGateway.labels.istio`                               | **Type: string.** Istio selector identifying the east-west gateway Pods. Required selector for the generated Istio Gateway.                                                                                        | `polyad-eastwest` |
| `istioEastWestGateway.labels.networking.istio.io/gatewayPort`     | **Type: string.** Advertised gateway TLS Service port; update it together with the Service port. Must match the tls Service port.                                                                                  | `15443` |
| `istioEastWestGateway.networkGatewayPorts.status-port.port`       | **Type: integer.** Service port exposed to callers; keep matching gateway labels and peer destination ports synchronized.                                                                                          | `15021` |
| `istioEastWestGateway.networkGatewayPorts.status-port.targetPort` | **Type: integer.** Pod listener port behind the Service.                                                                                                                                                           | `15021` |
| `istioEastWestGateway.networkGatewayPorts.tls.port`               | **Type: integer.** Service port exposed to callers; keep matching gateway labels and peer destination ports synchronized. Configurable; update the gatewayPort label at the same time. Minimum: 1. Maximum: 65535. | `15443` |
| `istioEastWestGateway.networkGatewayPorts.tls.targetPort`         | **Type: integer.** Pod listener port behind the Service. Pod listener port; may differ from the Service port. Minimum: 1. Maximum: 65535.                                                                          | `15443` |
