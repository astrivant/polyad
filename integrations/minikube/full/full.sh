#!/usr/bin/env bash
# Install or upgrade the local root as a bounded single-cluster HA feature lab.
set -euo pipefail

FULL_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$FULL_DIR/../../.." && pwd)"
PROFILE="${POLYAD_MINIKUBE_PROFILE:-polyad}"
NAMESPACE=polyad
STATE_DIR="$PROJECT_ROOT/.cache/minikube/full/$PROFILE"
CHART="$PROJECT_ROOT/charts/polyad"
TIMEOUT="${POLYAD_MINIKUBE_TIMEOUT:-15m}"
umask 077

##
# Run Kubernetes commands only against the selected profile and release namespace.
# args::string[] -> ret::exit_code
kube() {
    kubectl --context "$PROFILE" --namespace "$NAMESPACE" "$@"
}

##
# Ensure the release namespace is active before creating any namespaced credentials.
# -> ret::exit_code
prepare_namespace() {
    local namespace

    # Only NotFound means a namespace should be created. Authorization and API
    # errors must stop bootstrap, not be mistaken for an absent namespace.
    namespace="$(kube get namespace "$NAMESPACE" --ignore-not-found -o json)"
    if [[ -z "$namespace" ]]; then
        kube create namespace "$NAMESPACE" >/dev/null
    elif jq -e '.metadata.deletionTimestamp != null' <<<"$namespace" >/dev/null; then
        printf 'Namespace %s is terminating; wait for deletion to finish before retrying\n' "$NAMESPACE" >&2
        exit 1
    fi
    kube wait "namespace/$NAMESPACE" --for=jsonpath='{.status.phase}'=Active --timeout "$TIMEOUT"
}

##
# Generate a dedicated API token only when its Kubernetes Secret does not exist.
# name::string -> ret::exit_code
ensure_token() {
    local name="$1"
    if kube get secret "$name" -o name >/dev/null 2>&1; then return; fi
    openssl rand -hex 32 | tr -d '\n' >"$STATE_DIR/$name.token"
    kube create secret generic "$name" --from-file="token=$STATE_DIR/$name.token" >/dev/null
}

##
# Keep API, observer, record-encryption and local TLS keys out of Helm values and logs.
# -> ret::exit_code
prepare_credentials() {
    local name
    for name in polyad-api polyad-events polyad-metrics polyad-observer; do ensure_token "$name"; done
    if ! kube get secret polyad-record-encryption -o name >/dev/null 2>&1; then
        openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out "$STATE_DIR/private.pem" 2>/dev/null
        openssl pkey -in "$STATE_DIR/private.pem" -pubout -out "$STATE_DIR/public.pem"
        kube create secret generic polyad-record-encryption \
            --from-file="public.pem=$STATE_DIR/public.pem" --from-file="private.pem=$STATE_DIR/private.pem" >/dev/null
    fi
    if ! kube get secret polyad-ingress-tls -o name >/dev/null 2>&1; then
        openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj /CN=polyad.local \
            -addext subjectAltName=DNS:polyad.local -keyout "$STATE_DIR/ingress.key" -out "$STATE_DIR/ingress.crt" 2>/dev/null
        kube create secret tls polyad-ingress-tls --key="$STATE_DIR/ingress.key" --cert="$STATE_DIR/ingress.crt" >/dev/null
    fi
}

##
# Resolve only committed chart dependencies with isolated repository metadata.
# -> ret::exit_code
prepare_charts() {
    export HELM_REPOSITORY_CONFIG="$STATE_DIR/helm/repositories.yaml"
    export HELM_REPOSITORY_CACHE="$STATE_DIR/helm/repository-cache"
    mkdir -p "$HELM_REPOSITORY_CACHE"
    bash "$PROJECT_ROOT/scripts/tooling/build-chart-dependencies.sh" "$CHART" "$FULL_DIR/chart" >&2
}

