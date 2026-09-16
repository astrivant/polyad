#!/usr/bin/env bash
# Confirm a definition revision clears graph readiness before old children disappear.
set -euo pipefail
namespace="${POLYAD_TEST_NAMESPACE:-polyad}"
kubectl -n "$namespace" apply -f examples/finite.yaml
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=180s
uid="$(kubectl -n "$namespace" get graph finite -o jsonpath='{.metadata.uid}')"
job="$(kubectl -n "$namespace" get jobs -l "polyad.astrivant.com/owner=$uid,polyad.astrivant.com/node=first" -o jsonpath='{.items[0].metadata.name}')"
old_uid="$(kubectl -n "$namespace" get job "$job" -o jsonpath='{.metadata.uid}')"
kubectl -n "$namespace" patch job "$job" --type=merge -p '{"metadata":{"finalizers":["test.polyad.astrivant.com/hold"]}}'
kubectl -n "$namespace" patch workload hello --type=json -p '[{"op":"replace","path":"/spec/template/spec/containers/0/command","value":["sh","-c","echo replacement"]}]'
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.phase}'=Draining --timeout=60s
kubectl -n "$namespace" get graph finite -o json | python3 -c 'import json,sys; s=json.load(sys.stdin)["status"]; assert not s["ready"] and not s["completed"]'
kubectl -n "$namespace" patch job "$job" --type=merge -p '{"metadata":{"finalizers":[]}}'
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=180s
new_uid="$(kubectl -n "$namespace" get job "$job" -o jsonpath='{.metadata.uid}')"
test "$old_uid" != "$new_uid"
kubectl -n "$namespace" delete graph/finite --wait=true --timeout=120s
