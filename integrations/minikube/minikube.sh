#!/usr/bin/env bash
# Run standalone Polyad on native Minikube VMs: QEMU/HVF on macOS, KVM2 on Linux.
set -euo pipefail

INTEGRATION_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$INTEGRATION_DIR/../.." && pwd)"
PROFILE="${POLYAD_MINIKUBE_PROFILE:-polyad}"
NAMESPACE="${POLYAD_MINIKUBE_NAMESPACE:-polyad}"
KVM_QEMU_URI="${POLYAD_MINIKUBE_KVM_QEMU_URI:-qemu:///system}"
KVM_NETWORK="${POLYAD_MINIKUBE_KVM_NETWORK:-default}"
REGISTRY_PORT="${POLYAD_MINIKUBE_REGISTRY_PORT:-5000}"
NODES="${POLYAD_MINIKUBE_NODES:-3}"
NATIVE_SSH="${POLYAD_MINIKUBE_NATIVE_SSH:-true}"
TIMEOUT="${POLYAD_MINIKUBE_TIMEOUT:-10m}"
EXTRA_VALUES="${POLYAD_MINIKUBE_VALUES:-}"
KUBERNETES_VERSION="v$(bash "$PROJECT_ROOT/scripts/tooling/tool-version.sh" kubectl)"
CHART="$PROJECT_ROOT/charts/polyad"
HELM_CACHE="$PROJECT_ROOT/.cache/minikube/helm"
IMAGE_REPOSITORY=localhost:5000/polyad
IMAGE_TAG=minikube
DRIVER=""
HOST_OS=""
IMAGE_PLATFORM=""
QEMU_BINARY=""
DRIVER_OPTIONS=()

##
# Print supported Minikube lifecycle commands.
# -> ret::exit_code
usage() {
    cat <<'EOF'
Usage: integrations/minikube/minikube.sh COMMAND

  start    Start three VMs, build/push Polyad, install the chart, and smoke-test it.
  recover  Stop/start this VM profile and restore missing workers without installing Polyad.
  enable   Rebuild/push Polyad, refresh CRDs, and upgrade the running local release.
  test     Check cluster health and execute a fresh, uniquely named Graph.
  status   Show nodes, controller/cache workloads, and graph boundaries.
  render   Build locked dependencies and print manifests without accessing a cluster.
  disable  Uninstall Polyad only after application boundaries have been drained.
  stop     Stop only this profile, preserving its workloads and storage.
  delete   Delete only this profile, including its local volumes.

macOS: QEMU/HVF with socket_vmnet networking; crane pushes the exported image.
Linux: KVM2/libvirt with virsh and a local Docker daemon.
Both: Minikube, Docker/Buildx, jq, curl, Helm, and kubectl.
Docker/Buildx builds application images only; Kubernetes nodes run in VMs.
The local registry push uses a temporary loopback-only port-forward (port 5000).
See integrations/minikube/README.md for prerequisites and environment settings.
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
# Verify all requested executables are available before modifying infrastructure.
# executables::string[] -> ret::exit_code
require() {
    local executable
    for executable in "$@"; do
        command -v "$executable" >/dev/null 2>&1 || fail "Executable '$executable' is required"
    done
}

##
# Select the native VM driver and matching Linux image architecture without fallback.
# -> ret::exit_code
select_vm_driver() {
    HOST_OS="$(uname -s)"
    case "$HOST_OS" in
        Darwin)
            DRIVER=qemu2

            # Shared networking lets the three separate VMs reach each other.
            DRIVER_OPTIONS=(--network socket_vmnet)
            ;;
        Linux)
            DRIVER=kvm2
            DRIVER_OPTIONS=(--kvm-qemu-uri "$KVM_QEMU_URI" --kvm-network "$KVM_NETWORK")
            ;;
        *) fail "Unsupported host '$HOST_OS'; use macOS (QEMU/HVF) or Linux (KVM2/libvirt)" ;;
    esac
    local requested="${POLYAD_MINIKUBE_DRIVER:-$DRIVER}"
    [[ "$requested" != qemu ]] || requested=qemu2
    [[ "$requested" == "$DRIVER" ]] || fail "$HOST_OS requires driver '$DRIVER', not '$requested'"

    case "$(uname -m)" in
        arm64 | aarch64)
            IMAGE_PLATFORM=linux/arm64
            QEMU_BINARY=qemu-system-aarch64
            ;;
        x86_64)
            IMAGE_PLATFORM=linux/amd64
            QEMU_BINARY=qemu-system-x86_64
            ;;
        *) fail 'Unsupported host architecture; use native ARM64 or x86_64 tools' ;;
    esac
}

