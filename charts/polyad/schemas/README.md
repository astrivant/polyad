# Validation schemas

These schemas support chart checks through `scripts/kubeconform.sh`; they do not
install APIs into a cluster.

- `gateway-gateway-v1.json` and `httproute-gateway-v1.json` contain the v1
  `openAPIV3Schema` from the corresponding standard CRDs in
  [Gateway API v1.5.1](https://github.com/kubernetes-sigs/gateway-api/tree/v1.5.1/config/crd/standard).
  Upstream is licensed under [Apache-2.0](https://github.com/kubernetes-sigs/gateway-api/blob/v1.5.1/LICENSE).
  The only added properties are `$schema` and a provenance `$comment`.
- `dragonfly-dragonflydb-v1alpha1.json` validates the chart's pinned Dragonfly API.

When updating Gateway API schemas, extract `spec.versions[name=v1].schema.openAPIV3Schema`
from both upstream CRDs and preserve their constraints and descriptions. Run the
chart tests and hypothesis-helm with kubeconform enabled.
