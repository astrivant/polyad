# Benchmark studies

Repeatable experiments keep their recipes here and their execution code in
[`polyad-benchmarks`](../pkg/polyad-benchmarks/README.md).

## Table of contents

- [Studies](#studies)
- [Refresh protocol](#refresh-protocol)
- [Study figures](#study-figures)
- [Tests and CI](#tests-and-ci)

## Studies

| Study | Measures | Requires |
| --- | --- | --- |
| [Load](load/README.md) | Activation acceptance and completion under bounded arrivals | Installed operator, benchmark fixture Graph and optional `plots` extra |
| [Symbiosis](symbiosis/README.md) | Queue envelopes and guard costs for six service interaction categories | Local Python and the optional reachability extra |
| [Reachability state variables](reachability-state/README.md) | Lost information and computation costs for one-, two- and three-variable models | Local Python and the optional reachability extra |
| [Reachability rerouting](reachability-routing/README.md) | Predicted queue safety versus real producer/consumer process behavior | Local Python with process creation and optional `plots` extra |
| [Soul process population](soul/README.md) | SDK strategies, worker growth, rolling replacement, admission and recovery across six services | Checkout and optional `polyad-benchmarks[soul]` extra |
| [Nature process population](nature/README.md) | Capability replacement, composition, survival and retirement above local adaptation | Checkout and optional `polyad-benchmarks[nature]` extra |

No cloud measurements are bundled initially. Recorded results must come from an
actual run; the offline smoke checks validate the harness, not operator capacity.

Each study keeps deployment manifests, overlays, plans and its `scenario.json`
under `studies/NAME/fixtures/`. The study README and published `results.json`
remain at the study root. Soul and Nature map directly to
`pkg/polyad-benchmarks/polyad_benchmarks/studies/<study_name>/`, with their own
monitors and strategy modules. Their `figures/` directories contain measured PNG
and SVG plots. Shared execution code stays in `polyad-benchmarks`.

## Refresh protocol

Following the `hypothesis-helm` study workflow, `polyad-benchmarks-refresh` has
three phases: `prepare`, `study`, and `finish`. Preparation snapshots input JSON
and source hashes once. The Python `STUDIES` and `LOCAL_STUDIES` inventories supply
the prepared matrix; `--suite cluster` is the default, `--suite local` selects
all five local experiments. `--suite reachability` selects the three reachability
experiments, `--suite process` selects Soul and Nature, and `--suite all` includes
the cloud study as well.
Study jobs retain independent logs, cluster observations, results and status.
Finish rejects missing, mislabeled, failed or incomplete studies and checks every
figure's inventory and checksum before creating `summary.json`. `--publish`
refreshes `studies/NAME/results.json` and `studies/NAME/figures/` together.

Use a fresh `.cache/benchmarks/refresh-RUN` directory for every experiment; keep
that directory or the corresponding CI artifact to preserve raw measurements.
Preparation refuses to overwrite prior runs. Changed inputs fail before any
cluster request is submitted. Cloud timeouts leave work running for inspection;
local process studies clean up owned processes on failure. Each study documents
its cleanup and artifact lifecycle.

## Study figures

All study plotters ship in the benchmarks package. Install
`polyad-benchmarks[plots]` for rendering saved results and running the cloud or
analytic-only studies. The `reachability` extra adds the numerical backend and
plotting; the existing Soul and Nature extras also include plotting. See the
[importable rendering API](../pkg/polyad-benchmarks/README.md#optional-study-plots).

Every figure places the question it answers beneath its title. These questions
live with the figure inventory in
[`descriptions.py`](../pkg/polyad-benchmarks/polyad_benchmarks/studies/descriptions.py),
so new figures require an explicit question. Legends and run identifiers appear
separately below the question, with space reserved above the measured panels.

| Study | Figures generated as PNG and SVG |
| --- | --- |
| Load | `outcomes`: completion, failure and skipped arrivals; `latencies`: measured acceptance/completion samples and distributions |
| Symbiosis | `interactions`: effects on each participant and analytic admission margins; `guard-cost`: measured guard time and artifact size |
| Reachability state | `state-tradeoffs`: disagreements with the full analytic model and grid growth; `analysis-cost`: guard time, memory estimates, measured RSS and optional solver timings |
| Reachability rerouting | `routing`: actual consumer assignments and queue peaks; `outcomes`: completion, rejection and timing for each repetition |
| Soul and Nature | `topology`, `adaptations`, `outcomes`, `strategies`: process graphs, worker and admission timelines, comparisons and strategy assessments |

New plots use the recorded values and preserve missing measurements as missing.
Numerical analysis disabled in a recipe produces a labeled empty solver panel;
failed requests do not become successful completion samples. Cloud figures are
generated when the installed fixture produces real measurements. Raw figures
remain with each run and in its CI artifacts, even when publication fails.

## Tests and CI

Ordinary pytest includes `pkg/tests/test_benchmarks.py`. The **Benchmarks** workflow
runs those tests and chart rendering on pull requests and pushes, retaining JUnit
and Helm output. It builds both images with Buildx, for AMD64 and ARM64.
Plotting tests verify every study's inventory, preserve failure and missing-data
semantics, and reject missing or altered images before publication.

The **Reachability studies** workflow tests the SDK extra on Python 3.11 through
3.14, then runs the local prepare, study matrix and finish phases on ordinary
hosted runners. It retains numerical and real-process measurements as artifacts
without requiring cluster credentials. See the [local suite commands](symbiosis/README.md#run).

The **Process studies** workflow tests guard reactions, planner isolation and
process ownership on Python 3.13 and 3.14, then runs Soul and Nature through the
same preparation, matrix and verified publication stages. It checks raw evidence
and figure checksums and retains plots, measurements and failure logs.

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
