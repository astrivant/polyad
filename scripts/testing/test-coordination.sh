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
            "import json; from polyad.operator.lifecycle.probes import read_health; print(json.dumps(read_health()['scheduler']))"
        ], capture_output=True, text=True)
        if result.returncode == 0:
            health.append((pod['metadata']['name'], json.loads(result.stdout)))
    owners = [state['shards'] for _, state in health]
    flat = [shard for shards in owners for shard in shards]
    if len(owners) == 3 and all(owners) and len(flat) == len(set(flat)) == 32:
        leaders = [pod for pod, state in health if state['leader']]
        if len(leaders) == 1:
            previous_leader = get('get', 'lease', 'polyad-leader')['spec']['holderIdentity']
            subprocess.check_call(['kubectl', '-n', namespace, 'delete', 'pod', leaders[0], '--wait=true'])
            election_deadline = time.monotonic() + 300
            while time.monotonic() < election_deadline:
                elected = get('get', 'lease', 'polyad-leader')['spec']['holderIdentity']
                if elected != previous_leader:
                    print('Verified three exclusive shard owners and election of a replacement leader.')
                    break
                time.sleep(5)
            else:
                raise SystemExit('leader replacement did not complete')
            break
    time.sleep(5)
else:
    raise SystemExit('replicas did not converge to exclusive shard ownership')
PY
kubectl -n "$namespace" apply -f examples/finite.yaml
kubectl -n "$namespace" wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=300s
# Restart shared storage; rescan/reclaim must continue without replacing completed Jobs.
original_jobs="$(kubectl -n "$namespace" get jobs -o jsonpath='{.items[*].metadata.uid}')"
kubectl -n "$namespace" wait pod -l app=polyad-queue --for=condition=Ready --timeout=300s
primary="$(kubectl -n "$namespace" get pod -l app=polyad-queue,role=master -o jsonpath='{.items[0].metadata.name}')"
if [[ "${POLYAD_TEST_DRAGONFLY_HA:-false}" == true ]]; then
    # Prevent the old primary from returning until a replica serves real work.
    primary_node="$(kubectl -n "$namespace" get pod "$primary" -o jsonpath='{.spec.nodeName}')"
    trap 'kubectl uncordon "$primary_node"' EXIT
    kubectl cordon "$primary_node"
    kubectl -n "$namespace" delete pod "$primary" --wait=true
    export POLYAD_TEST_PREVIOUS_PRIMARY="$primary"
    python3 - <<'PYHA'
import json
import os
import subprocess
import time

namespace = os.environ['POLYAD_TEST_NAMESPACE']
previous = os.environ['POLYAD_TEST_PREVIOUS_PRIMARY']
deadline = time.monotonic() + 300
while time.monotonic() < deadline:
    slices = json.loads(subprocess.check_output([
        'kubectl', '-n', namespace, 'get', 'endpointslices',
        '-l', 'kubernetes.io/service-name=polyad-queue', '-o', 'json'
    ]))
    targets = [
        endpoint['targetRef']['name']
        for item in slices['items'] for endpoint in item.get('endpoints', [])
        if endpoint.get('conditions', {}).get('ready') is True
    ]
    if len(targets) == 1 and targets[0] != previous:
        print(f'Primary Service promoted {targets[0]} while {previous} is unavailable.')
        break
    time.sleep(2)
else:
    raise SystemExit('Dragonfly primary failover did not complete')
PYHA
else
    kubectl -n "$namespace" delete pod "$primary" --wait=true
    kubectl -n "$namespace" wait pod/"$primary" --for=create --timeout=180s
    kubectl -n "$namespace" wait pod/"$primary" --for=condition=Ready --timeout=180s
fi
kubectl -n "$namespace" apply -f examples/resources-and-gates.yaml
kubectl -n "$namespace" wait graph/configured --for=jsonpath='{.status.completed}'=true --timeout=300s
resumed_jobs="$(kubectl -n "$namespace" get jobs -o jsonpath='{.items[*].metadata.uid}')"
for uid in $original_jobs; do
    [[ " $resumed_jobs " == *" $uid "* ]]
done
if [[ "${POLYAD_TEST_DRAGONFLY_HA:-false}" == true ]]; then
    kubectl uncordon "$primary_node"
    trap - EXIT
    kubectl -n "$namespace" wait pod/"$primary" --for=create --timeout=300s
    kubectl -n "$namespace" wait pod -l app=polyad-queue --for=condition=Ready --timeout=300s
fi
kubectl -n "$namespace" delete graph/finite graph/configured --wait=true --timeout=180s
kubectl -n "$namespace" scale deployment/polyad-polyad --replicas=2
