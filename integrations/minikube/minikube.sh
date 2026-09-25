#!/usr/bin/env bash
# Run the canonical Polyad chart in a dedicated, non-HA Minikube profile.
set -euo pipefail

INTEGRATION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$INTEGRATION_DIR/../.." && pwd)"
PROFILE="${POLYAD_MINIKUBE_PROFILE:-polyad}"
NAMESPACE="${POLYAD_MINIKUBE_NAMESPACE:-polyad}"
DRIVER="${POLYAD_MINIKUBE_DRIVER:-docker}"
NODES="${POLYAD_MINIKUBE_NODES:-3}"
TIMEOUT="${POLYAD_MINIKUBE_TIMEOUT:-10m}"
EXTRA_VALUES="${POLYAD_MINIKUBE_VALUES:-}"
KUBERNETES_VERSION="v$(bash "$PROJECT_ROOT/scripts/tooling/tool-version.sh" kubectl)"
CHART="$PROJECT_ROOT/charts/polyad"
HELM_CACHE="$PROJECT_ROOT/.cache/minikube/helm"
IMAGE_REPOSITORY=polyad
IMAGE_TAG=minikube

usage() {
    cat <<'EOF'
Usage: integrations/minikube/minikube.sh COMMAND

  start    Start three nodes, build/load Polyad, install the chart, and smoke-test it.
  enable   Rebuild/load Polyad, refresh CRDs, and upgrade the running local release.
  test     Check cluster health and execute a fresh, uniquely named Graph.
  status   Show nodes, controller/cache workloads, and graph boundaries.
  render   Build locked dependencies and print manifests without accessing a cluster.
  disable  Uninstall Polyad only after application boundaries have been drained.
  stop     Stop only this profile, preserving its workloads and storage.
  delete   Delete only this profile, including its local volumes.

See integrations/minikube/README.md for prerequisites and environment settings.
EOF
}

fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

require() {
    local executable
    for executable in "$@"; do
        command -v "$executable" >/dev/null 2>&1 || fail "Executable '$executable' is required"
    done
}

kube() {
    kubectl --context "$PROFILE" --namespace "$NAMESPACE" "$@"
}

helm_local() {
    HELM_REPOSITORY_CONFIG="$HELM_CACHE/repositories.yaml" \
        HELM_REPOSITORY_CACHE="$HELM_CACHE/repository-cache" helm "$@"
}

prepare_chart() {
    require helm
    mkdir -p "$HELM_CACHE/repository-cache"

    # Reuse CI's complete repository inventory and committed dependency locks.
    HELM_REPOSITORY_CONFIG="$HELM_CACHE/repositories.yaml" \
        HELM_REPOSITORY_CACHE="$HELM_CACHE/repository-cache" \
        bash "$PROJECT_ROOT/scripts/tooling/build-chart-dependencies.sh" charts/polyad >&2
}

build_image() {
    local image_id base_image
    base_image="$IMAGE_REPOSITORY:minikube-$PROFILE"

    # Content-derived tags trigger upgrades only when the actual image changes.
    docker buildx build --load --provenance=false --target production \
        --file "$PROJECT_ROOT/services/operator/Dockerfile" --tag "$base_image" "$PROJECT_ROOT"
    image_id="$(docker image inspect --format '{{.Id}}' "$base_image")"
    [[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]] || fail 'Docker returned an invalid image ID'
    IMAGE_TAG="local-${image_id#sha256:}"
    docker tag "$base_image" "$IMAGE_REPOSITORY:$IMAGE_TAG"

    # Load from the host daemon into every node; no registry or docker-env mutation.
    minikube --profile "$PROFILE" image load --daemon "$IMAGE_REPOSITORY:$IMAGE_TAG"
}

chart_options() {
    CHART_OPTIONS=(--namespace "$NAMESPACE" --values "$INTEGRATION_DIR/values.yaml")
    if [[ -n "$EXTRA_VALUES" ]]; then
        CHART_OPTIONS+=(--values "$EXTRA_VALUES")
    fi

    # The integration stays standalone even when an optional overlay sets HA fields.
    # Other feature/resource settings remain configurable through the normal chart.
    CHART_OPTIONS+=(
        --set ha=false --set worker.enabled=false --set architecture.mode=Dense
        --set architecture.autoscaling=false --set rootControlPlane.enabled=false
        --set operator.replicaCount=1 --set operator.autoscaling.enabled=false
        --set operator.autoscaling.connections.enabled=false
        --set dragonfly.enabled=true --set dragonfly.ha.enabled=false
        --set dragonfly.autoscaling.enabled=false --set dragonflyOperator.replicaCount=1
        --set keda.install=false
        --set-string "operator.image.repository=$IMAGE_REPOSITORY"
        --set-string "operator.image.tag=$IMAGE_TAG" --set operator.image.pullPolicy=Never
    )
}

