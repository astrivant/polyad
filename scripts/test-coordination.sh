#!/usr/bin/env bash
# Exercise shard handoff and queue recovery in an isolated installed namespace.
set -euo pipefail
namespace="${POLYAD_TEST_NAMESPACE:-polyad}"
export POLYAD_TEST_NAMESPACE="$namespace"
kubectl -n "$namespace" scale deployment/polyad-polyad --replicas=3
kubectl -n "$namespace" rollout status deployment/polyad-polyad --timeout=180s
# Require real work ownership on all replicas, not just Ready pods.
python3 - <<'PY'
import json
import os
import subprocess
import time

namespace = os.environ['POLYAD_TEST_NAMESPACE']
def get(*args):
    return json.loads(subprocess.check_output(['kubectl', '-n', namespace, *args, '-o', 'json']))

deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    pods = get('get', 'pods', '-l', 'app.kubernetes.io/name=polyad')['items']
    health = []
    for pod in pods:
        result = subprocess.run([
            'kubectl', '-n', namespace, 'exec', pod['metadata']['name'], '--', 'python', '-c',
            "import json,urllib.request; print(json.dumps(json.load(urllib.request.urlopen('http://localhost:8080/healthz'))['scheduler']))"
        ], capture_output=True, text=True)
        if result.returncode == 0:
            health.append((pod['metadata']['name'], json.loads(result.stdout)))
    owners = [state['shards'] for _, state in health]
    flat = [shard for shards in owners for shard in shards]
    if len(owners) == 3 and all(owners) and len(flat) == len(set(flat)) == 32:
        leaders = [pod for pod, state in health if state['leader']]
        if len(leaders) == 1:
            subprocess.check_call(['kubectl', '-n', namespace, 'delete', 'pod', leaders[0], '--wait=true'])
            print('Verified three exclusive shard owners and terminated the elected leader.')
            break
    time.sleep(5)
else:
    raise SystemExit('replicas did not converge to exclusive shard ownership')
PY
kubectl -n "$namespace" apply -f examples/finite.yaml
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=300s
# Restart shared storage; rescan/reclaim must continue without replacing completed Jobs.
original_jobs="$(kubectl -n "$namespace" get jobs -o jsonpath='{.items[*].metadata.uid}')"
kubectl -n "$namespace" delete pod/polyad-dragonfly-0 --wait=true
kubectl -n "$namespace" rollout status statefulset/polyad-dragonfly --timeout=180s
kubectl -n "$namespace" apply -f examples/resources-and-gates.yaml
kubectl -n "$namespace" wait graph/configured --for=jsonpath='{.status.completed}'=true --timeout=300s
resumed_jobs="$(kubectl -n "$namespace" get jobs -o jsonpath='{.items[*].metadata.uid}')"
for uid in $original_jobs; do
  [[ " $resumed_jobs " == *" $uid "* ]]
done
kubectl -n "$namespace" delete graph/finite graph/configured --wait=true --timeout=180s
kubectl -n "$namespace" scale deployment/polyad-polyad --replicas=2
