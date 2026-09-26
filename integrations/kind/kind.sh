#!/usr/bin/env bash
# Run the canonical chart in a three-node Kind cluster with one operator and cache.
set -euo pipefail

INTEGRATION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$INTEGRATION_DIR/../.." && pwd)"
CLUSTER="${POLYAD_KIND_CLUSTER:-polyad}"
NAMESPACE="${POLYAD_KIND_NAMESPACE:-polyad}"
TIMEOUT="${POLYAD_KIND_TIMEOUT:-10m}"
CONFIG="${POLYAD_KIND_CONFIG:-$INTEGRATION_DIR/cluster.yaml}"
EXTRA_VALUES="${POLYAD_KIND_VALUES:-}"
KUBERNETES_VERSION="v$(bash "$PROJECT_ROOT/scripts/tooling/tool-version.sh" kubectl)"
NODE_IMAGE="${POLYAD_KIND_NODE_IMAGE:-kindest/node:$KUBERNETES_VERSION}"
CONTEXT="kind-$CLUSTER"
KUBECONFIG_FILE="$PROJECT_ROOT/.cache/kind/$CLUSTER/kubeconfig"
HELM_CACHE="$PROJECT_ROOT/.cache/kind/helm"
CHART="$PROJECT_ROOT/charts/polyad"
IMAGE_REPOSITORY=polyad
IMAGE_TAG=kind

# Building and loading must use the same provider, regardless of host auto-detection.
export KIND_EXPERIMENTAL_PROVIDER=docker

##
# Print command usage and supported environment settings.
# -> ret::exit_code
usage() {
    cat <<'EOF'
Usage: integrations/kind/kind.sh COMMAND

  start    Create/reuse three nodes, build/load Polyad, install, and smoke-test it.
  enable   Rebuild/load Polyad and upgrade its standalone release in an existing cluster.
  test     Check readiness and execute a fresh, uniquely named Graph.
  status   Show cluster nodes, controller/cache workloads, and graph boundaries.
  render   Build locked chart dependencies and print manifests without a cluster.
  disable  Uninstall Polyad after application boundaries have been drained.
  delete   Destroy only the selected Kind cluster, including its node-local storage.

Requires Docker with Buildx, Kind, Helm, and kubectl. Run as your normal user.
Uses a private .cache/kind/CLUSTER/kubeconfig; your current context is unchanged.
Use a dedicated cluster: CRDs and the Dragonfly controller are cluster-scoped.
Kind has no stop command; delete is destructive, disable only removes the release.

Optional environment settings:
  POLYAD_KIND_CLUSTER      Cluster name (default: polyad; context: kind-polyad).
  POLYAD_KIND_NAMESPACE    Release namespace (default: polyad).
  POLYAD_KIND_TIMEOUT      Timeout per wait (default: 10m).
  POLYAD_KIND_CONFIG       Creation config (default: integrations/kind/cluster.yaml).
  POLYAD_KIND_NODE_IMAGE   Node image (default: kindest/node:v<KUBECTL_PIN>).
  POLYAD_KIND_VALUES       Additional values file; standalone settings remain enforced.
EOF
}

##
# Print an error and terminate the script with status one.
# message::string[] -> ret::never
fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

##
# Verify every named executable is available before modifying infrastructure.
# executables::string[] -> ret::exit_code
require() {
    local executable
    for executable in "$@"; do
        command -v "$executable" >/dev/null 2>&1 || fail "Executable '$executable' is required"
    done
}

##
# Invoke kubectl with this cluster's private credentials and namespace.
# args::string[] -> ret::exit_code
kube() {
    kubectl --kubeconfig "$KUBECONFIG_FILE" --context "$CONTEXT" --namespace "$NAMESPACE" "$@"
}

##
# Invoke Helm with repository metadata isolated from the caller's configuration.
# args::string[] -> ret::exit_code
helm_local() {
    HELM_REPOSITORY_CONFIG="$HELM_CACHE/repositories.yaml" \
        HELM_REPOSITORY_CACHE="$HELM_CACHE/repository-cache" helm "$@"
}

##
# Export private credentials for the selected existing cluster and check access.
# -> ret::exit_code
connect_cluster() {
    local nodes
    require kind kubectl
    nodes="$(kind get nodes --name "$CLUSTER")"
    [[ -n "$nodes" ]] || fail "Kind cluster '$CLUSTER' is missing; run start first"

    # Export only to our private file, even if the caller has a shared KUBECONFIG.
    # Refreshing it also handles a cluster recreated outside this helper.
    (
        umask 077
        mkdir -p "$(dirname "$KUBECONFIG_FILE")"
        kind export kubeconfig --name "$CLUSTER" --kubeconfig "$KUBECONFIG_FILE"
    )
    chmod 600 "$KUBECONFIG_FILE"
    kube cluster-info
}

##
# Register dependency repositories and build the locked Polyad chart dependencies.
# -> ret::exit_code
prepare_chart() {
    require helm
    mkdir -p "$HELM_CACHE/repository-cache"

    # Register disabled dependency repositories too, before consuming the lockfile.
    HELM_REPOSITORY_CONFIG="$HELM_CACHE/repositories.yaml" \
        HELM_REPOSITORY_CACHE="$HELM_CACHE/repository-cache" \
        bash "$PROJECT_ROOT/scripts/tooling/build-chart-dependencies.sh" charts/polyad >&2
}

