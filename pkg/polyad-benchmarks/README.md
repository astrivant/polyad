# Polyad benchmarks

A standalone Python 3.13–3.14 package for repeatable operator experiments. Uses
`polyad-client`; it does not install the operator or grant Kubernetes permissions.

## Table of contents

- [Install](#install)
- [Commands](#commands)
- [Plans and replicas](#plans-and-replicas)
- [Study lifecycle](#study-lifecycle)

## Install

From a checkout:

```sh
pip install ./pkg/polyad-types ./pkg/client ./pkg/polyad-benchmarks
```

## Commands

- `polyad-benchmarks-plan --plan studies/load/fixtures/plan.json --render-only`: render a
  composition from a plan using the canonical Helm fixture templates. Omit
  `--render-only` to submit it through the client. Requires Helm and chart dependencies.
- `polyad-benchmarks start --graph-uid UID --run-id RUN`: explicitly activate the
  installed study runner, using `POLYAD_API_URL` and optional `POLYAD_API_TOKEN`.
- `polyad-benchmarks run --plan /etc/polyad-benchmarks/plan.json`: execute bounded
  arrivals inside a managed runner Job. Requires the injected graph context and
  `POLYAD_BENCHMARK_FIXTURE_URL`.
- `polyad-benchmarks-fixture`: serve the private mock application endpoint.
- `polyad-benchmarks-fixture --once --delay 0.1`: perform one finite batch.
- `polyad-benchmarks-refresh --ci-phase prepare|study|finish --root DIRECTORY`:
  snapshot, execute and verify an experiment. The study phase requires `--context`.

## Plans and replicas

The [example plan](../../studies/load/fixtures/plan.json) specifies a unique `requestId`,
fixture replicas, batch execution concurrency, images, placement and arrival
parameters. `composition_plan` renders the versioned chart into a typed
`CompositionRequest`; `submit_plan(client, plan, chart, namespace)` submits it.
The referenced administrator GraphRule must already exist. Each composed run has
its own graph, immutable ConfigMap and runner Job. Reusing its request ID is an
idempotent retry, not a new experiment.

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
