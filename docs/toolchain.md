# Development toolchain

Polyad uses pre-commit checks and four-space indentation for Python and shell
scripts. `.tool-versions` pins the local and CI tools; `.python-version` keeps
the Python 3.13 interpreter selected for development. The operator supports
Python 3.13 and 3.14; the standalone client and types packages support Python
3.11 through 3.14. CI runs operator tests on both supported versions and checks
the standalone wheels on each supported version. `.editorconfig` supplies
editor indentation.

## Setup

Install asdf and Bash 4.4 or newer, then run from the repository root:

```bash
bash scripts/install-asdf-tools.sh
python -m venv .venv
poetry install
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm dependency build charts/polyad
poetry run pre-commit install --install-hooks
poetry run pre-commit run --all-files
```

On macOS, put a current Bash on `PATH` ahead of `/bin/bash`. The shell checker
requires the exact ShellCheck and shfmt versions in `.tool-versions`. If you
manage tools without asdf, install those versions using your preferred manager.

To install shfmt with Homebrew, run `brew bundle install` from the repository
root. The [Brewfile](../Brewfile) installs the current Homebrew release; the shell
checker still enforces the version pinned in `.tool-versions`. It searches `PATH`
for a matching executable, so an older formatter in an activated Python virtual
environment does not shadow the correct Homebrew or asdf installation. If no
matching version is installed on `PATH`, the hook reports the versions it found.

`scripts/project-python.sh` uses `.venv/bin/python` when present and otherwise
runs Python through Poetry. Hooks therefore use the project's locked Python
dependencies. Pre-commit installs its pinned docstring and Helm documentation
tools in isolated environments. The Mermaid wrapper runs `npm ci` from its
lockfile when the dependencies or Node version change.

## Formatting and checks

```bash
bash scripts/project-python.sh -m ruff check --fix pkg tests examples
bash scripts/project-python.sh -m ruff format pkg tests examples
git ls-files -z --cached --others --exclude-standard -- '*.sh' '*.bash' | xargs -0 shfmt -w
poetry run pre-commit run --all-files
poetry run pytest
```

Pytest runs in parallel locally and in CI through the `pytest-xdist` development
dependency. The project defaults to automatic CPU-based worker selection, capped
at eight workers. Work stealing redistributes pending tests as workers finish.
The installed-wheel typing checks use the same configuration.

