# Polyad benchmarks

A standalone Python 3.13–3.14 package for repeatable Polyad load studies. Render
and submit graph plans through the SDK, run a mock application and bounded load
generator, and measure activation acceptance and completion. Shared run IDs
correlate results with logs and traces; refresh commands capture, execute and
verify study artifacts. Uses `polyad-sdk`; it does not install the operator or
grant Kubernetes permissions.

Optional [Soul](../../studies/soul/README.md) and [Nature](../../studies/nature/README.md)
process studies exercise SDK strategies and capability changes, retain real job
and process measurements, and plot adaptation before, during and after disturbances.

## Table of contents

- [Install](#install)
- [Commands](#commands)
- [Plans and replicas](#plans-and-replicas)
- [Study lifecycle](#study-lifecycle)
- [Optional study plots](#optional-study-plots)

## Install

From a checkout:

```sh
pip install ./pkg/polyad-types ./pkg/polyad-sdk ./pkg/polyad-benchmarks
```

## Commands

- `polyad-benchmarks-plan --plan studies/load/fixtures/plan.json --render-only`: render a
  composition from a plan using the canonical Helm fixture templates. Omit
  `--render-only` to submit it through the client. Requires Helm and chart dependencies.
- `polyad-benchmarks start --graph-uid UID`: explicitly activate the
  installed study runner, using `POLYAD_API_URL` and optional `POLYAD_API_TOKEN`.
- `polyad-benchmarks run --plan /etc/polyad-benchmarks/plan.json`: execute bounded
  arrivals inside a managed runner Job. Requires the injected graph context and
  `POLYAD_BENCHMARK_FIXTURE_URL`.
- `polyad-benchmarks-fixture`: serve the private mock application endpoint.
- `polyad-benchmarks-fixture --once --delay 0.1`: perform one finite batch.
- `polyad-benchmarks-refresh --ci-phase prepare|study|finish --root DIRECTORY`:
  snapshot, execute and verify an experiment. Cloud study phases require `--context`.
  Add `--suite reachability` during preparation for interaction, state-variable and
  process-rerouting studies without a cluster. Install
  `pip install './pkg/polyad-benchmarks[reachability]'` for their optional HJ backend.
  Follow the [local suite commands](../../studies/symbiosis/README.md#run).
- `python -m polyad_benchmarks.studies.soul --output DIRECTORY` and
  `python -m polyad_benchmarks.studies.nature --output DIRECTORY`: run local process
  studies from a checkout containing the unchanged root demos. Install the `soul`
  or `nature` extra for the corresponding plotting dependencies, or
  `pip install './pkg/polyad-benchmarks[process-studies]'` for both. Use
  `--suite process` in the refresh workflow to run both as a reproducible matrix.
  See [process study refresh](../../studies/soul/README.md#refresh-and-evidence).

## Plans and replicas

The [example plan](../../studies/load/fixtures/plan.json) specifies fixture replicas,
batch execution concurrency, images, placement and arrival parameters. Each new
submission generates a UUID-backed `runId`, submitted as its `requestId`. `composition_plan` renders the versioned chart into a typed
`CompositionRequest`; `submit_plan(client, plan, chart, namespace)` submits it.
The referenced administrator GraphRule must already exist. Each composed run has
its own graph, immutable ConfigMap and runner Job. Reusing its request ID is an
idempotent retry of the same experiment. Use `--run-id SAVED_RUN_ID` or an explicit
plan `requestId` only for that retry; leave them unset for a new run. Rendering
with `--render-only` also generates an ID: pass that ID with `--run-id` when
submitting the reviewed plan.

Start/plan submissions print the run key and a `grafanaPath` in their receipt.
An initial JSON stderr record retains the key even if the response is lost.
Numbered arrivals, fixture and batch logs, runner output and operator traces
carry the same run identity; see [run correlation](../../studies/load/README.md#run-identity-and-correlation).

For the reusable Helm-installed fixture, `polyadResources.variables.run` and
`replicas` become a projected ConfigMap. The runner reads one snapshot at startup
and includes it in its results. Optional Reloader support rolls the fixture
Daemon after plan updates; it never restarts finite batch or runner Jobs.
Change plans between runs. See [plan configuration](../../studies/load/README.md#plans-and-replica-counts).

The [chart reference](../../charts/polyad-benchmarks/README.md#parameters) types
arrival rate, window, request cap, concurrency, execution deadline, HTTP timeout
and polling interval. `POLYAD_BENCHMARK_CONFIG` remains a JSON fallback for direct
CLI runs without `--plan`. `POLYAD_BENCHMARK_FIXTURE_TOKEN` authenticates fixture
calls independently of `POLYAD_API_TOKEN`; neither is serialized into results.

## Study lifecycle

See [the load study](../../studies/load/README.md) for images, deployment, monitoring,
raw artifacts, cleanup and interpretation. The [study index](../../studies/README.md)
explains the shared refresh protocol and its pytest/CI checks. No results represent
cloud measurements until a study has actually run there.

## Optional study plots

Every study exports PNG and SVG figures from its recorded results. Install plotting
on the machine running the refresh, or import it to inspect saved measurements:

```sh
pip install './pkg/polyad-benchmarks[plots]'
```

The `reachability`, `soul`, `nature` and `process-studies` extras also include
Matplotlib. The base package keeps plotting imports lazy; ordinary fixture and
load-generator containers use the base installation. `prepare` and `finish`
verify artifacts without importing Matplotlib. A study checks for its plotting
dependency before initiating cluster work.

Plotters live under `polyad_benchmarks.studies.<study_name>.plotting`, using
underscores for hyphenated study names. The shared import API accepts the full
raw result JSON saved by a refresh:

```python
import json
from pathlib import Path
from typing import Any

from polyad_benchmarks.studies.plotting import render

source = Path(".cache/benchmarks/refresh-RUN/outputs/load/results.json")
measurements: dict[str, Any] = json.loads(source.read_text())
render("load", measurements, Path(".cache/benchmarks/load-plots"))
```

This renders existing evidence without submitting new requests or running an
analysis. For Soul and Nature, use full raw results with samples and graph frames;
their compact published summaries omit those details. Normal refreshes generate
plots automatically and attach figure names and checksums to `results.json`.
Finish requires every expected figure to be intact before publishing measurements
and copying figures to `studies/NAME/figures/`. See the
[figure inventory](../../studies/README.md#study-figures).