##
# Build the production image and load its content-addressed tag into all Kind nodes.
# -> ret::exit_code
build_image() {
    local image_id base_image
    base_image="$IMAGE_REPOSITORY:kind-$CLUSTER"
    docker buildx build --load --provenance=false --target production \
        --file "$PROJECT_ROOT/services/operator/Dockerfile" --tag "$base_image" "$PROJECT_ROOT"
    image_id="$(docker image inspect --format '{{.Id}}' "$base_image")"
    [[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]] || fail 'Docker returned an invalid image ID'

    # The content tag changes the Deployment only when the built image changes.
    IMAGE_TAG="local-${image_id#sha256:}"
    docker tag "$base_image" "$IMAGE_REPOSITORY:$IMAGE_TAG"
    kind load docker-image "$IMAGE_REPOSITORY:$IMAGE_TAG" --name "$CLUSTER"
}

##
# Populate CHART_OPTIONS with user overlays followed by enforced standalone settings.
# -> ret::exit_code
chart_options() {
    CHART_OPTIONS=(--namespace "$NAMESPACE" --values "$INTEGRATION_DIR/values.yaml")
    if [[ -n "$EXTRA_VALUES" ]]; then
        CHART_OPTIONS+=(--values "$EXTRA_VALUES")
    fi

    # User overlays may tune ordinary features, but cannot turn this profile into HA.
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

##
# Rebuild Polyad and install its CRDs and standalone Helm release in the selected cluster.
# -> ret::exit_code
enable() {
    require docker helm
    connect_cluster
    prepare_chart
    build_image
    chart_options

    # Helm does not upgrade crds/. Fail on conflicting ownership instead of forcing it.
    kube apply --server-side --field-manager=polyad-kind -f "$PROJECT_ROOT/charts/polyad-crds/crds"
    kube wait --for=condition=Established --timeout "$TIMEOUT" -f "$PROJECT_ROOT/charts/polyad-crds/crds"
    helm_local upgrade --install polyad "$CHART" --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CONTEXT" \
        --create-namespace --wait --timeout "$TIMEOUT" "${CHART_OPTIONS[@]}"
}

##
# Check controller readiness and execute a unique Graph, preserving failed resources.
# -> ret::exit_code
smoke_test() {
    connect_cluster
    kube wait nodes --all --for=condition=Ready --timeout "$TIMEOUT"
    kube rollout status deployment/polyad-dragonfly-operator --timeout "$TIMEOUT"
    kube rollout status deployment/polyad-polyad --timeout "$TIMEOUT"
    kube wait pods -l app=polyad-queue --for=condition=Ready --timeout "$TIMEOUT"
    [[ "$(kube get deployment polyad-polyad -o jsonpath='{.spec.replicas}')" == 1 ]] || fail 'Expected one Polyad replica'
    [[ "$(kube get dragonfly polyad-queue -o jsonpath='{.spec.replicas}')" == 1 ]] || fail 'Expected one Dragonfly instance'
    kube exec deployment/polyad-polyad -c operator -- python -m polyad.operator.lifecycle.probes --ready

    # Unique names avoid replacing application objects. Failed runs remain inspectable.
    local smoke_name
    smoke_name="$(kube create -f "$INTEGRATION_DIR/workload.yaml" -o jsonpath='{.metadata.name}')"
    [[ "$smoke_name" =~ ^polyad-kind-smoke-[a-z0-9]+$ ]] || fail 'Kubernetes returned an unexpected smoke-test name'
    printf 'Smoke resources: %s/%s (retained if a check fails)\n' "$NAMESPACE" "$smoke_name"
    kube create -f - <<EOF
apiVersion: polyad.astrivant.com/v1alpha1
kind: Graph
metadata:
  name: $smoke_name
  labels:
    app.kubernetes.io/part-of: polyad-kind-smoke
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
    printf 'Polyad standalone Kind smoke test passed.\n'
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
    start | enable | test | status | render | disable | delete) ;;
    *)
        usage >&2
        exit 2
        ;;
esac

for name in "$CLUSTER" "$NAMESPACE"; do
    [[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ && ${#name} -le 63 ]] || fail 'Cluster and namespace must be DNS labels of at most 63 characters'
done
if [[ "$1" == start ]]; then
    [[ -f "$CONFIG" ]] || fail "Kind configuration not found: $CONFIG"
fi
if [[ -n "$EXTRA_VALUES" && ("$1" == start || "$1" == enable || "$1" == render) ]]; then
    [[ -f "$EXTRA_VALUES" ]] || fail "Values file not found: $EXTRA_VALUES"
fi

case "$1" in
    start)
        require kind docker helm kubectl
        clusters="$(kind get clusters)"
        if ! printf '%s\n' "$clusters" | grep -Fxq "$CLUSTER"; then
            # Kind normally changes the caller's kubeconfig; scope creation as well as use.
            (
                umask 077
                mkdir -p "$(dirname "$KUBECONFIG_FILE")"
                kind create cluster --name "$CLUSTER" \
                    --config "$CONFIG" --image "$NODE_IMAGE" --wait "$TIMEOUT" --kubeconfig "$KUBECONFIG_FILE"
            )
        fi
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
        connect_cluster
        kube get nodes -o wide
        kube get deployments,statefulsets,pods,services,pvc,dragonflies,graphs,polygraphs,replicagroups
        ;;
    disable)
        require helm
        connect_cluster

        # Keep the operator alive until application boundaries have completed draining.
        boundaries="$(kube get graphs,polygraphs,replicagroups,compositions,rewrites -o name)"
        [[ -z "$boundaries" ]] || fail "Drain/delete application boundaries before disabling Polyad: $boundaries"
        helm_local uninstall polyad --kubeconfig "$KUBECONFIG_FILE" --kube-context "$CONTEXT" --namespace "$NAMESPACE" \
            --ignore-not-found --wait --timeout "$TIMEOUT"
        ;;
    delete)
        require kind
        kind delete cluster --name "$CLUSTER" --kubeconfig "$KUBECONFIG_FILE"
        ;;
esac
