# Repository scripts

Utilities are grouped by responsibility. CI, pre-commit and documentation use
these paths directly.

## Table of contents

- [Groups](#groups)
- [Running checks](#running-checks)

## Groups

| Directory | Purpose |
| --- | --- |
| [tooling](tooling/) | Select the checkout's Python, read tool version pins, and install local or CI tools. |
| [validation](validation/) | Check docstrings, shell scripts, Helm values, Kubernetes manifests and Mermaid diagrams. The Mermaid Node package lives in `validation/mermaid/`. |
| [schemas](schemas/) | Generate resource status, network and Helm reference schemas, or check for drift. |
| [gitops](gitops/) | Generate Argo CD and Flux health configurations from the resource registry. |
| [testing](testing/) | Check built containers and the installed types package; exercise an operator in a test cluster. |
| [release](release/) | Derive a release tag from package metadata. |

## Running checks

Run repository commands from the checkout root:

```sh
bash scripts/tooling/project-python.sh scripts/validation/check-values.py
bash scripts/tooling/project-python.sh scripts/schemas/generate-reference-schema.py --check
bash scripts/validation/check-shell.sh
bash scripts/validation/check-mermaid.sh
npm test --prefix scripts/validation/mermaid
```

The cluster tests in `testing/test-*.sh` require an installed operator and mutate
their test cluster. CI provisions an isolated Kind cluster before running them.
See the [toolchain guide](../docs/development/toolchain.md) for tool installation,
validation prerequisites and release workflows.