enable() {
    require minikube docker helm kubectl
    minikube --profile "$PROFILE" status
    prepare_chart
    build_image
    chart_options

    # Helm does not upgrade its crds/ definitions. Own their local updates explicitly
    # without force-conflicts, so another manager's incompatible edits fail safely.
    kube apply --server-side --field-manager=polyad-minikube -f "$PROJECT_ROOT/charts/polyad-crds/crds"
    kube wait --for=condition=Established --timeout "$TIMEOUT" -f "$PROJECT_ROOT/charts/polyad-crds/crds"
    helm_local upgrade --install polyad "$CHART" --kube-context "$PROFILE" \
        --create-namespace --wait --timeout "$TIMEOUT" "${CHART_OPTIONS[@]}"
}

smoke_test() {
    require minikube kubectl
    minikube --profile "$PROFILE" status
    kube wait nodes --all --for=condition=Ready --timeout "$TIMEOUT"
    kube rollout status deployment/polyad-dragonfly-operator --timeout "$TIMEOUT"
    kube rollout status deployment/polyad-polyad --timeout "$TIMEOUT"
    kube wait pods -l app=polyad-queue --for=condition=Ready --timeout "$TIMEOUT"
    [[ "$(kube get deployment polyad-polyad -o jsonpath='{.spec.replicas}')" == 1 ]] || fail 'Expected one Polyad replica'
    [[ "$(kube get dragonfly polyad-queue -o jsonpath='{.spec.replicas}')" == 1 ]] || fail 'Expected one Dragonfly instance'
    kube exec deployment/polyad-polyad -c operator -- python -m polyad.operator.lifecycle.probes --ready

    # Server-generated names cannot overwrite a developer's Graph or Workload.
    # Retain failed runs for inspection; delete only this run's objects on success.
    local smoke_name
    smoke_name="$(kube create -f "$INTEGRATION_DIR/workload.yaml" -o jsonpath='{.metadata.name}')"
    [[ "$smoke_name" =~ ^polyad-minikube-smoke-[a-z0-9]+$ ]] || fail 'Kubernetes returned an unexpected smoke-test name'
    printf 'Smoke resources: %s/%s (retained if a check fails)\n' "$NAMESPACE" "$smoke_name"
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
    kube delete "graph/$smoke_name" --wait=true --timeout "$TIMEOUT"
    kube delete "workload/$smoke_name" --wait=true --timeout "$TIMEOUT"
    printf 'Polyad standalone smoke test passed.\n'
}

if [[ $# -ne 1 ]]; then
    usage >&2
    exit 2
fi
case "$1" in
    -h | --help | help)
        usage
        exit 0
        ;;
    start | enable | test | status | render | disable | stop | delete) ;;
    *)
        usage >&2
        exit 2
        ;;
esac

for name in "$PROFILE" "$NAMESPACE"; do
    [[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ && ${#name} -le 63 ]] || fail 'Profile and namespace must be DNS labels of at most 63 characters'
done
[[ "$NODES" =~ ^[1-9][0-9]*$ ]] || fail 'Node count must be a positive integer'
if [[ -n "$EXTRA_VALUES" && ("$1" == start || "$1" == enable || "$1" == render) ]]; then
    [[ -f "$EXTRA_VALUES" ]] || fail "Values file not found: $EXTRA_VALUES"
fi

case "$1" in
    start)
        require minikube docker helm kubectl
        minikube --profile "$PROFILE" start --driver "$DRIVER" --keep-context --ha=false \
            --kubernetes-version "$KUBERNETES_VERSION" --container-runtime containerd \
            --nodes "$NODES" --cpus "${POLYAD_MINIKUBE_CPUS:-2}" --memory "${POLYAD_MINIKUBE_MEMORY:-2048}" \
            --disk-size 30g --wait-timeout "$TIMEOUT"

        # The default hostpath provisioner is not multi-node aware. Local-path
        # storage pins each claim to its consumer's node; it is not replicated HA storage.
        minikube --profile "$PROFILE" addons disable storage-provisioner
        minikube --profile "$PROFILE" addons disable default-storageclass
        minikube --profile "$PROFILE" addons enable storage-provisioner-rancher
        minikube --profile "$PROFILE" addons enable metrics-server
        enable
        smoke_test
        ;;
    enable) enable ;;
    test) smoke_test ;;
    render)
        prepare_chart
        chart_options
        helm_local template polyad "$CHART" --kube-version "$KUBERNETES_VERSION" --include-crds "${CHART_OPTIONS[@]}"
        ;;
    status)
        require minikube kubectl
        minikube --profile "$PROFILE" status
        kube get nodes -o wide
        kube get deployments,statefulsets,pods,services,pvc,dragonflies,graphs,polygraphs,replicagroups
        ;;
    disable)
        require minikube helm kubectl
        minikube --profile "$PROFILE" status

        # Stop application graphs while their controller still exists. Never erase
        # arbitrary application resources or strand their drain finalizers on uninstall.
        boundaries="$(kube get graphs,polygraphs,replicagroups,compositions,rewrites -o name)"
        [[ -z "$boundaries" ]] || fail "Drain/delete application boundaries before disabling Polyad: $boundaries"
        helm_local uninstall polyad --kube-context "$PROFILE" --namespace "$NAMESPACE" \
            --ignore-not-found --wait --timeout "$TIMEOUT"
        ;;
    stop | delete)
        require minikube
        minikube --profile "$PROFILE" "$1"
        ;;
esac