##
# Wait for HA controllers, synchronous storage and the reserved root graph.
# -> ret::exit_code
test_full() {
    local deployment
    for deployment in polyad-polyad polyad-dragonfly-operator polyad-polyad-observer istiod \
        keda-operator keda-operator-metrics-apiserver keda-admission-webhooks; do
        kube rollout status "deployment/$deployment" --timeout "$TIMEOUT"
    done
    kube wait cluster/polyad-state --for=condition=Ready --timeout "$TIMEOUT"
    kube wait pods -l app=polyad-queue --for=condition=Ready --timeout "$TIMEOUT"
    kube wait polygraph/polyad-atlas --for=jsonpath='{.status.ready}'=true --timeout "$TIMEOUT"
    [[ "$(kube get deployment polyad-polyad -o jsonpath='{.status.availableReplicas}')" -ge 2 ]]
    [[ "$(kube get dragonfly polyad-queue -o jsonpath='{.spec.replicas}')" -ge 2 ]]
    kube exec deployment/polyad-polyad -c operator -- python -m polyad.operator.lifecycle.probes --ready
    kube exec -i deployment/polyad-polyad -c operator -- python - <<'PY'
import os
from pathlib import Path
from urllib.request import Request, urlopen

from polyad.auth.keys import Keyring

# Verify credentials without logging them. Mesh transport is checked separately
# by KEDA, observer and collector readiness/metrics; these are local API checks.
tokens = {
    endpoint: Path(os.environ[f"POLYAD_{endpoint}_TOKEN_FILE"]).read_text().strip()
    for endpoint in ("API", "EVENTS")
}
keys = Keyring.from_environment()
tokens["METRICS"] = next(token for _, key, token in keys.read() if key.name == "local-metrics")
for endpoint, port in (("API", 8090), ("EVENTS", 8091), ("METRICS", 8092)):
    request = Request(f"http://127.0.0.1:{port}/openapi.json", headers={"Authorization": f"Bearer {tokens[endpoint]}"})
    with urlopen(request, timeout=10) as response:
        assert response.status == 200, endpoint
    print(f"Authenticated {endpoint} API passed")
PY

    # A new Graph proves scheduling works after bootstrap. Keep failed fixtures
    # for diagnosis; remove only this run's generated resources after success.
    local smoke_name
    smoke_name="$(kube create -f "$PROJECT_ROOT/integrations/minikube/workload.yaml" -o jsonpath='{.metadata.name}')"
    [[ "$smoke_name" =~ ^polyad-minikube-smoke-[a-z0-9]+$ ]] || return 1
    kube create -f - <<EOF
apiVersion: polyad.astrivant.com/v1alpha1
kind: Graph
metadata:
  name: $smoke_name
  labels:
    app.kubernetes.io/part-of: polyad-minikube-smoke
spec:
  nodes:
    - name: worker
      kind: Workload
      ref: $smoke_name
EOF
    kube wait "graph/$smoke_name" --for=jsonpath='{.status.completed}'=true --timeout "$TIMEOUT"
    kube wait "graph/$smoke_name" --for=jsonpath='{.status.metrics.execution.completedNodes}'=1 --timeout "$TIMEOUT"
    kube delete "graph/$smoke_name" "workload/$smoke_name" --wait=true --timeout "$TIMEOUT"
    kube get pods,hpa,scaledobjects,clusters,dragonflies,polygraphs
    printf 'Polyad HA smoke test passed.\n'
}

