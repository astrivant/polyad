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

- `authorizationpolicy-security-v1.json`, `peerauthentication-security-v1.json`,
  `gateway-networking-v1.json` and `virtualservice-networking-v1.json` are extracted
  from the pinned Istio 1.30.4 base chart's `files/crd-all.gen.yaml`. The only additions
  are `$schema` and a provenance `$comment`. Istio uses
  [Apache-2.0](https://github.com/istio/istio/blob/1.30.4/LICENSE).

- `triggerauthentication-keda-v1alpha1.json` is the v1alpha1 schema from
  [KEDA v2.20.0](https://github.com/kedacore/keda/blob/v2.20.0/config/crd/bases/keda.sh_triggerauthentications.yaml).
- `externalsecret-external-secrets-v1.json` is the v1 schema from
  [External Secrets helm-chart-2.10.0](https://github.com/external-secrets/external-secrets/blob/helm-chart-2.10.0/config/crds/bases/external-secrets.io_externalsecrets.yaml).
  Both preserve upstream constraints and descriptions, adding only `$schema`
  and a provenance `$comment`. These projects use
  [Apache-2.0](https://github.com/kedacore/keda/blob/v2.20.0/LICENSE) and
  [Apache-2.0](https://github.com/external-secrets/external-secrets/blob/helm-chart-2.10.0/LICENSE),
  respectively. They validate optional authentication and secret resources in
  hypothesis-helm's kubeconform checks.
