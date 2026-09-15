#!/usr/bin/env bash
# Exercise an already-installed operator in an isolated namespace/cluster.
set -euo pipefail
namespace="${POLYAD_TEST_NAMESPACE:-polyad}"
kubectl -n "$namespace" rollout status deployment/polyad-polyad --timeout=180s
kubectl -n "$namespace" apply -f examples/finite.yaml
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=180s
finite_uid="$(kubectl -n "$namespace" get graph finite -o jsonpath='{.metadata.uid}')"
original_jobs="$(kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$finite_uid" -o jsonpath='{.items[*].metadata.uid}')"
kubectl -n "$namespace" rollout restart deployment/polyad-polyad
kubectl -n "$namespace" rollout status deployment/polyad-polyad --timeout=180s
resumed_jobs="$(kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$finite_uid" -o jsonpath='{.items[*].metadata.uid}')"
test "$original_jobs" = "$resumed_jobs"
kubectl -n "$namespace" apply -f examples/resources-and-gates.yaml
kubectl -n "$namespace" wait graph/configured --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" apply -f examples/feedback.yaml
kubectl -n "$namespace" wait feedback/recurring --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" apply -f examples/persistent.yaml
kubectl -n "$namespace" wait graph/persistent --for=jsonpath='{.status.ready}'=true --timeout=180s
uid="$(kubectl -n "$namespace" get graph persistent -o jsonpath='{.metadata.uid}')"
deployment="$(kubectl -n "$namespace" get deployments -l "polyad.astrivant.com/owner=$uid" -o jsonpath='{.items[0].metadata.name}')"
selector="$(kubectl -n "$namespace" get deployment "$deployment" -o jsonpath='{.spec.selector.matchLabels.polyad\.astrivant\.com/instance}')"
pod="$(kubectl -n "$namespace" get pods -l "polyad.astrivant.com/instance=$selector" -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "$namespace" patch pod "$pod" --type=merge -p '{"metadata":{"finalizers":["test.polyad.astrivant.com/hold"]}}'
kubectl -n "$namespace" delete graph persistent --wait=false
kubectl -n "$namespace" wait pod/"$pod" --for=jsonpath='{.metadata.deletionTimestamp}' --timeout=60s
# The graph boundary cannot disappear while a descendant has an unacknowledged finalizer.
kubectl -n "$namespace" get graph persistent -o jsonpath='{.metadata.finalizers}' | python3 -c 'import sys; assert "polyad.astrivant.com/drain" in sys.stdin.read()'
kubectl -n "$namespace" patch pod "$pod" --type=merge -p '{"metadata":{"finalizers":[]}}'
kubectl -n "$namespace" wait graph/persistent --for=delete --timeout=120s
kubectl label nodes --all polyad.astrivant.com/capacity=spot --overwrite
kubectl -n "$namespace" apply -f examples/ephemeral.yaml
kubectl -n "$namespace" wait ephemeralgraph/spot-pipeline --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" apply -f examples/ephemeral-interruption.yaml
kubectl -n "$namespace" wait graph/interrupted --for=jsonpath='{.status.nodes.worker.started}'=true --timeout=60s
interrupted_uid="$(kubectl -n "$namespace" get graph interrupted -o jsonpath='{.metadata.uid}')"
job="$(kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$interrupted_uid" -o jsonpath='{.items[0].metadata.name}')"
kubectl -n "$namespace" wait job/"$job" --for=jsonpath='{.status.active}'=1 --timeout=60s
kubectl -n "$namespace" wait pods -l "job-name=$job" --for=condition=Ready --timeout=60s
kubectl -n "$namespace" delete pods -l "job-name=$job" --grace-period=0 --force --wait=true
kubectl -n "$namespace" get graph interrupted -o jsonpath='{.status.completed}' | python3 -c 'import sys; assert sys.stdin.read() == "false"'
kubectl -n "$namespace" wait graph/interrupted --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" delete graph/interrupted graph/finite graph/configured feedback/recurring ephemeralgraph/spot-pipeline --wait=false
