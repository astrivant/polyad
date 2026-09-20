# Local Natural Selection: mutation, survival and retirement

<!-- toc:start -->
**Table of contents**

- [Run the example](#run-the-example)
- [Read the supervisors](#read-the-supervisors)
- [Changing the conditions for survival](#changing-the-conditions-for-survival)
- [How selection works](#how-selection-works)
- [What stays under Soul searching](#what-stays-under-soul-searching)
- [Readiness, revision fences and death](#readiness-revision-fences-and-death)
- [Controls and evidence](#controls-and-evidence)
- [Relationship to Copolyad](#relationship-to-copolyad)
<!-- toc:end -->

[`nature.py`](../../demo/nature.py) runs a parent planner above the worker and policy
machinery in [`soul.py`](../../demo/soul.py). Natural Selection chooses services,
capabilities and their composition. Soul searching adapts worker pools inside
that admitted composition. Both run as real processes with verified work,
bounded resources and graceful shutdown.

## Run the example

Keep the two scripts together and use Python 3.13 or 3.14:

```sh
python -m pip install ./pkg/polyad-types ./pkg/polyad-sdk
python demo/nature.py
python demo/nature.py --jobs 128
python demo/nature.py --help
```

The shared `soul.py` module uses the SDK; `poetry install` also supplies this
local development dependency. No running operator is required. The opening documentation, also printed
by `--help`, includes ASCII drawings of the process tree, selected routes and
rolling handoffs. JSON lines show the decisions and their effects on PIDs.

The default experiment verifies **672 producer jobs** over three environments.
A producer feeds the parent, which relays results between selected service
stages through multiprocessing pipes. The [Soul example](local-soul-searching.md)
demonstrates direct TCP peer connections separately.

## Read the supervisors

Start at `main()` in [nature.py](../../demo/nature.py). It validates configuration,
owns the `Nature` supervisor and always closes its population. The rest of the
experiment follows four short flows:

```text
Nature.run:
    for each environment -> apply its plan -> verify its load
    -> retire the final population

Nature.apply:
    select_plan -> prepare_population -> wait for ready replacements
    -> adopt_revision -> commit_plan -> retire_exclusions

Nature.load:
    start_round -> [admit_jobs -> route_results -> check_round_health]
    -> wait for completed input, results and idle services -> verify_round

Service.run:
    receive results and commands -> observe -> propose_profile
    -> admit_profile -> commit_profile -> dispatch -> report_idle
    -> acknowledge a fully drained stop
```

`LoadRound` holds one experiment's producer and routing ledger. Its `inflight`
entries record which route and stage currently own each job; `complete_stage()`
either sends the result to the next stage or verifies the final outcome.
This keeps transport bookkeeping out of the high-level environment flow.

Both scripts distinguish a policy proposal from admission and readiness commit.
Both service loops use `soul.Observation` for backlog, completion deltas and
timing evidence. Nature's extra responsibility is selecting the capability and
composition revision within which that local worker adaptation happens.

Read the named steps' Google-style docstrings when following a particular
handoff or failure. The [Soul reading guide](local-soul-searching.md#read-the-experiment-from-the-root)
shows the corresponding root flow for peer topology changes.

## Changing the conditions for survival

The initial outcome is a squared integer, with three independent routes and a
total capability cost no greater than four:

```text
                +--> A: square --+
input ----------+--> B: square --+----------> verified square
                +--> C: square --+

cost = 4, composition Cheeger = 1.5
```

The next environment requires an enriched result, `x*x + 1`, through two
independent routes at a total cost no greater than three:

```text
                +--> A': square-plus-one --------+
input ----------+                               +--> verified square-plus-one
                +--> B: square --> D: increment -+

cost = 3, composition Cheeger = 1
```

| Service | Approved capabilities | What happens |
| --- | --- | --- |
| A | Square; fused square-plus-one | Mutates: a replacement service PID activates the fused implementation |
| B | Square | Survives with its existing service PID and now supplies D |
| C | Square, at twice B's cost | Loses its place, drains and exits |
| D | Increment a squared value | Is born as a new service process to complete B's route |

The third environment keeps the enriched outcome. A', B and D retain their
service PIDs through another load round. All services and workers shut down
after verification.

“Complacent” means a service has a fixed capability catalog. B and C both stay
fixed; B survives because its work remains useful within the new budget.
Selection retires C because its alternative composition costs too much. Changing
the catalog costs changes the winner, which the planner tests demonstrate.

## How selection works

`Capability` declares a semantic input, output and executable function.
`Placement` assigns that capability and its resource cost to a logical service.
`Requirement` declares the outcome, number of independent routes and budget.

For example, D accepts `squared` values and produces `enriched` values. It cannot
replace a square service directly because its input contract does not accept
`raw` values. The planner derives the B → D pipeline from compatible contracts.

`natural_selection()`:

1. Enumerates paths from `raw` to the required output through the approved catalog.
2. Forms combinations with the required number of independent routes.
3. Rejects shared service placements, cost overruns, process-limit violations
   and insufficient exact Cheeger values.
4. Accounts for old and replacement service processes existing simultaneously.
5. Ranks feasible plans by total cost, then by the number of new incarnations.

The candidate budget bounds the search; exhausting it blocks admission. The
small catalog keeps exact enumeration practical. The requirement is an explicit
input to each environment. Resource costs are declared policy inputs; completion
counts, elapsed time, backlog changes and observed rates come from real work.

## What stays under Soul searching

Every selected service reuses `soul.worker`, `soul.search_soul`, the approved
interactive/batch profiles and `soul.cheeger`. The shared worker accepts an
optional computation function; its default remains the original square operation.

```text
Nature owns the capability and route
                  |
                  v
service: observe backlog -> Soul searching -> bounded worker handoff
                  |
                  +-- one interactive worker
                  +-- three batch workers under pressure
                  +-- one fresh interactive worker after draining
```

Mutation changes the service's implementation and PID. Soul searching changes
its worker profile while preserving the service PID and capability contract.
An unchanged survivor can therefore keep adapting internally.

The composition and each worker-routing graph have independent Cheeger
calculations. The outer graph includes input, output and service vertices, with
directions ignored. Its expansion changes from `1.5` to `1`. Each service's
dispatch → workers → collect graph changes from `1` to `1.5` and back. The hard
floor stays at `1`; result correctness and completion before the deadline are
checked separately from structure.

## Readiness, revision fences and death

The parent completes each environment's accepted work and waits for every
service to return to its interactive profile before changing the composition.
Replacements become ready before routing changes. Each job and observation
carries a plan revision; stale admissions and duplicate jobs are rejected.

```text
old plan drains
       |
       v
[A, B, C] + start [A', D]    five live service processes during handoff
       |
       v
wait for replacement readiness
       |
       v
adopt new revision -> commit routes -> drain/join old A and C
       |
       v
[A', B, D]                 three live service processes
```

Natural Selection owns the capability and routing decisions. Soul searching
cannot restore C, send work through an old route or change A' back to the square
capability. It continues choosing worker profiles within the active plan.

Death is an actual process exit after a clean drain and join. On interruption or
failure, bounded cleanup stops producers and service subtrees. A failed
experiment emits no success record.

## Controls and evidence

| Option | Default | Purpose |
| --- | --- | --- |
| `--jobs` | `96` | Producer jobs per route in each environment; 96–2000 |
| `--window` | `32` | Shared in-flight limit divided by route count; total limit is this value times the number of routes; 16–64 |
| `--work-seconds` | `0.04` | Simulated overhead per child-worker dispatch |
| `--tick` | `0.02` | Observation and supervision interval |
| `--sustained` | `3` | Consecutive high-backlog observations before selecting batch workers |
| `--cooldown` | `0.1` | Minimum time between worker-profile changes |
| `--idle-seconds` | `0.2` | Quiet time before restoring interactive workers |
| `--worker-limit` | `4` | Live workers per service, including rolling overlap; maximum six |
| `--service-limit` | `3` | Services in an admitted composition; maximum four |
| `--overlap-limit` | `5` | Live service incarnations including replacements; maximum eight |
| `--candidate-limit` | `100` | Maximum candidate combinations examined per selection |
| `--hard-minimum` | `1` | Exact structural floor at composition and worker boundaries |
| `--timeout` | `25` | Deadline in seconds for each load round or lifecycle barrier |

The finite scenario requires three initial services and space for five during
replacement. For example, `--overlap-limit 4` blocks the new composition, then
cleans up the original population. A run that never creates enough sustained
pressure to demonstrate worker adaptation also fails explicitly.

Follow `selection`, `born`, `mutated`, `survived`, `plan_committed` and `retired`
to see composition decisions. `plan_committed` includes added/removed logical
edges and the surviving service PIDs. `observed` and `committed` describe Soul
searching's local response. `round_verified` reports exact completion counts and
the achieved rate for the whole round, including warmup and quiet recovery.
`nature_success` reports an empty, joined population.

The [tests](../../pkg/tests/test_local_nature.py) exercise changed selection
costs, infeasible contracts and bounds, plan fences, real dataflow, stable
survivor identity, readiness before handoff, cancellation and process cleanup:

```sh
poetry run pytest pkg/tests/test_local_nature.py
```

## Relationship to Copolyad

This executable implements a finite local Natural Selection planner and its
process lifecycle. The [Copolyad proposal](../proposals/copolyad.md) extends this
planning boundary to capability discovery, cluster placement, application state
transfer and admission through Polyad. Its [contract language](../proposals/copolyad-language.md)
describes the richer outcome and capability representations.

The local planner uses a fixed catalog, disjoint routes, stateless integer
operations and drained handoffs. These choices make capability composition,
authority over Soul searching and the full lifecycle directly observable.
