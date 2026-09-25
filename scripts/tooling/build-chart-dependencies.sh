#!/usr/bin/env bash
# Register every HTTP chart source before rebuilding the committed dependency locks.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
if [[ $# == 0 ]]; then set -- charts/polyad charts/polyad-benchmarks; fi

# Even disabled dependencies must be resolvable on a fresh Helm installation.
# OCI and file:// dependencies are fetched directly, not registered as repositories.
# test_ci_dependencies.py checks this inventory against every Chart.yaml and lock.
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm repo add kedacore https://kedacore.github.io/charts --force-update
helm repo add stakater https://stakater.github.io/stakater-charts --force-update
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo add grafana-community https://grafana-community.github.io/helm-charts --force-update
helm repo add open-telemetry https://open-telemetry.github.io/opentelemetry-helm-charts --force-update

for chart in "$@"; do
    # Never resolve new versions during CI or release packaging. Repository adds
    # above already refresh their indexes, so avoid a second network refresh.
    if [[ ! -f "$chart/Chart.lock" ]]; then
        echo "Missing committed dependency lock: $chart/Chart.lock" >&2
        exit 2
    fi
    helm dependency build "$chart" --skip-refresh
done
