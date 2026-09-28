# Minikube Cluster Autoscaler integration

<!-- toc:start -->
**Table of contents**

- [Activate](#activate)
- [Limits and placement](#limits-and-placement)
- [Exercise growth and shrinkage](#exercise-growth-and-shrinkage)
- [Stop, restart and recover](#stop-restart-and-recover)
- [Migrate an existing Polyad addon](#migrate-an-existing-polyad-addon)
- [Update the dependency](#update-the-dependency)
<!-- toc:end -->

Polyad uses the published
[Minikube Cluster Autoscaler Addon](https://github.com/astrivant/minikube-cluster-autoscaler-addon).
The Go provider, native bridge, container, Helm chart, tests and releases live in
that project. This directory contains only a thin launcher and Polyad lab settings.
It is not a built-in `minikube addons enable` entry.

[source.lock.json](source.lock.json) pins published commit
`fc8444230df55fe7831f7584115f3de002696051` and its source archive's SHA-256.
There is no tagged upstream release at the time of this pin, so `build` builds
the published source. It never follows a moving branch or uses `../` checkouts.
Downloaded source lives under `.cache/minikube/addons/`, separate from private
addon state. No download or cluster change happens unless this integration is used.

## Activate

Start the three-node [Polyad Minikube lab](../README.md) first. Follow the
upstream [host prerequisites](https://github.com/astrivant/minikube-cluster-autoscaler-addon#activate-on-a-cluster).
The launcher also needs curl, tar, jq and either sha256sum or shasum.
Polyad's VM lab uses macOS QEMU/HVF or Linux amd64 KVM2/libvirt. The standalone
addon's Linux arm64 Docker-node option is not a VM backend for this lab.

```bash
bash integrations/minikube/autoscaler/autoscaler.sh build
# Review the example; use a private edited copy for different addresses or limits.
export POLYAD_MINIKUBE_AUTOSCALER_CONFIG="$PWD/integrations/minikube/autoscaler/config.example.json"
bash integrations/minikube/autoscaler/autoscaler.sh init
bash integrations/minikube/autoscaler/autoscaler.sh bridge
```

The launcher selects `POLYAD_MINIKUBE_PROFILE` (default `polyad`). Leave the
bridge running under your task/process supervisor. In another terminal:

```bash
bash integrations/minikube/autoscaler/autoscaler.sh enable
bash integrations/minikube/autoscaler/autoscaler.sh test
bash integrations/minikube/autoscaler/autoscaler.sh status
```

The configuration is persisted by `init`; subsequent terminals need not export
its path. If you set a custom profile or `MINIKUBE_AUTOSCALER_STATE_DIR`, use the
same settings in **every** terminal, including when running `minikube.sh` or
`full.sh`. The upstream state default is
`${XDG_STATE_HOME:-$HOME/.local/state}/minikube-cluster-autoscaler-addon/<profile>`.
Other upstream settings, such as `MINIKUBE_AUTOSCALER_IMAGE`, pass through unchanged.
Conflicting upstream and Polyad profile/configuration settings are rejected.

The example binds the provider to `192.168.105.1:50051`, the usual socket_vmnet
host gateway. Verify that address; Linux users must supply their libvirt bridge
address. Follow the upstream [configuration and network security reference](https://github.com/astrivant/minikube-cluster-autoscaler-addon/blob/main/docs/configuration.md).
The addon requires Kubernetes 1.35.x. Both links use mutual TLS; never expose
the provider or native bridge on an untrusted network.

## Limits and placement

The Polyad example allows zero to two **additional** workers with
`maxTotalMemoryMiB: 20480`. Three base VMs at 4096 MiB each can grow to five VMs.
The ceiling excludes host and Docker overhead. The addon validates this budget
against the existing Minikube profile; it does not resize base VMs.

The full lab and upstream addon share the placement label
`minikube-autoscaler.astrivant.com/pool=base`. Infrastructure stays on base nodes.
Elastic workers have pool `elastic` and the taint
`minikube-autoscaler.astrivant.com/elastic=true:NoSchedule`. Polyad retains its
own `polyad.astrivant.com/minikube-worker=true` label to keep operator replicas
on base workers. Ownership, eviction checks, recovery and safety limits are
documented [upstream](https://github.com/astrivant/minikube-cluster-autoscaler-addon/blob/main/docs/operations.md).

## Exercise growth and shrinkage

This small Polyad fixture explicitly targets the elastic pool, so it works even
when the three base VMs have spare capacity:

```bash
kubectl --context polyad create -f integrations/minikube/autoscaler/demand.yaml
kubectl --context polyad -n polyad-elastic-demo rollout status deployment/demand --timeout=16m
kubectl --context polyad get nodes -L minikube-autoscaler.astrivant.com/pool
kubectl --context polyad -n polyad-elastic-demo scale deployment/demand --replicas=0
```

Scale to two replicas to exercise the configured ceiling. After scaling to zero,
allow the autoscaler's five-minute idle window before expecting removal. Do not
put persistent application data on disposable test workers. For k6/HPA scenarios,
use the upstream [load-test demo](https://github.com/astrivant/minikube-cluster-autoscaler-addon/blob/main/docs/demo.md);
its default one-base-node topology differs from this three-base-node lab.

## Stop, restart and recover

```bash
bash integrations/minikube/autoscaler/autoscaler.sh disable
# Stop the foreground bridge with Ctrl-C.
```

Disabling retains VMs, journals, credentials and base placement. It is not a
scale-to-zero command. Drain idle elastic workers before disabling if desired.
Before `minikube.sh start`, `recover` or `stop`, disable the addon and stop its
bridge. Polyad verifies the provider container and native maintenance lock and
preserves the current dynamic VM count. Cluster deletion is refused while
either the old or new ownership journal exists.

Inspect `<state-dir>/provider/state.json` on errors. After repairing the cause,
use `autoscaler.sh resume` with both bridge and container stopped, then restart
them. Never reuse journals with a replacement cluster, remove active journals,
or copy old journals into the standalone addon's state directory.

## Migrate an existing Polyad addon

Migration is deliberately **not automatic**. The legacy addon has different
worker identities, labels, release/container names and state ownership. The
launcher refuses activation while `.cache/minikube/autoscaler/<profile>/provider/state.json`
exists. Removing source code does not stop the running legacy addon.

For the default `polyad` profile:

1. Stop test demand and let the **legacy** autoscaler drain all elastic workers.
   Confirm `.Workers` is empty in its provider journal and only base VMs remain.
   Do not migrate while workers or incomplete operations remain.
2. Uninstall only the old autoscaler release and stop its host provider:

   ```bash
   helm uninstall polyad-minikube-autoscaler --kube-context polyad -n polyad --wait --timeout 5m
   docker stop --timeout 30 polyad-minikube-autoscaler-polyad
   docker rm polyad-minikube-autoscaler-polyad
   ```

3. Stop the legacy native bridge. The retained, ignored legacy binary can verify
   that its lock is released before you archive state:

   ```bash
   .cache/minikube/autoscaler/bin/provider --mode=maintenance-check \
     --config="$PWD/.cache/minikube/autoscaler/polyad/config.json" \
     --state-dir="$PWD/.cache/minikube/autoscaler/polyad"
   jq -e '.Workers | length == 0' .cache/minikube/autoscaler/polyad/provider/state.json
   ```

4. Privately archive that exact profile directory outside its active path.
   Preserve its permissions, credentials and journals. If the legacy binary is
   missing, recover it from the old Polyad revision before proceeding; do not
   bypass the maintenance check. No migration step should delete VM data.
5. Follow [Activate](#activate) to build and initialize **fresh** upstream state.
   Initialization labels the base nodes for the new addon. Refresh the full lab
   with `bash integrations/minikube/full/full.sh enable` to align Helm-managed
   placement. Existing legacy labels can remain inert.
6. Update any custom elastic workload selectors and tolerations to the new keys
   in [Limits and placement](#limits-and-placement), then rerun the growth test.

Adjust every command consistently if the old profile or namespace was customized.

## Update the dependency

Review an upstream commit or release, then update the immutable revision and
independently verified archive checksum in `source.lock.json`. Run the launcher
tests and render the external chart before committing the pin. `fetch` downloads
without building or contacting Kubernetes; `path` prints the source path offline:

```bash
bash integrations/minikube/autoscaler/autoscaler.sh fetch
bash integrations/minikube/autoscaler/autoscaler.sh path
```

Once release archives are available, this bootstrap can use their platform
binaries instead of source builds. Do not copy the Go implementation, Dockerfile
or chart back here. The full lab's ProvisioningRequest CRD remains local because
Polyad's capacity API needs it even when node autoscaling is disabled.
