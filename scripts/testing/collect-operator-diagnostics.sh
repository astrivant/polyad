#!/usr/bin/env bash
# Collect whatever exists after a partial installation without losing later logs.
set -euo pipefail
namespace="${POLYAD_TEST_NAMESPACE:-polyad}"
diagnostics_failed=0
kubectl_command=(kubectl --request-timeout=20s)

##
# Run one diagnostic command in a log group and record failure without stopping later checks.
# command::string args::string[] -> ret::exit_code
collect() {
    printf '::group::%s\n' "$*"
    if ! "$@"; then
        # Keep the original error visible, but continue to the remaining evidence.
        printf '::warning::Diagnostic command failed: %s\n' "$*" >&2
        diagnostics_failed=1
    fi
    printf '::endgroup::\n'
}

# Built-in APIs remain useful even when Helm never installed the custom resources.
for resource in pods deployments.apps statefulsets.apps jobs.batch leases.coordination.k8s.io events; do
    collect "${kubectl_command[@]}" -n "$namespace" get "$resource" -o yaml
done

# Discovery distinguishes an absent CRD from a failed read of an installed API.
# If discovery itself fails, try each API anyway and still collect operator logs.
discovery_ok=true
if ! available=$("${kubectl_command[@]}" api-resources --namespaced=true --verbs=list -o name); then
    printf '::warning::API discovery failed; attempting custom-resource diagnostics individually.\n' >&2
    discovery_ok=false
    diagnostics_failed=1
fi
for resource in \
    dragonflies.dragonflydb.io \
    dragonflypools.polyad.astrivant.com \
    graphs.polyad.astrivant.com \
    polygraphs.polyad.astrivant.com \
    replicagroups.polyad.astrivant.com \
    scaledobjects.keda.sh; do
    if [[ "$discovery_ok" == true && $'\n'"$available"$'\n' != *$'\n'"$resource"$'\n'* ]]; then
        printf 'Skipping diagnostics for %s: API not installed.\n' "$resource"
        continue
    fi
    collect "${kubectl_command[@]}" -n "$namespace" get "$resource" -o yaml
done

# Never let a missing CRD, RBAC denial, or failed resource read suppress the logs.
collect "${kubectl_command[@]}" -n "$namespace" logs deployment/polyad-polyad --all-containers=true --tail=200

# The failure-only workflow step is best effort. Nonzero still identifies genuine
# collection failures to local callers without changing the original CI failure.
exit "$diagnostics_failed"
