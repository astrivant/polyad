#!/usr/bin/env bash
# Delegate to the checksum-pinned, independently maintained Minikube addon.
set -euo pipefail

ADDON_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "$ADDON_DIR/../../.." && pwd)"
PROFILE="${POLYAD_MINIKUBE_PROFILE:-polyad}"

##
# Report a dependency or ownership failure without modifying the cluster.
# message::string[] -> ret::never
fail() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

##
# Describe the upstream command interface and Polyad's local defaults.
# -> ret::exit_code
usage() {
    cat <<'EOF'
Usage: integrations/minikube/autoscaler/autoscaler.sh COMMAND

  fetch    Download and verify the pinned published source; print its directory.
  path     Print the pinned source directory without downloading anything.
  build|init|bridge|enable|test|status|render|disable|resume
           Delegate to the standalone addon's scripts/addon.sh.

POLYAD_MINIKUBE_PROFILE defaults to polyad.
POLYAD_MINIKUBE_AUTOSCALER_CONFIG selects an edited config.example.json for init.
MINIKUBE_AUTOSCALER_STATE_DIR and other upstream settings are passed through.
The provider, container, chart and VM lifecycle are maintained upstream:
https://github.com/astrivant/minikube-cluster-autoscaler-addon
See integrations/minikube/autoscaler/README.md before migrating an older lab.
EOF
}

##
# Verify a published archive before extracting or executing any of its code.
# archive::path -> ret::exit_code
verify_archive() {
    local actual
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$1")"
    else
        actual="$(shasum -a 256 "$1")"
    fi
    [[ "${actual%% *}" == "$AUTOSCALER_SHA256" ]] || fail 'Published addon checksum mismatch; nothing was executed'
}

##
# Cache one immutable upstream source tree without storing cluster state in it.
# -> ret::exit_code
prepare_source() {
    if [[ -f "$AUTOSCALER_SOURCE/scripts/addon.sh" ]]; then return; fi
    [[ ! -e "$AUTOSCALER_SOURCE" ]] || fail "Incomplete addon cache at $AUTOSCALER_SOURCE; inspect it before retrying"
    mkdir -p "$AUTOSCALER_CACHE"
    local download
    download="$(mktemp -d "$AUTOSCALER_CACHE/.fetch.XXXXXX")"

    # Keep failed downloads for inspection. Only verified source is made active.
    curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
        --connect-timeout 15 --max-time 300 --retry 2 \
        "https://codeload.github.com/astrivant/minikube-cluster-autoscaler-addon/tar.gz/$AUTOSCALER_REVISION" \
        --output "$download/source.tar.gz"
    verify_archive "$download/source.tar.gz"
    tar -xzf "$download/source.tar.gz" -C "$download"
    [[ -f "$download/minikube-cluster-autoscaler-addon-$AUTOSCALER_REVISION/scripts/addon.sh" ]] || fail 'Archive has no addon entry point'
    mv "$download/minikube-cluster-autoscaler-addon-$AUTOSCALER_REVISION" "$AUTOSCALER_SOURCE"
    rm -f "$download/source.tar.gz"
    rmdir "$download"
}

[[ $# -eq 1 ]] || {
    usage >&2
    exit 2
}
case "$1" in
    help | -h | --help)
        usage
        exit 0
        ;;
    fetch | path | build | init | bridge | enable | test | status | render | disable | resume) ;;
    *)
        usage >&2
        exit 2
        ;;
esac
[[ "$PROFILE" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?$ && ${#PROFILE} -le 40 ]] || fail 'Invalid Polyad Minikube profile'
[[ -z "${MINIKUBE_AUTOSCALER_PROFILE:-}" || "$MINIKUBE_AUTOSCALER_PROFILE" == "$PROFILE" ]] || fail 'Upstream and Polyad profiles differ'

# Shared paths keep the parent VM lifecycle guard aware of the upstream owner.
# shellcheck source=integrations/minikube/autoscaler/paths.sh
source "$ADDON_DIR/paths.sh"
case "$1" in
    fetch | path | build | render) ;;
    *)
        [[ ! -f "$AUTOSCALER_LEGACY_STATE/provider/state.json" ]] || fail 'Legacy autoscaler ownership exists; follow the README migration steps before using the standalone addon'
        ;;
esac
if [[ "$1" == path ]]; then
    printf '%s\n' "$AUTOSCALER_SOURCE"
    exit 0
fi

# The addon owns all implementation and lifecycle commands. Only translate the
# existing Polyad configuration option and select this lab's cluster identity.
if [[ -n "${POLYAD_MINIKUBE_AUTOSCALER_CONFIG:-}" ]]; then
    [[ -z "${MINIKUBE_AUTOSCALER_CONFIG:-}" || "$MINIKUBE_AUTOSCALER_CONFIG" == "$POLYAD_MINIKUBE_AUTOSCALER_CONFIG" ]] || fail 'Upstream and Polyad configuration paths differ'
    export MINIKUBE_AUTOSCALER_CONFIG="$POLYAD_MINIKUBE_AUTOSCALER_CONFIG"
fi
prepare_source
if [[ "$1" == fetch ]]; then
    printf '%s\n' "$AUTOSCALER_SOURCE"
    exit 0
fi
export MINIKUBE_AUTOSCALER_PROFILE="$PROFILE"
export MINIKUBE_AUTOSCALER_STATE_DIR="$AUTOSCALER_STATE"
export MINIKUBE_AUTOSCALER_BINARY="$AUTOSCALER_BINARY"
exec bash "$AUTOSCALER_SOURCE/scripts/addon.sh" "$1"
