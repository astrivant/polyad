#!/usr/bin/env bash
# Exercise an already-installed operator in an isolated namespace/cluster.
set -euo pipefail
namespace="${POLYAD_TEST_NAMESPACE:-polyad}"
kubectl -n "$namespace" rollout status deployment/polyad-polyad --timeout=180s
kubectl -n "$namespace" apply -f examples/finite.yaml
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.metrics.execution.completedNodes}'=2 --timeout=60s
kubectl -n "$namespace" get graph finite -o json | python3 -c '
import json, sys
obj = json.load(sys.stdin)
metrics = obj["status"]["metrics"]
assert metrics["observedGeneration"] == obj["metadata"]["generation"]
assert metrics["topology"]["admission"]["layerWidths"] == [1, 1]
assert metrics["topology"]["admission"]["breadthDepth"] == 2
assert metrics["observedTopology"]["nodeCount"] == 2
'
finite_uid="$(kubectl -n "$namespace" get graph finite -o jsonpath='{.metadata.uid}')"
original_jobs="$(kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$finite_uid" -o jsonpath='{.items[*].metadata.uid}')"
kubectl -n "$namespace" rollout restart deployment/polyad-polyad
kubectl -n "$namespace" rollout status deployment/polyad-polyad --timeout=180s
resumed_jobs="$(kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$finite_uid" -o jsonpath='{.items[*].metadata.uid}')"
test "$original_jobs" = "$resumed_jobs"
kubectl label nodes --all workloads.example.com/capacity=on-demand --overwrite
kubectl -n "$namespace" apply -f examples/storage-and-delay.yaml
kubectl -n "$namespace" wait graph/stored-work --for=jsonpath='{.status.delays.record.notBefore}' --timeout=120s
deadline="$(kubectl -n "$namespace" get graph stored-work -o jsonpath='{.status.delays.record.notBefore}')"
kubectl -n "$namespace" wait graph/stored-work --for=jsonpath='{.status.completed}'=true --timeout=180s
stored_uid="$(kubectl -n "$namespace" get graph stored-work -o jsonpath='{.metadata.uid}')"
kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$stored_uid" -o json | python3 -c '
import json, sys
from datetime import datetime
job = json.load(sys.stdin)["items"][0]
# Kubernetes creation timestamps have whole-second precision.
assert datetime.fromisoformat(job["metadata"]["creationTimestamp"].replace("Z", "+00:00")) >= datetime.fromisoformat(sys.argv[1]).replace(microsecond=0)
pod = job["spec"]["template"]["spec"]
assert pod["nodeSelector"]["workloads.example.com/capacity"] == "on-demand"
assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "application-data"
' "$deadline"
kubectl -n "$namespace" delete graph/stored-work --wait=true --timeout=120s
kubectl -n "$namespace" get pvc application-data
kubectl -n "$namespace" delete pvc application-data --wait=true --timeout=120s
kubectl -n "$namespace" apply -f examples/resources-and-gates.yaml
kubectl -n "$namespace" wait graph/configured --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" apply -f examples/feedback.yaml
kubectl -n "$namespace" wait feedback/recurring --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" wait feedback/recurring --for=jsonpath='{.status.metrics.resources.total}'=0 --timeout=60s
kubectl -n "$namespace" apply -f examples/persistent.yaml
kubectl -n "$namespace" wait graph/persistent --for=jsonpath='{.status.ready}'=true --timeout=180s
kubectl -n "$namespace" wait graph/persistent --for=jsonpath='{.status.metrics.execution.readyNodes}'=1 --timeout=60s
kubectl -n "$namespace" get graph persistent -o json | python3 -c '
import json, sys
metrics = json.load(sys.stdin)["status"]["metrics"]
assert metrics["execution"]["completedNodes"] == 0
assert metrics["topology"]["connections"]["cyclicComponents"] == 1
assert metrics["topology"]["connections"]["condensation"]["depth"] == 1
'
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
kubectl -n "$namespace" apply -f examples/polygraph.yaml
kubectl -n "$namespace" wait polygraph/composed --for=jsonpath='{.status.completed}'=true --timeout=300s
kubectl -n "$namespace" wait polygraph/composed --for=jsonpath='{.status.metrics.rollup.completedLeafNodes}'=2 --timeout=60s
kubectl -n "$namespace" get polygraph composed -o json | python3 -c '
import json, sys
rollup = json.load(sys.stdin)["status"]["metrics"]["rollup"]
assert rollup["observationsComplete"]
assert rollup["graphCount"] == 5
assert rollup["nestingDepth"] == 3
assert rollup["resourceCount"] == 6
'
kubectl -n "$namespace" delete polygraph/composed --wait=true --timeout=180s
kubectl -n "$namespace" apply -f examples/ephemeral.yaml
kubectl -n "$namespace" wait ephemeralgraph/spot-pipeline --for=jsonpath='{.status.completed}'=true --timeout=180s
kubectl -n "$namespace" wait ephemeralgraph/spot-pipeline --for=jsonpath='{.status.metrics.execution.completedNodes}'=1 --timeout=60s
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