##
# Check native virtualization capabilities without installing or reconfiguring host tools.
# -> ret::exit_code
check_vm_host() {
    if [[ "$DRIVER" == qemu2 ]]; then
        require "$QEMU_BINARY" qemu-img
        local accelerators
        accelerators="$("$QEMU_BINARY" -accel help)"
        grep -Eq '^[[:space:]]*hvf[[:space:]]*$' <<<"$accelerators" ||
            fail 'QEMU must support HVF; install native QEMU for this Mac (not a Rosetta build)'

        # Minikube checks socket_vmnet's installed client and running service.
        # Its native QEMU driver owns these VMs; macOS does not use virsh here.
        if [[ -n "${TMUX:-}" ]]; then
            # A detached tmux server can retain a different macOS network privacy
            # context from its terminal, even when iTerm's permission is enabled.
            printf '%s\n' 'WARNING: Running inside tmux on macOS. If SSH reports "no route to host", retry from a fresh Terminal.app shell outside tmux; do not delete the VM or kill your existing tmux server.' >&2
        fi
    else
        require virsh

        # Preflight and Minikube must use the same connection, independent of virsh defaults.
        virsh --connect "$KVM_QEMU_URI" domcapabilities --virttype kvm >/dev/null ||
            fail "KVM is unavailable through $KVM_QEMU_URI; check virtualization, libvirt, and user permissions"
        virsh --connect "$KVM_QEMU_URI" net-info "$KVM_NETWORK" >/dev/null ||
            fail "Libvirt network '$KVM_NETWORK' is unavailable through $KVM_QEMU_URI"
    fi
}

##
# Refuse deployment into a mismatched VM profile without changing or deleting it.
# -> ret::exit_code
check_vm_profile() {
    require minikube jq
    local profiles
    profiles="$(minikube --profile "$PROFILE" profile list --light --output=json)"
    jq -e --arg profile "$PROFILE" --arg driver "$DRIVER" \
        'any(.valid[]?; .Name == $profile and .Config.Driver == $driver
            and ($driver != "qemu2" or .Config.Network == "socket_vmnet"))' <<<"$profiles" >/dev/null ||
        fail "Profile '$PROFILE' does not match driver '$DRIVER' and its required network; choose a fresh POLYAD_MINIKUBE_PROFILE and run start. Existing profiles are never converted or deleted automatically"
}

##
# Count this profile's configured nodes only when it has one control plane.
# -> ret::integer
vm_node_count() {
    local profiles
    profiles="$(minikube --profile "$PROFILE" profile list --light --output=json)"
    jq -er --arg profile "$PROFILE" \
        '.valid[]? | select(.Name == $profile) | .Config.Nodes | arrays
            | select(length > 0 and ([.[] | select(.ControlPlane == true)] | length) == 1)
            | length' <<<"$profiles" ||
        fail "Profile '$PROFILE' must contain nodes and exactly one control plane; recovery does not convert HA clusters"
}

