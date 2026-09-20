# Dragonfly dependency provenance

<!-- toc:start -->
**Table of contents**

- [Refresh the dependency and CRD together](#refresh-the-dependency-and-crd-together)
<!-- toc:end -->

Polyad installs [Dragonfly](https://github.com/dragonflydb/dragonfly) through the
[official Kubernetes operator](https://github.com/dragonflydb/dragonfly-operator).
`Chart.yaml` pins operator chart **v1.6.1**; `Chart.lock` locks its resolution.
The OCI artifact digest inspected for this version is
`sha256:a2e9f431f46b0dfb4aee426b70efb4394f970525516ea3f51c8d60a345bbc260`.
Downloaded dependencies under `charts/` are build artifacts, excluded from Git.

`../polyad-crds/crds/dragonflies.yaml` is the upstream chart's rendered `templates/crds.yaml`,
with a provenance comment added. Its Apache-2.0 license is retained in
[LICENSE.dragonfly-operator](LICENSE.dragonfly-operator). Polyad disables the
subchart's templated CRD and installs this copy through Helm's `crds/` phase so a
fresh install can immediately submit a Dragonfly instance. This CRD is installed
even when the bundled cache is disabled, like the other APIs shipped here.

`schemas/dragonfly-dragonflydb-v1alpha1.json` contains that CRD's
converted served-version `openAPIV3Schema` as JSON. The same source produces
the standalone Python schema through the [central pipeline](../../schemas/README.md). The CI Kubeconform wrapper uses
it alongside Kubernetes schemas to validate the managed Dragonfly instance.

## Refresh the dependency and CRD together

After reviewing an upstream release, update the version in `Chart.yaml`, resolve
`Chart.lock`, and download that exact version into a scratch directory:

```sh
helm dependency update charts/polyad --skip-refresh
helm pull oci://ghcr.io/dragonflydb/dragonfly-operator/helm/dragonfly-operator \
  --version v1.6.1 --untar --untardir /tmp/polyad-upstream
helm template upstream /tmp/polyad-upstream/dragonfly-operator \
  --show-only templates/crds.yaml > /tmp/dragonflies.yaml
```

Review `/tmp/dragonflies.yaml` against the vendored CRD, replace the copy, restore
its provenance comment, and update this document and license if needed. Run
the following to refresh the validator schema:

```sh
poetry run python scripts/schemas/generate-all.py
poetry run python scripts/schemas/generate-all.py --check
```

Run
`helm dependency build charts/polyad`, the chart tests, and the single-instance
and HA integration jobs. The chart tests compare the vendored API schema against
the locked dependency.

Helm installs CRDs only on first installation and retains them on uninstall.
Review schema compatibility, then apply CRD changes **before** upgrading the
release. Use server-side apply for this large upstream schema:

```sh
kubectl apply --server-side -f charts/polyad-crds/crds/dragonflies.yaml
helm upgrade polyad charts/polyad --namespace polyad
```

When a Dragonfly operator already manages the cluster, use its external primary
endpoint (`dragonfly.enabled=false`) instead of installing another controller.
