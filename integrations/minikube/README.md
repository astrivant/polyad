# Polyad on Minikube

<!-- toc:start -->
**Table of contents**

- [What runs](#what-runs)
- [Install prerequisites](#install-prerequisites)
  - [Install host tools](#install-host-tools)
  - [Get Polyad and install pinned clients](#get-polyad-and-install-pinned-clients)
  - [Check the prerequisites](#check-the-prerequisites)
- [Activate and verify Polyad](#activate-and-verify-polyad)
  - [First installation](#first-installation)
  - [Confirm the non-HA installation](#confirm-the-non-ha-installation)
  - [Run an application example](#run-an-application-example)
- [Inspect and configure](#inspect-and-configure)
- [Developer workflow](#developer-workflow)
  - [Rebuild after code or chart changes](#rebuild-after-code-or-chart-changes)
  - [Check the integration without a running cluster](#check-the-integration-without-a-running-cluster)
- [Troubleshooting](#troubleshooting)
- [Stop or remove](#stop-or-remove)
<!-- toc:end -->

Install Polyad on your own machine using the canonical
[Polyad Helm chart](../../charts/polyad/README.md). This guide covers first-time
installation for application developers and the rebuild/test loop for Polyad
contributors. Cluster startup uses **QEMU/HVF on macOS** or **KVM2/libvirt on
Linux**, selected automatically. The commands use Bash and run from the checkout
root unless stated otherwise.

This integration builds Polyad from a source checkout. You do not need a container
registry account, a published Polyad image, or a host Python/Poetry environment to
install and run it. Python and its dependencies are installed inside the image.
The integration is a repository helper, not a built-in Minikube addon: activation
means running `minikube.sh start`, not `minikube addons enable polyad`.

## What runs

The default profile creates **three native virtual machines**: one Kubernetes
control-plane node and two workers, each using containerd. These nodes are not
Docker containers. Polyad itself runs **standalone, without HA**:

- One dense Polyad operator, using a production image built from this checkout.
- One Dragonfly instance and one Dragonfly operator replica.
- A 1 GiB Dragonfly snapshot claim using the multi-node-aware `local-path` class.
- Metrics Server plus Polyad's internal metrics endpoint and per-graph metrics.
- Minikube's local registry and per-node registry proxies for the built image.
- No KEDA installation, horizontal autoscaling, distributed executor fleet, root
  control plane, or Istio installation by default.

Three nodes provide room to schedule application workloads, not control-plane or
cache redundancy. Local-path volumes remain tied to one node and are not
replicated. Dragonfly's periodic snapshots are not a zero-data-loss guarantee.
This is a development environment, not a production availability configuration.
On macOS, Minikube manages QEMU/HVF directly, with `socket_vmnet` connecting the
VMs. On Linux it manages KVM2 through libvirt. `virsh` is not used on the macOS path.

## Install prerequisites

### Install host tools

Install Git, Bash, Minikube, `jq`, `curl`, Docker with Buildx, Helm, and kubectl,
plus the platform-specific VM tools below. The optional
asdf setup below requires asdf 0.16 or newer and the developer toolchain's Bash
4.4 or newer. Follow the upstream installers for your operating system:

- **macOS:** [QEMU driver prerequisites](https://minikube.sigs.k8s.io/docs/drivers/qemu/),
  including native QEMU with HVF acceleration and the running `socket_vmnet`
  service. Use native Apple Silicon or Intel binaries, not Rosetta. The helper
  explicitly selects `--driver qemu2 --network socket_vmnet`; the isolated
  `builtin` network is not used for this multi-node cluster. Install
  [crane](https://github.com/google/go-containerregistry/tree/main/cmd/crane)
  for host-side registry pushes. No libvirt installation is needed on this path.
  On Apple Silicon, use QEMU 7 or newer so Minikube can enable high-memory
  addressing for the default 4 GiB VMs.
- **Linux:** [KVM2 driver prerequisites](https://minikube.sigs.k8s.io/docs/drivers/kvm2/),
  including hardware virtualization, libvirt services, and access from your
  normal user. If Linux itself runs in a VM, the outer host must expose nested
  virtualization. Configure the libvirt `default` network, or select another
  existing network with `POLYAD_MINIKUBE_KVM_NETWORK`.
- [Docker Desktop](https://docs.docker.com/desktop/) on macOS, or
  [Docker Engine](https://docs.docker.com/engine/install/) with Buildx on Linux.
  Docker builds the **application image only**, not the Kubernetes nodes. Linux
  needs a local daemon for the loopback push; macOS exports the image and uses
  `crane` because Docker Desktop's daemon runs in a separate VM.
- [Minikube](https://minikube.sigs.k8s.io/docs/start/) and its platform's VM driver.
  Use a recent stable version; the required local-path addon needs Minikube newer
  than 1.27. Run the integration as your normal user, not with `sudo`.
- [asdf](https://asdf-vm.com/guide/getting-started.html), if you want the repository
  helper to install the pinned Helm and kubectl versions.

On macOS, the repository [Brewfile](../../Brewfile) includes QEMU, `socket_vmnet`,
`crane`, Minikube, Docker CLI/Buildx, and the common host tools. From a checkout:

```sh
brew bundle install
sudo "$(command -v brew)" services start socket_vmnet
```

Installing `socket_vmnet` does not start its privileged networking service. Start
that service explicitly as above, then run the integration as your normal user,
not with `sudo`. The Brewfile installs Docker CLI tools, not a Docker daemon;
start Docker Desktop separately. If `docker buildx version` cannot find the
Homebrew plugin, follow its [plugin discovery instructions](https://formulae.brew.sh/formula/docker-buildx).
Helm and kubectl still use the repository's asdf pins below rather than unpinned
Homebrew versions. The Brewfile also includes Kind for its separate integration;
it does not change Kind's container-based node model.

On Linux, install the distribution's KVM/libvirt packages using the KVM2 guide
above. The helper checks capabilities and network access with `virsh`; it does
not install host packages, change group membership, or reconfigure libvirt
automatically. Log in again after an administrator changes your group membership.

The default cluster has three VMs configured with 2 CPUs, 4096 MiB (4 GiB) of memory, and
a 30 GiB virtual disk each. Allow memory beyond the nodes' combined 12 GiB
allocation for Docker, the image build, and your other applications. Image layers
and VM disks also need free host disk space, including a temporary image archive
on macOS. Internet access is required for chart dependencies,
Kubernetes components, Python build dependencies, and public container images.

### Get Polyad and install pinned clients

Clone the public repository, or use your existing checkout:

```sh
git clone https://github.com/astrivant/polyad.git
cd polyad
```

Install only the two clients needed for this integration through asdf:

```sh
export PATH="${ASDF_DATA_DIR:-$HOME/.asdf}/shims:$PATH"
bash scripts/tooling/install-asdf-tools.sh helm kubectl
```

If you do not use asdf, install the Helm and kubectl versions recorded in
[`.tool-versions`](../../.tool-versions) using your preferred method. There is no
need to run the full contributor setup or `pip install polyad` for this workflow.
Kubernetes itself is installed by Minikube during activation; its version is
selected from the repository's kubectl pin.

### Check the prerequisites

Run these checks from the repository root before creating the cluster:

```sh
bash --version
uname -s
minikube version
jq --version
curl --version
docker version
docker buildx version
docker info
helm version --short
kubectl version --client
```

On macOS, also check the native QEMU accelerator, registry client, and network:

```sh
# Apple Silicon; use qemu-system-x86_64 on an Intel Mac.
qemu-system-aarch64 -accel help
qemu-img --version
crane version
brew services info socket_vmnet
test -S "$(brew --prefix)/var/run/socket_vmnet"
```

QEMU must list `hvf`, and `socket_vmnet` must be running with its socket present.
On Linux, check libvirt instead:

```sh
virt-host-validate
virsh --connect qemu:///system domcapabilities --virttype kvm
virsh --connect qemu:///system net-info default
```

Resolve KVM/libvirt permission or capability failures and ensure the selected
network is active before continuing. The default Linux connection is
`qemu:///system`; substitute your overrides in these checks if applicable.
`docker version` must report a reachable server, not only an installed client.
Keep this shell, including its `PATH`, for the commands below.

## Activate and verify Polyad

### First installation

```sh
bash integrations/minikube/minikube.sh start
bash integrations/minikube/minikube.sh status
```

`start` creates or starts the selected native VM profile, configures storage and metrics,
builds Polyad, enables the local registry, pushes the image through a temporary
localhost port-forward, updates CRDs, installs the chart, and runs
the smoke test. The first run downloads dependencies and builds the production
image, so it can take several minutes. There is no separate activation, Helm
repository registration, namespace creation, or image-push step to perform.

The defaults are Minikube profile/context `polyad`, namespace `polyad`, and Helm
release `polyad`. All cluster commands explicitly target that profile, and
`--keep-context` preserves your current kubectl context. Use a dedicated profile:
addon changes affect the whole selected cluster. If you already have a profile
named `polyad`, choose a different name using the settings below before starting.

On an interrupted startup, Minikube may have saved only the first node. Repeating
`minikube start --nodes 3` does not add missing workers to an existing profile.
The helper explicitly adds missing workers, preserves their configured memory
allocation, and waits for all nodes to become Ready before installing addons or
Polyad. It never shrinks the cluster or converts an HA topology.

To repair just the VMs and Kubernetes, without requiring Docker or reinstalling
Polyad, run:

```sh
bash integrations/minikube/minikube.sh recover
```

`recover` **temporarily stops all VMs in the selected profile**, then starts them
again and adds missing workers up to `POLYAD_MINIKUBE_NODES`. It retains existing
disks and workloads, preserves the active kube context, and requires an existing
matching non-HA profile. Use this during a local maintenance window, not while
depending on running workloads. It does not alter host networking or permissions.

An existing Docker-backed profile **cannot be converted in place**. Select a
fresh profile, for example `export POLYAD_MINIKUBE_PROFILE=polyad-vm`, and run
`start`. The helper does not delete old profiles or migrate their data. Leave
`POLYAD_MINIKUBE_DRIVER` unset for automatic selection, or explicitly use `qemu2`
(`qemu` alias accepted) on macOS and `kvm2` on Linux. Incompatible overrides,
Docker-backed profiles, and macOS QEMU profiles using the `builtin` network are
rejected for deployment and testing.

Existing VMs keep their original memory allocation; changing the helper's default
does not resize them. To move from the old 2 GiB allocation to 4 GiB without
deleting existing data, create a fresh profile:

```sh
export POLYAD_MINIKUBE_PROFILE=polyad-4g
export POLYAD_MINIKUBE_MEMORY=4096
bash integrations/minikube/minikube.sh start
```

On Apple Silicon, Minikube also chooses the high-memory QEMU machine settings
when it creates the new VM. Do not reuse an old `highmem=off` machine configuration
with 4096 MiB; that configuration caps RAM at 3 GiB. The
[Minikube QEMU configuration](https://github.com/kubernetes/minikube/blob/v1.39.0/pkg/minikube/registry/drvs/qemu2/qemu2.go)
selects the appropriate settings automatically with QEMU 7 or newer.

The storage setup disables Minikube's single-node hostpath provisioner and default
class, then enables
[Rancher local-path provisioning](https://minikube.sigs.k8s.io/docs/tutorials/local_path_provisioner/).
Unlike the default provisioner, this addon supports multi-node clusters and waits
for a consuming Pod before binding its volume.

The [registry addon](https://minikube.sigs.k8s.io/docs/handbook/registry/) exposes
the same registry as `localhost:5000` on each VM through its registry-proxy
DaemonSet. The helper waits for the registry and proxies, forwards the registry
Service to `127.0.0.1:5000` on the host, and pushes the built image there. It closes
its port-forward before installing Helm, including on push failure. Nodes then
pull `localhost:5000/polyad:<content-tag>` using `IfNotPresent`. No `minikube image
load` or `docker-env` is needed.

This is an **unauthenticated HTTP development registry**. The host port-forward
binds only to loopback, but the addon's registry proxies expose port 5000 on the
VMs. Use a trusted, isolated development network, not a shared production
environment. Containerd is configured to allow `localhost:5000` as an insecure
registry. The addon's registry storage is ephemeral; run `enable` to republish
the image after registry data is lost.

The smoke test checks node and controller readiness, confirms one operator and
one cache instance, and creates a uniquely named Workload and Graph. It waits for
both graph completion and the completed-node metric. Successful runs remove only
their own Graph and Workload; failed runs retain them and print their names for
inspection. It does not apply or delete your application examples.

### Confirm the non-HA installation

```sh
kubectl --context polyad get nodes
kubectl --context polyad -n polyad get deployments polyad-polyad polyad-dragonfly-operator
kubectl --context polyad -n polyad get dragonfly polyad-queue -o jsonpath='{.spec.replicas}'
helm --kube-context polyad --namespace polyad list
bash integrations/minikube/minikube.sh test
```

Expect three `Ready` nodes, both Deployments ready at `1/1`, Dragonfly's replica
count `1`, and Helm release `polyad` listed as deployed. The helper prints
`Polyad standalone smoke test passed.` on success. The smoke Graph is removed
after success, so an empty Graph list immediately afterward is expected.

`ha: false` selects one dense Polyad operator. The integration also fixes
`dragonfly.ha.enabled: false` and one Dragonfly controller replica. These are
separate settings: three Kubernetes nodes do not turn any of these components
into HA. See [deployment profiles](../../docs/deployment/deployment-profiles.md)
for installations that do need replicated control-plane components.

### Run an application example

On a fresh profile, create the finite pipeline example and wait for its two Jobs:

```sh
kubectl --context polyad -n polyad create -f examples/finite.yaml
kubectl --context polyad -n polyad wait graph/finite --for=jsonpath='{.status.completed}'=true --timeout=300s
kubectl --context polyad -n polyad get graph finite -o yaml
kubectl --context polyad -n polyad get jobs,pods
```

This example creates Workload `hello` and Graph `finite`. `create` refuses to
overwrite existing objects with those names. The resulting Graph status should
show completion and `status.metrics.execution.completedNodes: 2`. The
[other examples](../../docs/introduction/getting-started.md#examples) may require
optional APIs, Istio, KEDA, credentials, or placement labels; this local profile
does not enable those features automatically.

## Inspect and configure

Use the explicit context and namespace for your own commands too:

```sh
kubectl --context polyad -n polyad get graphs,polygraphs,replicagroups,pods
kubectl --context polyad -n polyad logs deployment/polyad-polyad -c operator --tail=100
kubectl --context polyad -n polyad port-forward service/polyad-polyad-metrics 8092:8092
```

While the port-forward runs, inspect `http://127.0.0.1:8092/metrics`. The local
overlay enables internal metrics without metrics authentication; it does not
create an ingress or enable the application APIs. See the
[metrics guide](../../docs/operations/metrics.md) for JSON routes and authentication.

| Environment variable | Default | Purpose |
| --- | --- | --- |
| `POLYAD_MINIKUBE_PROFILE` | `polyad` | Dedicated Minikube profile and kubectl context. |
| `POLYAD_MINIKUBE_NAMESPACE` | `polyad` | Namespace for the fixed `polyad` Helm release and smoke resources. |
| `POLYAD_MINIKUBE_DRIVER` | Automatic | `qemu2` on macOS, `kvm2` on Linux. Explicit overrides must match the host; `qemu` aliases `qemu2`. |
| `POLYAD_MINIKUBE_KVM_QEMU_URI` | `qemu:///system` | Linux only: libvirt connection used by Minikube and the `virsh` preflight. |
| `POLYAD_MINIKUBE_KVM_NETWORK` | `default` | Linux only: existing libvirt network selected for the KVM2 VMs. |
| `POLYAD_MINIKUBE_REGISTRY_PORT` | `5000` | Free host loopback port (1024-65535) for the temporary registry forward and push. The VM-side pull port remains 5000. |
| `POLYAD_MINIKUBE_NODES` | `3` | Target node count for `start` and `recover`. Missing workers are added; existing nodes are never removed automatically. |
| `POLYAD_MINIKUBE_NATIVE_SSH` | `true` | Minikube's embedded SSH client. Set `false` to use the host's `ssh` executable during startup and worker provisioning. This does not bypass the driver's TCP readiness probe or macOS Local Network permissions. |
| `POLYAD_MINIKUBE_CPUS` | `2` | CPUs per node passed to `start`. |
| `POLYAD_MINIKUBE_MEMORY` | `4096` | Memory per node passed to `start`, in MiB or a Minikube-supported quantity. Defaults to 4 GiB per VM, 12 GiB for three nodes. Existing VMs are not resized. |
| `POLYAD_MINIKUBE_TIMEOUT` | `10m` | Timeout per cluster startup, rollout, Helm, and smoke wait. |
| `POLYAD_MINIKUBE_VALUES` | Unset | Optional values overlay path, relative to your current directory or absolute. |

Export custom settings consistently across lifecycle commands. For example:

```sh
export POLYAD_MINIKUBE_PROFILE=polyad-dev
export POLYAD_MINIKUBE_NAMESPACE=polyad-dev
export POLYAD_MINIKUBE_MEMORY=4096
bash integrations/minikube/minikube.sh start
```

To add a custom overlay, first create `my-local-values.yaml` in your checkout.
For example, this enables additional graph diagnostics:

```yaml
metrics:
  graphSpectra: true
```

Then apply it to the running profile:

```sh
export POLYAD_MINIKUBE_VALUES="$PWD/my-local-values.yaml"
bash integrations/minikube/minikube.sh enable
bash integrations/minikube/minikube.sh test
```

Substitute your selected profile and namespace in the direct `kubectl` and Helm
commands in this guide. Those commands do not read the `POLYAD_MINIKUBE_*`
variables automatically. Keep private overrides out of version control.

The script applies [the local values overlay](values.yaml), then your optional
overlay, then enforces standalone mode, one operator/cache/controller, disabled
autoscaling, and the locally built image. Other options use the main chart's
[complete values reference](../../charts/polyad/values.yaml). Defaults outside
this integration are unchanged. Remove incompatible settings from optional
overlays if the chart rejects them.

Use `render` to inspect manifests without starting or accessing a cluster:

```sh
bash integrations/minikube/minikube.sh render > /tmp/polyad-minikube.yaml
```

Rendering still fetches locked chart dependencies. Its placeholder
`localhost:5000/polyad:minikube` image is illustrative; `enable` publishes the
built image and replaces the placeholder tag with its content tag.
`enable` also refreshes cluster-scoped CRDs with server-side apply because Helm
does not upgrade CRDs. It does not force conflicting field ownership. Resolve
any reported conflict explicitly. Keep one Polyad installation per profile;
separate namespaces do not isolate shared CRDs and controllers.

## Developer workflow

### Rebuild after code or chart changes

Use the [developer toolchain setup](../../docs/development/toolchain.md#setup)
if you also want to run Python tests, formatters, or schema generators on the
host. Once the cluster is running, use:

```sh
bash integrations/minikube/minikube.sh enable
bash integrations/minikube/minikube.sh test
```

`enable` builds `services/operator/Dockerfile` with the `production` target, pushes
the image to the selected cluster's registry, refreshes CRDs, and upgrades the Helm release.
It does not create a stopped/missing cluster or run the Graph smoke test; use
`start` for startup, and `test` for verification. Source files are not live-mounted
into the operator, so edits need a rebuild.

The image is tagged with its content ID, so changed image contents change the
Deployment's image reference. Buildx targets `linux/arm64` or `linux/amd64` to match
the host's native VM architecture. On Linux, Docker pushes
`127.0.0.1:<host-port>/polyad:<content-tag>` through the temporary forward. On
macOS, `docker image save` exports the built image into a private temporary file
and host-native `crane push --insecure` sends it through the same forward. This
avoids needing a relay container in Docker Desktop's VM. The archive and forward
are cleaned up after success or failure.
The cluster pulls the same image as `localhost:5000/polyad:<content-tag>` with
`imagePullPolicy: IfNotPresent`; changing the host forwarding port does not change
the node-side repository. Helm repositories and indexes live under the checkout's ignored
`.cache/minikube/helm/`; normal Helm repository settings are unchanged. Dependency
versions come from the committed chart lock.

Changes to schema sources need the normal generator before rebuilding:

```sh
poetry run python scripts/schemas/generate-all.py
bash integrations/minikube/minikube.sh enable
bash integrations/minikube/minikube.sh test
```

### Check the integration without a running cluster

After installing developer dependencies and building the chart dependencies:

```sh
bash scripts/tooling/build-chart-dependencies.sh charts/polyad
poetry run pytest pkg/tests/test_minikube.py -n 2
bash scripts/validation/check-shell.sh integrations/minikube/minikube.sh
poetry run python scripts/validation/check-values.py
```

The Python tests record infrastructure commands and render the real Helm chart;
they do not start or delete a Minikube profile. They verify KVM/HVF preflights, native
architecture selection, macOS archive cleanup, Brewfile dependencies, cluster
targeting, registry push ordering and port-forward cleanup, failure handling,
standalone settings, and smoke-test resource ownership. They
do not replace the live `start`/`test` check. When editing shared values or schemas,
also run the normal repository pre-commit checks.

## Troubleshooting

Always use the profile and namespace selected for this integration. These
examples use the defaults:

```sh
minikube --profile polyad status
kubectl --context polyad get nodes
kubectl --context polyad -n polyad get pods,pvc
kubectl --context polyad -n polyad get events --sort-by=.metadata.creationTimestamp
kubectl --context polyad -n polyad logs deployment/polyad-polyad -c operator --tail=100
```

- **QEMU/HVF or `crane` is missing on macOS.** Run `brew bundle install` and check
  the native QEMU binaries and `crane` are on `PATH`. The helper rejects a QEMU
  build without HVF instead of falling back to slow software emulation.
- **DHCP timeout after a guest boot failure.** Check the selected VM's `serial.log`
  under Minikube's `machines/` directory before changing host networking. A
  2 GiB guest can fail with `Initramfs unpacking failed: write error` and a kernel
  panic before it ever requests a DHCP lease. Use a fresh 4 GiB profile as above.
  The default does not resize an existing 2 GiB VM. A missing DHCP leases file
  alone does not prove a firewall problem; do not create an empty leases file.
- **`socket_vmnet` is installed but networking fails.** Start its root-owned service
  using the command above and check its socket. On macOS 15 or later, also allow
  Local Network access for the terminal/IDE running Minikube in System Settings.
  The [QEMU troubleshooting guide](https://minikube.sigs.k8s.io/docs/drivers/qemu/#cannot-connect-to-the-vm-on-macos)
  covers this permission. The helper does not change your firewall or privacy settings.
- **SSH timeout followed by `parsing IP:` in `profile list`.** An interrupted QEMU
  start can leave a running guest whose IP was never saved. Use
  `minikube profile list --light` to inspect metadata without the failing status
  probe, and `ps -axo pid,command | rg '[q]emu-system'` to inspect running VMs.
  The first node's empty internal `Name` is normal; do not replace it manually.
  Once connectivity is restored, run `recover` to rediscover DHCP leases through
  a graceful stop/start. For SSH-client-specific problems, try
  `POLYAD_MINIKUBE_NATIVE_SSH=false bash integrations/minikube/minikube.sh recover`.
  The driver still requires direct TCP access to the guest, regardless of SSH client.
- **KVM2 is unavailable on Linux.** The Linux path requires hardware virtualization
  (including nested virtualization when applicable), and permission to use
  KVM/libvirt. Check `virt-host-validate` and the `virsh` commands above. There is
  no automatic Docker-driver fallback on either platform.
- **Libvirt connection or network fails.** Ensure your user can access the selected
  URI and the network exists and is active. Ask the host administrator to fix
  permissions or networking; the helper does not modify the host configuration.
- **An existing profile uses Docker.** Select a fresh `POLYAD_MINIKUBE_PROFILE` for
  native VMs. `status`, `stop`, `disable`, and `delete` remain available for an old
  profile, but they do not migrate it. Back up any data before deleting it.
- **Docker is unavailable or Buildx is missing.** Start the local daemon, confirm
  `docker info` works as your normal user, and install the Buildx component from
  Docker's instructions. Installing only the Docker CLI is insufficient.
- **A Pod or claim stays Pending.** Inspect it with `kubectl describe` using the
  explicit context and namespace. Check node capacity and the `local-path`
  StorageClass. A claim can legitimately wait for its first consuming Pod;
  insufficient resources or a missing provisioner need to be resolved first.
- **Registry forwarding or push fails.** Check `kube-system` Deployment `registry`
  and DaemonSet `registry-proxy`. If port 5000 is occupied, export
  `POLYAD_MINIKUBE_REGISTRY_PORT=5500` (or another free port) and rerun `enable`.
  The helper fails rather than pushing to an unrelated process already listening
  on that port. On Linux, Docker must run on the same host as the forward. On
  macOS, check `crane` is installed and there is space for the temporary image archive.
- **`ImagePullBackOff` for the operator.** Check the registry/proxy readiness and
  Pod events, then run `enable` again to rebuild and publish the selected image.
  Restarted registry Pods can lose their ephemeral image storage. The intended
  repository is `localhost:5000/polyad`, not a public registry. Existing VM
  profiles must also allow this insecure registry; `start` supplies the flag.
- **An upgrade or smoke test times out.** Inspect events and operator logs before
  retrying. If downloads or startup are merely slow, export
  `POLYAD_MINIKUBE_TIMEOUT=20m` and rerun `start` or `enable`, then `test`.
- **The smoke Graph fails.** Its unique name is printed by the helper. Inspect
  that Graph's YAML and its Jobs/Pods. Failed resources are retained, so resolve
  or delete that specific Graph, followed by its same-named Workload, while the
  operator is still running. Rerunning `test` creates a new pair.
- **CRD field-ownership conflict.** The helper deliberately avoids
  `--force-conflicts`. Confirm that this is your dedicated development profile
  and inspect the other field manager before resolving ownership. Do not force
  an overwrite against a shared cluster.
- **Uninstall is refused.** Drain/delete the listed application boundaries first.
  The operator must remain available to process their finalizers.

## Stop or remove

```sh
bash integrations/minikube/minikube.sh stop
```

`stop` keeps the selected profile and its local state for a later `start`.

If you created the finite example above, remove just those example objects while
the operator is still running:

```sh
kubectl --context polyad -n polyad delete graph finite --wait=true --timeout=300s
kubectl --context polyad -n polyad delete workload hello --wait=true --timeout=300s
```

To uninstall only the release, first drain/delete your Graph, PolyGraph,
ReplicaGroup, Composition, and Rewrite resources while the operator still runs,
then use `disable`. The helper refuses to uninstall while those boundaries remain
in its namespace. It does not explicitly delete the namespace, CRDs, or PVCs;
normal Kubernetes ownership and storage reclaim policies still apply, so back up
anything important before uninstalling.

```sh
bash integrations/minikube/minikube.sh disable
```

To destroy the entire selected local cluster, including its node-local volumes:

```sh
bash integrations/minikube/minikube.sh delete
```

**Deletion is destructive.** Only the named Minikube profile is targeted. The
helper never uses `minikube delete --all`, global Docker cleanup, or blanket
`virsh` domain deletion. Minikube owns the selected profile's VMs and their cleanup.
