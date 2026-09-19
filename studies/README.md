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
| [Symbiosis](symbiosis/README.md) | Queue envelopes and guard costs for six service interaction categories | Local Python and the optional reachability extra |
| [Reachability state variables](reachability-state/README.md) | Lost information and computation costs for one-, two- and three-variable models | Local Python and the optional reachability extra |
| [Reachability rerouting](reachability-routing/README.md) | Predicted queue safety versus real producer/consumer process behavior | Local Python with process creation |

No cloud measurements are bundled initially. Recorded results must come from an
actual run; the offline smoke checks validate the harness, not operator capacity.

Each study keeps deployment manifests, overlays, plans and its `scenario.json`
under `studies/NAME/fixtures/`. The study README and published `results.json`
remain at the study root. Shared execution code stays in `polyad-benchmarks`.

## Refresh protocol

Following the `hypothesis-helm` study workflow, `polyad-benchmarks-refresh` has
three phases: `prepare`, `study`, and `finish`. Preparation snapshots input JSON
and source hashes once. The Python `STUDIES` and `LOCAL_STUDIES` inventories supply
the prepared matrix; `--suite cluster` is the default, `--suite local` selects
the three local experiments, and `--suite all` selects both.
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

The **Reachability studies** workflow tests the SDK extra on Python 3.11 through
3.14, then runs the local prepare, study matrix and finish phases on ordinary
hosted runners. It retains numerical and real-process measurements as artifacts
without requiring cluster credentials. See the [local suite commands](symbiosis/README.md#run).

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
