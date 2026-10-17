---
name: polyad
description: Develop and verify Polyad operator, SDK, Helm charts and supporting packages.
---

# Work on Polyad

<!-- toc:start -->
**Table of contents**

- [Prepare and inspect](#prepare-and-inspect)
- [Maintain contracts together](#maintain-contracts-together)
- [Validate and hand off](#validate-and-hand-off)
<!-- toc:end -->

Polyad coordinates graph-shaped applications through Kubernetes resources and a
separate Python SDK. Begin with the [README](README.md) and
[operator responsibilities](docs/development/operator-layout.md).
This skill is also [readable on GitHub](https://github.com/astrivant/polyad/blob/main/SKILL.md).
Running checks requires a local checkout; reading this document grants no cluster,
credential, publication or release authority.

## Prepare and inspect

Read Git status and current PR notes before editing. Preserve unrelated work.
Use the committed Poetry lockfile and [shared tool pins](.tool-versions):

```sh
poetry sync --all-extras --no-interaction
npm ci --prefix scripts/documentation --ignore-scripts --no-audit --no-fund
npm ci --prefix scripts/validation/mermaid --ignore-scripts --no-audit --no-fund
poetry run pre-commit install --install-hooks
bash scripts/tooling/build-chart-dependencies.sh
```

Dependency builds need network access. Cluster demos and integration scripts
create resources and require an explicitly selected disposable environment.

## Maintain contracts together

- Runtime and client implementations live under `pkg/`; tests are under `tests/features/`
  and `tests/contracts/`, with shared data and provider helpers in `tests/`.
- Charts live under `helm/`. Edit canonical CRDs, schema sources and annotated values,
  then regenerate with `scripts/schemas/generate-all.py`. Each reference overlay has
  its own `references/<profile>/values.yaml` and generated README.
- Keep API routes in the single composed Flask application. Runtime owners control
  admission, queue bounds and shutdown; preserve authentication and tenancy contracts.
- Run the pinned Ownerloom dependency whenever changing `.github/CODEOWNERS`. Commit hooks
  normalize literal paths; push hooks verify the committed tree. The separate linter
  validates syntax and path coverage.
- Preserve saved measurements and their provenance. Do not rewrite historical results
  merely because current source directories moved.

Keep [generated-artifact ownership](docs/development/generated-artifacts.md) current
when adding or changing pipeline generators. Only reviewed base-owned adapters may
automatically repair conflicts; source-policy ambiguity remains a review task.

## Validate and hand off

```sh
poetry run pytest -n auto
poetry run pre-commit run --all-files
poetry run pre-commit run codeowners-tree-push --hook-stage pre-push --all-files
```

Tests needing PostgreSQL, Dragonfly, Docker or Kubernetes require their documented
services; identify skipped cases explicitly. See [the development guide](docs/development/toolchain.md)
for focused commands and CI responsibilities. Keep source checks parallel with
builds, bind validation to the actual revision, and inspect final generated output.

Open a review PR with the problem, behavior, implementation checklist and exact local
validation. Hosted CI and actual release publication are separate outcomes. The
maintainer reviews, merges and selects a signed tag; never create one just to validate a patch.