##
# Complete an interrupted multi-node startup without removing any existing nodes.
# -> ret::exit_code
ensure_vm_nodes() {
    local count previous
    count="$(vm_node_count)"
    ((count <= NODES)) || fail "Profile '$PROFILE' already has $count nodes, more than requested $NODES; no nodes were removed"
    while ((count < NODES)); do
        previous="$count"

        # Minikube ignores --nodes for existing profiles. Its node-add path also
        # auto-sizes memory when growing from one node unless this value is set.
        # The environment override prevents that resizing; the saved profile
        # still supplies the actual memory and CPU allocations for new workers.
        MINIKUBE_MEMORY="${POLYAD_MINIKUBE_MEMORY:-4096}" MINIKUBE_NATIVE_SSH="$NATIVE_SSH" \
            minikube --profile "$PROFILE" node add --worker --control-plane=false
        count="$(vm_node_count)"
        ((count == previous + 1)) || fail "Expected one new worker in '$PROFILE'; inspect the profile before retrying"
    done

    # A successful VM start is not sufficient: networking and kubelet must also
    # report readiness before storage addons or application installation begins.
    kube wait nodes --all --for=condition=Ready --timeout "$TIMEOUT"
}

##
# Start the selected VM profile and restore any missing non-HA worker nodes.
# -> ret::exit_code
start_vms() {
    if [[ "$NATIVE_SSH" == false ]]; then require ssh; fi
    local profiles existing_nodes
    local profile_options=(--keep-context)
    profiles="$(minikube --profile "$PROFILE" profile list --light --output=json)"
    jq -e '(.valid | type) == "array"' <<<"$profiles" >/dev/null || fail 'Minikube returned an invalid profile inventory'

    # --nodes and --ha are creation-only flags. Passing them on a restart emits
    # misleading topology-change warnings even when their values are unchanged.
    if jq -e --arg profile "$PROFILE" 'any(.valid[]; .Name == $profile)' <<<"$profiles" >/dev/null; then
        check_vm_profile
        existing_nodes="$(vm_node_count)"
        ((existing_nodes <= NODES)) || fail "Profile '$PROFILE' has more nodes than requested; no VMs were started or removed"
    else
        profile_options+=(--nodes "$NODES" --ha=false)
    fi

    # Never delete a profile to resolve driver or topology mismatches. Keep the
    # caller's active kube context; every subsequent Kubernetes call is scoped.
    minikube --profile "$PROFILE" start --driver "$DRIVER" "${profile_options[@]}" \
        "${DRIVER_OPTIONS[@]}" --native-ssh="$NATIVE_SSH" \
        --insecure-registry localhost:5000 \
        --kubernetes-version "$KUBERNETES_VERSION" --container-runtime containerd \
        --cpus "${POLYAD_MINIKUBE_CPUS:-2}" --memory "${POLYAD_MINIKUBE_MEMORY:-4096}" \
        --disk-size 30g --wait-timeout "$TIMEOUT"
    check_vm_profile
    ensure_vm_nodes
}

##
# Invoke kubectl using the selected Minikube context and Polyad namespace.
# args::string[] -> ret::exit_code
kube() {
    kubectl --context "$PROFILE" --namespace "$NAMESPACE" "$@"
}

##
# Invoke Helm with repository metadata isolated from the caller's configuration.
# args::string[] -> ret::exit_code
helm_local() {
    HELM_REPOSITORY_CONFIG="$HELM_CACHE/repositories.yaml" \
        HELM_REPOSITORY_CACHE="$HELM_CACHE/repository-cache" helm "$@"
}

##
# Register dependency repositories and build the locked Polyad chart dependencies.
# -> ret::exit_code
prepare_chart() {
    require helm
    mkdir -p "$HELM_CACHE/repository-cache"

    # Reuse CI's complete repository inventory and committed dependency locks.
    HELM_REPOSITORY_CONFIG="$HELM_CACHE/repositories.yaml" \
        HELM_REPOSITORY_CACHE="$HELM_CACHE/repository-cache" \
        bash "$PROJECT_ROOT/scripts/tooling/build-chart-dependencies.sh" charts/polyad >&2
}

