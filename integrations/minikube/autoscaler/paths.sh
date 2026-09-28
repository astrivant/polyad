#!/usr/bin/env bash
# Source from Polyad's launchers after setting PROJECT_ROOT and PROFILE.
# Keep upstream state outside the source cache so pin updates do not lose ownership.
# These variables are consumed by each sourcing launcher, not by this file itself.
# shellcheck disable=SC2034
AUTOSCALER_LOCK="$PROJECT_ROOT/integrations/minikube/autoscaler/source.lock.json"
AUTOSCALER_REVISION="$(jq -er '.revision | select(test("^[0-9a-f]{40}$"))' "$AUTOSCALER_LOCK")"
AUTOSCALER_SHA256="$(jq -er '.sha256 | select(test("^[0-9a-f]{64}$"))' "$AUTOSCALER_LOCK")"
AUTOSCALER_CACHE="$PROJECT_ROOT/.cache/minikube/addons/minikube-cluster-autoscaler-addon"
AUTOSCALER_SOURCE="$AUTOSCALER_CACHE/$AUTOSCALER_REVISION"
AUTOSCALER_BINARY="${MINIKUBE_AUTOSCALER_BINARY:-$AUTOSCALER_SOURCE/bin/minikube-cluster-autoscaler-addon}"
AUTOSCALER_STATE="${MINIKUBE_AUTOSCALER_STATE_DIR:-${XDG_STATE_HOME:-$HOME/.local/state}/minikube-cluster-autoscaler-addon/$PROFILE}"
[[ "$AUTOSCALER_STATE" == /* && "$AUTOSCALER_BINARY" == /* ]] || {
    printf 'Autoscaler state and binary paths must be absolute\n' >&2
    exit 2
}