[[ $# -eq 1 ]] || {
    printf 'Usage: %s enable|test|render\n' "$0" >&2
    exit 2
}
[[ "$PROFILE" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || {
    printf 'Invalid profile\n' >&2
    exit 2
}
mkdir -p "$STATE_DIR"
case "$1" in
    render)
        prepare_charts
        helm template polyad "$CHART" -n "$NAMESPACE" --kube-version 1.35.0 \
            -f "$PROJECT_ROOT/integrations/minikube/values.yaml" -f "$FULL_DIR/values.yaml" \
            --set-string "global.multiCluster.clusterName=$PROFILE"
        ;;
    enable)
        minikube --profile "$PROFILE" status
        kube wait nodes --all --for=condition=Ready --timeout "$TIMEOUT"

        # Never recapture the addon's elastic workers as base infrastructure
        # during a full-profile upgrade.
        # shellcheck source=integrations/minikube/autoscaler/paths.sh
        source "$FULL_DIR/../autoscaler/paths.sh"
        journal="$AUTOSCALER_STATE/provider/state.json"
        if [[ -f "$journal" ]]; then
            base_nodes="$(jq -er '.Base | keys | select(length > 0) | .[]' "$journal")"
        else
            inventory="$(kube get nodes -o json)"
            jq -e 'all(.items[]; .metadata.labels["minikube-autoscaler.astrivant.com/pool"] != "elastic")' <<<"$inventory" >/dev/null || {
                printf 'Elastic workers exist but their ownership journal is missing; restore addon state first\n' >&2
                exit 1
            }
            base_nodes="$(jq -er '.items[].metadata.name' <<<"$inventory")"
        fi
        while IFS= read -r node; do
            kube label node "$node" minikube-autoscaler.astrivant.com/pool=base --overwrite
            if kube get node "$node" -o json | jq -e '.metadata.labels | has("node-role.kubernetes.io/control-plane") | not' >/dev/null; then
                kube label node "$node" polyad.astrivant.com/minikube-worker=true --overwrite
            fi
        done <<<"$base_nodes"
        prepare_charts
        prepare_namespace
        prepare_credentials

        # Remember the explicit HA choice so ordinary enable/test commands do
        # not silently downgrade this profile to the standalone defaults.
        touch "$STATE_DIR/enabled"

        # Reuse the verified production build already published to the local
        # registry. This upgrade does not need a parallel memory-heavy image build.
        image="${POLYAD_MINIKUBE_IMAGE:-$(kube get deployment polyad-polyad -o jsonpath='{.spec.template.spec.containers[?(@.name=="operator")].image}')}"
        [[ "$image" == localhost:5000/polyad:* ]] || {
            printf 'Expected an existing local Polyad image\n' >&2
            exit 1
        }

        # The remembered HA choice can outlive a deleted Minikube cluster. Apply
        # the same CRDs with the same field owner as standalone bootstrap before
        # Helm encounters any Polyad or Dragonfly custom resources.
        kube apply --server-side --field-manager=polyad-minikube -f "$PROJECT_ROOT/charts/polyad-crds/crds"
        kube wait --for=condition=Established --timeout "$TIMEOUT" -f "$PROJECT_ROOT/charts/polyad-crds/crds"
        kube apply --server-side --field-manager=polyad-minikube-full -f "$FULL_DIR/chart/crds/provisioningrequests.yaml"
        helm upgrade --install polyad-lab "$FULL_DIR/chart" --kube-context "$PROFILE" -n "$NAMESPACE" --skip-crds --wait --timeout "$TIMEOUT"

        # KEDA CRDs must exist before Helm can discover ScaledObjects. The
        # prerequisite release already installs CNPG, VPA and Istio APIs.
        helm template polyad "$CHART/charts/keda-2.20.2.tgz" -n "$NAMESPACE" --set crds.install=true \
            --show-only templates/crds/crd-cloudeventsources.yaml \
            --show-only templates/crds/crd-clustercloudeventsources.yaml \
            --show-only templates/crds/crd-clustertriggerauthentications.yaml \
            --show-only templates/crds/crd-scaledjobs.yaml \
            --show-only templates/crds/crd-scaledobjects.yaml \
            --show-only templates/crds/crd-triggerauthentications.yaml |
            kube apply --server-side --field-manager=polyad-minikube-full -f -
        kube wait crd/scaledobjects.keda.sh --for=condition=Established --timeout "$TIMEOUT"
        common=(--kube-context "$PROFILE" -n "$NAMESPACE" --skip-crds --wait --timeout "$TIMEOUT"
            -f "$PROJECT_ROOT/integrations/minikube/values.yaml" -f "$FULL_DIR/values.yaml"
            --set-string "global.multiCluster.clusterName=$PROFILE"
            --set-string "operator.image.repository=${image%:*}" --set-string "operator.image.tag=${image##*:}")

        # Let KEDA's admission webhook become ready before creating its targets.
        helm upgrade --install polyad "$CHART" "${common[@]}" --set operator.autoscaling.enabled=false \
            --set operator.autoscaling.connections.enabled=false --set dragonfly.autoscaling.enabled=false \
            --set postgresql.autoscaling.enabled=false
        kube rollout status deployment/keda-admission-webhooks --timeout "$TIMEOUT"
        helm upgrade polyad "$CHART" "${common[@]}"
        test_full
        ;;
    test) test_full ;;
    *)
        printf 'Usage: %s enable|test|render\n' "$0" >&2
        exit 2
        ;;
esac