##
# Build the production image and derive its immutable local registry tag.
# -> ret::exit_code
build_image() {
    local image_id base_image
    base_image="polyad:minikube-$PROFILE"

    # Content-derived tags trigger upgrades only when the actual image changes.
    docker buildx build --load --provenance=false --platform "$IMAGE_PLATFORM" --target production \
        --file "$PROJECT_ROOT/services/operator/Dockerfile" --tag "$base_image" "$PROJECT_ROOT"
    image_id="$(docker image inspect --format '{{.Id}}' "$base_image")"
    [[ "$image_id" =~ ^sha256:[a-f0-9]{64}$ ]] || fail 'Docker returned an invalid image ID'
    IMAGE_TAG="local-${image_id#sha256:}"
    docker tag "$base_image" "127.0.0.1:$REGISTRY_PORT/polyad:$IMAGE_TAG"
}

##
# Stop only this invocation's port-forward and remove its private temporary files.
# pid::integer log::path archive::path -> ret::exit_code
cleanup_registry_forward() {
    local pid="$1" log="$2" archive="$3"
    if [[ -n "$pid" ]]; then
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    fi
    rm -f "$log"
    if [[ -n "$archive" ]]; then
        rm -f "$archive"
    fi
}

##
# Push the built image through a temporary loopback-only forward to the cluster registry.
# -> ret::exit_code
publish_image() {
    minikube --profile "$PROFILE" addons enable registry
    kubectl --context "$PROFILE" --namespace kube-system rollout status deployment/registry --timeout "$TIMEOUT"
    kubectl --context "$PROFILE" --namespace kube-system rollout status daemonset/registry-proxy --timeout "$TIMEOUT"

    # A subshell scopes the traps, including cleanup on push failure or interruption.
    (
        forward_log="$(mktemp)"
        forward_pid=""
        image_archive=""
        trap 'cleanup_registry_forward "$forward_pid" "$forward_log" "$image_archive"' EXIT
        trap 'exit 130' INT
        trap 'exit 143' TERM

        # Docker Desktop's daemon has a different localhost from this Mac. Export
        # once, then push with a host-native client that can reach our port-forward.
        if [[ "$HOST_OS" == Darwin ]]; then
            image_archive="$(mktemp)"
            docker image save --output "$image_archive" "127.0.0.1:$REGISTRY_PORT/polyad:$IMAGE_TAG"
        fi
        kubectl --context "$PROFILE" --namespace kube-system port-forward --address 127.0.0.1 \
            service/registry "$REGISTRY_PORT:80" >"$forward_log" 2>&1 &
        forward_pid=$!
        deadline=$((SECONDS + 60))

        # Wait for our own process to bind before probing. If the port is already in
        # use, fail instead of pushing the image to an unrelated localhost registry.
        until grep -Fq "Forwarding from 127.0.0.1:$REGISTRY_PORT ->" "$forward_log"; do
            if ! kill -0 "$forward_pid" 2>/dev/null || ((SECONDS >= deadline)); then
                sed -n '1,80p' "$forward_log" >&2
                fail "Registry port-forward failed; choose an unused POLYAD_MINIKUBE_REGISTRY_PORT (currently $REGISTRY_PORT)"
            fi
            sleep 0.1
        done
        kill -0 "$forward_pid" 2>/dev/null || fail 'Registry port-forward exited before the image push'
        curl --fail --silent --show-error --noproxy '*' --connect-timeout 5 --max-time 10 \
            "http://127.0.0.1:$REGISTRY_PORT/v2/" >/dev/null
        if [[ "$HOST_OS" == Darwin ]]; then
            crane push --insecure "$image_archive" "127.0.0.1:$REGISTRY_PORT/polyad:$IMAGE_TAG"
        else
            docker push "127.0.0.1:$REGISTRY_PORT/polyad:$IMAGE_TAG"
        fi
    )
}

##
# Populate CHART_OPTIONS with image settings and enforced standalone configuration.
# -> ret::exit_code
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
        --set-string "operator.image.tag=$IMAGE_TAG" --set operator.image.pullPolicy=IfNotPresent
    )
}

