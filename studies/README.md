# Benchmark studies

Repeatable experiments keep their recipes here and their execution code in
[`polyad-benchmarks`](../pkg/polyad-benchmarks/README.md).

## Table of contents

- [Studies](#studies)
- [Refresh protocol](#refresh-protocol)
- [Tests and CI](#tests-and-ci)

## Studies

| Study | Measures | Requires |
| --- | --- | --- |
| [Load](load/README.md) | Activation acceptance and completion under bounded arrivals | Installed operator and benchmark fixture Graph |

No cloud measurements are bundled initially. Recorded results must come from an
actual run; the offline smoke checks validate the harness, not operator capacity.

Each study keeps deployment manifests, overlays, plans and its `scenario.json`
under `studies/NAME/fixtures/`. The study README and published `results.json`
remain at the study root. Shared execution code stays in `polyad-benchmarks`.

## Refresh protocol

Following the `hypothesis-helm` study workflow, `polyad-benchmarks-refresh` has
three phases: `prepare`, `study`, and `finish`. Preparation snapshots input JSON
and source hashes once. The Python `STUDIES` inventory supplies the CI matrix.
Study jobs retain independent logs, cluster observations, results and status.
Finish rejects missing, mislabeled, failed or incomplete studies before creating
`summary.json`. `--publish` additionally refreshes `studies/NAME/results.json`.

Use a fresh `.cache/benchmarks/refresh-RUN` directory for every experiment; keep
that directory or the corresponding CI artifact to preserve raw measurements.
Preparation refuses to overwrite prior runs. Changed inputs fail before any
cluster request is submitted. A timeout leaves work running for inspection;
cleanup is explicit and documented by each study.

## Tests and CI

Ordinary pytest includes `pkg/tests/test_benchmarks.py`. The **Benchmarks** workflow
runs those tests and chart rendering on pull requests and pushes, retaining JUnit
and Helm output. It builds both images with Buildx, for AMD64 and ARM64.

Manual `full-refresh` uses prepare → study matrix → finish, with the same Python
entry point as local runs. Study jobs use an administrator-provided runner inside
the cluster network with an explicit kubeconfig context. They require read access
to the fixture Graph, definitions, Pods and activation receipts, Pod logs and
`pods/exec` on the fixture. Configure the `benchmarks` GitHub environment and the
`POLYAD_BENCHMARK_RUNNER` repository variable before enabling cloud execution.
Cloud jobs do not provision or destroy GKE or deploy the operator.

Artifacts are uploaded even on failure. Failed measurements remain available but
cannot replace successful published results. Local studies run sequentially;
independent CI study jobs may run in parallel against separately scoped fixtures.
