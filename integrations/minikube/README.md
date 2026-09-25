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
contributors. The commands use Bash on macOS or Linux and run from the checkout
root unless stated otherwise.

This integration builds Polyad from a source checkout. You do not need a container
registry account, a published Polyad image, or a host Python/Poetry environment to
install and run it. Python and its dependencies are installed inside the image.
The integration is a repository helper, not a built-in Minikube addon: activation
means running `minikube.sh start`, not `minikube addons enable polyad`.

## What runs

The default profile creates **three Kubernetes nodes**: one control-plane node and
two workers. Polyad itself runs **standalone, without HA**:

- One dense Polyad operator, using a production image built from this checkout.
- One Dragonfly instance and one Dragonfly operator replica.
- A 1 GiB Dragonfly snapshot claim using the multi-node-aware `local-path` class.
- Metrics Server plus Polyad's internal metrics endpoint and per-graph metrics.
- No KEDA installation, horizontal autoscaling, distributed executor fleet, root
  control plane, or Istio installation by default.

Three nodes provide room to schedule application workloads, not control-plane or
cache redundancy. Local-path volumes remain tied to one node and are not
replicated. Dragonfly's periodic snapshots are not a zero-data-loss guarantee.
This is a development environment, not a production availability configuration.

## Install prerequisites

### Install host tools

Install Git, Bash, Minikube, Docker with Buildx, Helm, and kubectl. The optional
asdf setup below requires asdf 0.16 or newer and the developer toolchain's Bash
4.4 or newer. Follow the upstream installers for your operating system:

- [Docker Desktop](https://docs.docker.com/desktop/) on macOS, or
  [Docker Engine](https://docs.docker.com/engine/install/) with the Buildx plugin
  on Linux. Start the Docker daemon and verify access from your normal user.
- [Minikube](https://minikube.sigs.k8s.io/docs/start/) and its
  [Docker driver prerequisites](https://minikube.sigs.k8s.io/docs/drivers/docker/).
  Use a recent stable version; the required local-path addon needs Minikube newer
  than 1.27. Run the integration as your normal user, not with `sudo`.
- [asdf](https://asdf-vm.com/guide/getting-started.html), if you want the repository
  helper to install the pinned Helm and kubectl versions.

For macOS with Homebrew already installed, the CLI prerequisites can be installed
with the following commands. Install and start Docker Desktop separately:

```sh
brew install git bash asdf minikube
export PATH="$(brew --prefix)/bin:$PATH"
```

The default cluster has three nodes configured with 2 CPUs and 2048 MiB each.
Allow memory beyond the nodes' combined 6 GiB allocation for Docker, the image
build, and your other applications. On Docker Desktop, check the VM's resource
allocation as well as the host's available memory. Image layers and node storage
also need free disk space. Internet access is required for chart dependencies,
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
minikube version
docker version
docker buildx version
docker info
helm version --short
kubectl version --client
```

`docker version` must report a reachable server, not only an installed client.
Resolve missing executables or Docker permission/connection errors before
continuing. Keep this shell, including its `PATH`, for the commands below.

## Activate and verify Polyad

### First installation

```sh
bash integrations/minikube/minikube.sh start
bash integrations/minikube/minikube.sh status
```

`start` creates or starts the selected profile, configures storage and metrics,
builds and loads Polyad on all nodes, updates CRDs, installs the chart, and runs
the smoke test. The first run downloads dependencies and builds the production
image, so it can take several minutes. There is no separate activation, Helm
repository registration, namespace creation, or image-push step to perform.

The defaults are Minikube profile/context `polyad`, namespace `polyad`, and Helm
release `polyad`. All cluster commands explicitly target that profile, and
`--keep-context` preserves your current kubectl context. Use a dedicated profile:
addon changes affect the whole selected cluster. If you already have a profile
named `polyad`, choose a different name using the settings below before starting.

The storage setup disables Minikube's single-node hostpath provisioner and default
class, then enables
[Rancher local-path provisioning](https://minikube.sigs.k8s.io/docs/tutorials/local_path_provisioner/).
Unlike the default provisioner, this addon supports multi-node clusters and waits
for a consuming Pod before binding its volume.

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
| `POLYAD_MINIKUBE_DRIVER` | `docker` | Minikube driver; the host Docker daemon is still required for image builds. |
| `POLYAD_MINIKUBE_NODES` | `3` | Node count passed to `start`; changing an existing cluster's topology may require recreation. |
| `POLYAD_MINIKUBE_CPUS` | `2` | CPUs per node passed to `start`. |
| `POLYAD_MINIKUBE_MEMORY` | `2048` | Memory per node passed to `start`, in MiB or a Minikube-supported quantity. |
| `POLYAD_MINIKUBE_TIMEOUT` | `10m` | Timeout per cluster startup, rollout, Helm, and smoke wait. |
| `POLYAD_MINIKUBE_VALUES` | Unset | Optional values overlay path, relative to your current directory or absolute. |

Export custom settings consistently across lifecycle commands. For example:

```sh
export POLYAD_MINIKUBE_PROFILE=polyad-dev
export POLYAD_MINIKUBE_NAMESPACE=polyad-dev
export POLYAD_MINIKUBE_MEMORY=3072
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

Rendering still fetches locked chart dependencies. Its placeholder `polyad:minikube`
image is illustrative; `enable` replaces it with the built image's content tag.
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

`enable` builds `services/operator/Dockerfile` with the `production` target, loads
the image into the selected cluster, refreshes CRDs, and upgrades the Helm release.
It does not create a stopped/missing cluster or run the Graph smoke test; use
`start` for startup, and `test` for verification. Source files are not live-mounted
into the operator, so edits need a rebuild.

The image is tagged with its content ID, so changed image contents change the
Deployment's image reference. Loading uses the host Docker daemon and does not
require `minikube docker-env` or a registry push. `imagePullPolicy: Never` requires
that loaded image. Helm repositories and indexes live under the checkout's ignored
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
they do not start or delete a Minikube profile. They verify cluster targeting,
failure handling, standalone settings, and smoke-test resource ownership. They
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

- **Docker is unavailable or Buildx is missing.** Start the daemon, confirm
  `docker info` works as your normal user, and install the Buildx component from
  Docker's instructions. Installing only the Docker CLI is insufficient.
- **A Pod or claim stays Pending.** Inspect it with `kubectl describe` using the
  explicit context and namespace. Check node capacity and the `local-path`
  StorageClass. A claim can legitimately wait for its first consuming Pod;
  insufficient resources or a missing provisioner need to be resolved first.
- **`ErrImageNeverPull` for the operator.** Run `enable` again to rebuild and load
  the selected image on this profile. Do not change the pull policy to fetch a
  local content tag from a public registry.
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
helper never uses `minikube delete --all` or global Docker cleanup.
