# Development toolchain

Polyad shares Astrivant's pre-commit tooling and four-space Python and shell
formatting. `.tool-versions` pins the local and CI tools; `.python-version` keeps
the Python 3.13 interpreter selected. `.editorconfig` supplies editor indentation.

## Setup

Install asdf and Bash 4.4 or newer, then run from the repository root:

```bash
bash scripts/install-asdf-tools.sh
python -m venv .venv
poetry install
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
Kopf callback annotations receive no blanket exemption. Python remains targeted
at 3.13.

Mermaid checker regression tests run with:

```bash
bash scripts/check-mermaid.sh
npm test --prefix scripts/mermaid
```

## Python types and serialization

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
from polyad.graph import GraphNode, PolyGraph
from polyad.graph.topology import converter

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

Helm property tests cover Polyad's public values and its exposed dependency
settings. Upstream-only Grafana, Prometheus, and controller customization options
are outside this suite. `scripts/kubeconform.sh` adds the pinned Dragonfly schema
to Kubernetes API validation; a contract test keeps it aligned with the CRD and
the upstream dependency.

The Kubernetes job exercises both single-instance Dragonfly and HA. The HA test
temporarily cordons the primary's node in its disposable kind cluster, deletes
the primary pod, and checks graph progress through the promoted replica before
allowing the old primary to return.