##
# Rebuild and publish Polyad to the in-cluster registry before installing its standalone release.
# -> ret::exit_code
enable() {
    require minikube docker helm kubectl curl
    if [[ "$HOST_OS" == Darwin ]]; then require crane; fi
    check_vm_profile
    minikube --profile "$PROFILE" status
    prepare_chart
    build_image
    publish_image
    chart_options

    # Helm does not upgrade its crds/ definitions. Own their local updates explicitly
    # without force-conflicts, so another manager's incompatible edits fail safely.
    # Skip Helm's CRD installation after this step to keep one field manager.
    kube apply --server-side --field-manager=polyad-minikube -f "$PROJECT_ROOT/charts/polyad-crds/crds"
    kube wait --for=condition=Established --timeout "$TIMEOUT" -f "$PROJECT_ROOT/charts/polyad-crds/crds"
    helm_local upgrade --install polyad "$CHART" --kube-context "$PROFILE" \
        --skip-crds --create-namespace --wait --timeout "$TIMEOUT" "${CHART_OPTIONS[@]}"
}

##
# Verify readiness and execute a unique Graph, retaining failed smoke resources.
# -> ret::exit_code
smoke_test() {
    require minikube kubectl
    check_vm_profile
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
    start | recover | enable | test | status | render | disable | stop | delete) ;;
    *)
        usage >&2
        exit 2
        ;;
esac

for name in "$PROFILE" "$NAMESPACE"; do
    [[ "$name" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ && ${#name} -le 63 ]] || fail 'Profile and namespace must be DNS labels of at most 63 characters'
done
[[ "$NODES" =~ ^[1-9][0-9]*$ ]] || fail 'Node count must be a positive integer'
[[ "$NATIVE_SSH" == true || "$NATIVE_SSH" == false ]] || fail 'POLYAD_MINIKUBE_NATIVE_SSH must be true or false'
[[ "$REGISTRY_PORT" =~ ^[1-9][0-9]{3,4}$ ]] ||
    fail 'Registry port must be an integer between 1024 and 65535'
((REGISTRY_PORT >= 1024 && REGISTRY_PORT <= 65535)) ||
    fail 'Registry port must be an integer between 1024 and 65535'
if [[ -n "$EXTRA_VALUES" && ("$1" == start || "$1" == enable || "$1" == render) ]]; then
    [[ -f "$EXTRA_VALUES" ]] || fail "Values file not found: $EXTRA_VALUES"
fi
if [[ "$1" == start || "$1" == recover || "$1" == enable || "$1" == test ]]; then
    case "${POLYAD_MINIKUBE_DRIVER:-}" in
        '' | kvm2 | qemu | qemu2) ;;
        *) fail 'This integration uses only native VMs; unset POLYAD_MINIKUBE_DRIVER for automatic selection' ;;
    esac
    select_vm_driver
fi

case "$1" in
    start)
        require minikube docker helm kubectl jq curl
        if [[ "$HOST_OS" == Darwin ]]; then require crane; fi
        check_vm_host

        start_vms

        # The default hostpath provisioner is not multi-node aware. Local-path
        # storage pins each claim to its consumer's node; it is not replicated HA storage.
        minikube --profile "$PROFILE" addons disable storage-provisioner
        minikube --profile "$PROFILE" addons disable default-storageclass
        minikube --profile "$PROFILE" addons enable storage-provisioner-rancher
        minikube --profile "$PROFILE" addons enable metrics-server
        enable
        smoke_test
        ;;
    recover)
        require minikube kubectl jq
        if [[ "$NATIVE_SSH" == false ]]; then require ssh; fi
        check_vm_profile
        check_vm_host

        # QEMU only rediscovers its DHCP lease on VM start. A failed SSH wait
        # can leave a running guest with no saved IP, so explicitly stop/start
        # the selected profile while retaining disks, keys, and Kubernetes data.
        existing_nodes="$(vm_node_count)"
        ((existing_nodes <= NODES)) || fail "Profile '$PROFILE' has more nodes than requested; no VMs were stopped or removed"
        minikube --profile "$PROFILE" stop --keep-context-active
        start_vms
        minikube --profile "$PROFILE" status
        kube get nodes -o wide
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