Use `poetry run pytest -n 4` to choose a worker count, or
`poetry run pytest -n 0` for serial debugging. See the
[pytest-xdist execution options](https://pytest-xdist.readthedocs.io/en/latest/distribution.html).
Redis/Dragonfly integration tests use unique namespaces, local socket tests bind
ephemeral ports, and file-writing tests use pytest's isolated temporary directories.

The hooks check Google-style docstrings with pydocstyle and pydoclint, Python
lint and formatting with Ruff, types with mypy, shell scripts with ShellCheck
and shfmt, and Mermaid diagrams in Markdown and standalone diagram files.
All Python docstrings put both opening and closing triple quotes on separate
lines, even for a single sentence. The `docstring-layout` hook enforces this for
modules, classes and functions, including tests and examples. Ruff's `D213` rule
also keeps multiline summaries below the opening quotes; `D200` stays disabled
so tools do not require collapsing short docstrings.

```python
"""
Describe the module, class or function here.
"""
```

Check the layout directly with:

```sh
bash scripts/project-python.sh scripts/check-docstrings.py pkg tests examples scripts
```

Ruff requires postponed annotations and separates imports used only by type
checkers behind `if TYPE_CHECKING:`. Attrs field annotations remain importable
at runtime for cattrs serialization; constructors, base classes, decorators and
other runtime expressions also keep their imports. Dataclass annotations and
Kopf callback annotations receive no blanket exemption. Operator syntax and
static checks target the minimum supported Python version, 3.13; the client
and types packages target 3.11.

Mermaid checker regression tests run with:

```bash
bash scripts/check-mermaid.sh
npm test --prefix scripts/mermaid
```

## Python types and serialization

The shared models live in [`pkg/polyad-types`](../pkg/polyad-types/README.md), an
independently installable Python 3.11+ distribution. Install it from a checkout
with `pip install ./pkg/polyad-types`, then import `polyad_types`. Its runtime
dependencies are attrs, cattrs and typing-extensions. The operator and client
use these same definitions.

Python applications can specialize the node types accepted by a `PolyGraph`.
`PolyGraph[NodeT]` retains that type when code reads `graph.nodes`, so Mypy can
check custom reference fields and reject incompatible nodes before execution.
`NodeT` must extend `GraphNode`; the default is `GraphNode`, which accepts a
mixture of supported graph boundary kinds. The graph is immutable and its type
parameter is covariant, allowing a specialized graph wherever a more general
graph reference is accepted.

See [the checked typing example](../examples/typed_graphs.py) for a `BatchGraph`
reference that restricts its kind to `Graph`. The example demonstrates explicit
type parameters, inferred node types and mixed graph references. These types
describe Python objects; they do not add Kubernetes scheduling behavior or
extend CRD schemas. Kubernetes references still resolve reusable definitions
by kind and name.

For cattrs serialization, supply the same concrete graph type when converting
in both directions:

```python
from polyad_types.codec import converter
from polyad_types.topology import GraphNode, PolyGraph

graph = PolyGraph(
    nodes=(GraphNode(name="batch", kind="Graph", ref="batch-template"),),
)
graph_type = PolyGraph[GraphNode]
document = converter.unstructure(graph, unstructure_as=graph_type)
restored = converter.structure(document, graph_type)
```

Use `PolyGraph[YourReference]` in both calls when the graph contains custom
reference objects. Supplying the concrete type preserves their fields through
the round trip. Custom fields remain Python-library data unless the Kubernetes
schema and compiler explicitly support them.

The wheel ships inline annotations and the package-root `py.typed` marker. CI
installs that wheel into a consumer environment and checks positive and negative
Mypy contracts, including generic `PolyGraph` references. There is no separate
stub package to keep synchronized.

## Version tags

After every successful Test workflow for a push on `main`,
`.github/workflows/tag.yml` receives its completion event. It waits for the Python,
chart and both operator integration jobs, then tags that exact tested commit using
`project.version` from `pyproject.toml`. Stable versions receive `vX.Y.Z`; Python
prereleases use `vX.Y.Z-alphaN`, `vX.Y.Z-betaN` or `vX.Y.Z-rcN`. Pull requests and
other branches cannot create tags.

The first passing commit for a new version creates its tag. Later builds with
the same version leave the existing tag unchanged; bump the package version to
create another tag. Supported versions are `X.Y.Z` and Python prereleases such as
`X.Y.Zrc1`. Tag creation is serialized and only the tagging job gets repository
write permission. New tags, and reruns for a tag already pointing to that tested
commit, invoke the reusable chart workflow to validate and package the Helm chart.

Tags use `GITHUB_TOKEN`, so creating one does not start another push-triggered
workflow. The tagging workflow calls chart validation/build directly after creating
the tag. The Publish to PyPI workflow accepts a manual version-tag input for these automatically created tags.
User-pushed version tags start it directly.
See [GitHub's workflow trigger behavior](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).

## Verified package releases

The release path follows `hypothesis-helm`: reusable CI prepares the tagged source,
checks its release metadata, builds a wheel and source distribution,
and uploads them as `python-distributions-<version>`. The publishing job downloads
those exact artifacts, runs in the `pypi` environment and uses its `PYPI_API_TOKEN`
Secret. Configure environment protection and that credential before publishing.
For tagged builds, the Git tag determines the release version. The shared
`.github/actions/prepare-release` action runs `.github/prepare-release.py` in each
build checkout before dependencies are installed or artifacts are built. For
example, `v0.0.1-alpha3` sets the Python package version to `0.0.1a3` and the chart,
`appVersion` and default image tag to `0.0.1-alpha3`. It also refreshes the chart
README's image-tag default. Alpha, beta and release-candidate spellings are
normalized; malformed tags fail before metadata changes.

The standalone `polyad-client` and [`polyad-types`](../pkg/polyad-types/README.md)
packages receive the same release version. The client and operator pin the matching
types release; Poetry resolves that dependency from `pkg/polyad-types` in a checkout,
while built distributions declare a version dependency suitable for PyPI. CI checks
standalone types and client installations without operator dependencies and publishes
types first, then the client and operator. The PyPI token must permit all three names.

Python builds, both Docker profiles, operator integration tests, every Helm
validation shard, Helm packaging and PyPI publishing use this preparation step.
`.github/release-version.py` still verifies that the resulting package version
matches the tag. Changes exist only in the build checkout: CI does not commit
version bumps or move tags. Release preparation updates the local types lock entry;
Python and Docker builds refresh the lock metadata before installation, retaining
the locked third-party versions.

Branch and pull-request builds retain their declared versions. The automatic
main-branch tagging workflow also continues to derive its tag from the declared
package version; it does not increment that version on every push. To select a
release manually, create its tag on a commit containing this pipeline. Older tags
that contain the previous pipeline still use its strict version check when rerun.

To reproduce release metadata locally before building:

```sh
python .github/prepare-release.py --tag v0.0.1-alpha3
poetry lock
python .github/release-version.py --tag v0.0.1-alpha3
```

### Manual PyPI publishing

Each distribution has its own PyPI metadata and build configuration. Set
`POETRY_PYPI_TOKEN_PYPI` in your shell to a PyPI API token authorized to publish
the package names, then run these commands from the repository root with the
project's Python 3.13 interpreter selected:

```sh
poetry -C pkg/polyad-types check --strict
poetry -C pkg/client check --strict
poetry check --strict

poetry -C pkg/polyad-types publish --build
poetry -C pkg/client publish --build
poetry publish --build
```

Run only the first publish command to release `polyad-types` on its own. When
releasing all three, publish types first because the client and operator require
the matching version. Use the release-preparation commands above when changing
versions so all three distributions and dependency pins stay aligned; PyPI
requires a new version for a subsequent release.

`publish --build` builds the wheel and source distribution, then uploads them to
PyPI. Add `--dry-run` to validate the publishing flow without uploading. See
[Poetry's publish command](https://python-poetry.org/docs/cli/#publish) and
[token configuration](https://python-poetry.org/docs/repositories/#configuring-credentials).

## Verified Helm chart builds

Main-branch pushes and pull requests call `.github/workflows/chart.yml` from the
Test workflow. Chart validation uses `astrivant/hypothesis-helm@main` with **three
shards and two workers per shard**. PRs, main-branch pushes and tagged builds
inherit the action's defaults for test selection, sampling, example counts,
reruns and result caching. The release path does not request a separate
exhaustive mode. Since the action tracks `main`, those defaults follow upstream;
consult its [action definition](https://github.com/astrivant/hypothesis-helm/blob/main/action.yml)
for the current behavior.

All three shards must succeed before Test can permit automatic tagging. The same
workflow validates user-pushed/manual release tags through reusable CI, and is
called directly after automatic tagging. For tagged builds, its package job
requires all three validation shards, packages `charts/polyad`, and uploads
`helm-chart-<tag>` containing the `.tgz` archive for 30 days. Main and PR runs
validate without producing a release chart archive.

The workflow resolves the source commit once and uses that SHA for every shard
and the package job. Packaging also checks that the release tag still identifies
that SHA. The archive retains the chart's declared version; update `Chart.yaml`
when releasing a new chart version. These builds upload workflow artifacts;
they do not publish to a Helm registry or GitHub Release.

## Helm documentation

The pinned Bitnami generator derives the parameters table in
`charts/polyad/README.md` from the `@param` comments in `values.yaml`:

```bash
poetry run pre-commit run helm-readme-generator --all-files
```

Commit any regenerated table with the values change. The chart's hand-authored
`values.schema.json` retains its conditional validation rules. CI runs the same
pre-commit checks, Mermaid regression tests, Python tests, hypothesis-helm chart
validation, and Kubernetes operator integration tests.

Helm property tests cover all generated value paths, including upstream
dependency settings. `scripts/kubeconform.sh` adds the pinned Dragonfly schema
to Kubernetes API validation; a contract test keeps it aligned with the CRD and
the upstream dependency.

The Kubernetes job exercises both single-instance Dragonfly and HA. The HA test
temporarily cordons the primary's node in its disposable kind cluster, deletes
the primary pod, and checks graph progress through the promoted replica before
allowing the old primary to return.

## Compose artifact on main

Each main-branch push runs `astrivant/composer@v0.3.0` against `charts/polyad`.
The `polyad-compose` workflow artifact contains the generated Compose file, its
mount files and the compiler report. Generation uses the chart defaults and does
not commit back to the branch. Kubernetes scheduling and controller behavior still
require a cluster; inspect the report before adapting this output for local use.
