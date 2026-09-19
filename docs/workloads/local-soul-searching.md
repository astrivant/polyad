# Local Soul searching: processes and network topology

[`soul.py`](../../soul.py) is a local Python example built on the SDK's
[`AdaptiveService` ABC](../../pkg/polyad-sdk/README.md#subclass-contract). Its
opening documentation includes ASCII diagrams of the changing TCP network,
process tree and worker-routing graphs. Three service processes own adaptive
child-worker pools. A separate producer loads all three, while a root supervisor
admits changes to the TCP connections between the services. Each service runs the observe, propose, admit,
roll and drain cycle locally.

## Table of contents

- [Run the example](#run-the-example)
- [Read the experiment from the root](#read-the-experiment-from-the-root)
- [Three processes change their topology](#three-processes-change-their-topology)
- [Each service changes its processing tree](#each-service-changes-its-processing-tree)
- [Writing an adaptive application](#writing-an-adaptive-application)
- [Cheeger bounds at two boundaries](#cheeger-bounds-at-two-boundaries)
- [Controls and evidence](#controls-and-evidence)
- [From approved profiles to Natural Selection](#from-approved-profiles-to-natural-selection)

## Run the example

From the repository root, with Python 3.13 or 3.14:

```sh
python -m pip install ./pkg/polyad-types ./pkg/polyad-sdk
python soul.py
```

`poetry install` also installs the local SDK as a development dependency.
No Kubernetes cluster or external service is required.
The services listen on ephemeral `127.0.0.1` TCP ports. The producer supplies
96 jobs per service by default, for 288 verified load results. Opening a peer
connection also sends a separate square-computation job to its destination,
which executes it in a child worker and returns the verified result.

To change the load and timing:

```sh
python soul.py --jobs 128 --work-seconds 0.02 --tick 0.02
python soul.py --help
```

The operation computes an integer square. Both worker representations simulate
one I/O overhead per dispatch using `sleep`; the batch representation amortizes
that overhead over up to four jobs. The controlled delay makes capacity changes
repeatable. This example measures completion of those jobs; calibrate production
profiles against the application's own work and latency budgets.

## Read the experiment from the root

Start with `main()` at the bottom of [soul.py](../../soul.py). It parses settings,
installs termination handling and places `SoulExperiment.run()` inside a
`try/finally` cleanup boundary. The root experiment reads in the order it runs:

```text
start_services -> wait_for ready -> connect_services chain -> start_load
    -> wait_for batch pressure -> connect_services triangle
    -> wait_for restored workers -> connect_services chain
    -> stop_services -> verify_shutdown
```

`wait_for()` continues collecting all service reports while waiting for the
current phase. `connect_services()` checks Cheeger bounds, sends a topology
revision and waits for each service to confirm its actual TCP connections.
The service processes keep adapting their workers concurrently during these
root-level waits.

The code has three entry points to read in sequence:

| Entry point | What it explains |
| --- | --- |
| `main()` | Configuration, process ownership and unconditional cleanup |
| `SoulExperiment.run()` | Experiment phases and the evidence required to advance |
| `AdaptiveService.run()` | The application's concurrent input, policy, worker and drain cycle |

Follow the named methods only when you need their transport or validation
details. Every step has a Google-style docstring describing its inputs,
outputs and relevant failure conditions. The
[Nature example](local-natural-selection.md#read-the-supervisors) uses the same
reading order for changing requirements and capabilities.

## Three processes change their topology

The initial service graph is a directed chain. Sustained backlog activates batch
profiles. Once at least two services report that change, the root admits a
triangle by adding a direct connection from service 0 to service 2. After all
three services complete their work and restore their original worker profile,
the root removes the shortcut and restores the chain.

```mermaid
flowchart LR
    subgraph initial["1. Baseline: chain"]
        a0["Service 0"] --> a1["Service 1"] --> a2["Service 2"]
    end
    subgraph loaded["2. Sustained demand: triangle"]
        b0["Service 0"] --> b1["Service 1"] --> b2["Service 2"]
        b0 -->|"New TCP connection"| b2
    end
    subgraph recovered["3. Recovered: chain"]
        c0["Service 0"] --> c1["Service 1"] --> c2["Service 2"]
    end
```

These arrows are real, persistent TCP peer connections. A newly opened connection
carries a job and its result before the service acknowledges its topology epoch.
The root logs `topology_committed` only after all three services acknowledge that
epoch. The shortcut is closed after its accepted job completes. The original
chain connections remain open until final shutdown.

The independent load producer uses IPC pipes to feed each service's bounded
queue. That traffic drives worker adaptation; TCP peer jobs exercise the admitted
connections. Worker commands and results also use private IPC pipes. The
ownership tree, worker-routing graph and service peer graph each describe a
different relationship.

## Each service changes its processing tree

Each service has the same two approved representations of its capability:

| Profile | Worker processes | Jobs per dispatch | Trigger |
| --- | --- | --- | --- |
| `interactive` | 1 | 1 | Initial state and recovery after a quiet interval |
| `batch` | 3 | Up to 4 per worker | Sustained backlog above the configured high-water mark |

The transition starts replacement workers and waits for their readiness
acknowledgements. Until the replacement set is ready, the current generation
continues accepting dispatches. The service then commits the new generation,
stops assigning new jobs to retiring workers, collects their accepted results,
asks them to exit and joins them.

The four-worker ceiling includes both generations. Scaling up therefore holds
one old interactive worker plus three batch replacements; recovery holds three
old batch workers plus one interactive replacement. A new PID represents the
restored interactive worker. Each service owns its decision independently.

The processing queue holds at most 64 pending jobs, plus one bounded batch per
worker. The script checks result identity and value, rejects duplicate
completion and verifies that every accepted job finishes. After all services
restore baseline and the peer graph returns to its chain, the root requests
shutdown and joins the producer and service processes. Each service joins its
own children. Failures and interruption enter bounded cleanup and exit without
a success record.

## Writing an adaptive application

Start at `service()` in [soul.py](../../soul.py). It owns signal handling and a
`try/finally` boundary around `AdaptiveService.run()` and `close()`. The run loop
names each lifecycle step so the application's safety properties are visible:

```text
receive root commands and peer work
    -> collect results and reap exited workers
    -> admit producer work within the queue limit
    -> observe backlog, completion deltas and quiet time
    -> publish through SDK refresh() and dispatch()
    -> adapt(change) proposes a worker profile
    -> admit it against process and Cheeger budgets
    -> commit only after replacement readiness
    -> dispatch work and drain retiring workers
    -> report recovery or acknowledge complete shutdown
```

| Responsibility | Code to read | Property to preserve |
| --- | --- | --- |
| Application computation | `worker()` | Both execution profiles implement the same work contract |
| Input contracts and backpressure | `receive_producer_work()`, `accept_peer_work()` | Validate before accepting work; stop reading when admission is full or revoked |
| Useful completion | `collect_worker_results()`, `complete_batch()` | Match each result to its accepted identity and verify it before freeing its batch |
| Changing neighbors | `receive_control()`, `connect_peer()`, `retire_closed_peers()` | Apply admitted layouts, verify new links with work and release closed connections |
| SDK observation delivery | `publish_observation()`, `neighborhood()`, `LocalObservations` | Refresh local worker topology, validate events and deliver immutable deltas through the SDK |
| Evidence and policy | `observe()`, `adapt()`, `propose_profile()`, `search_soul()` | Use one immutable observation for the decision; preserve cooldown and sustained demand |
| Safe replacement | `admit_profile()`, `spawn_worker()`, `commit_profile()` | Count old and new workers together; require readiness before dispatch switches |
| Serving and draining | `dispatch_work()`, `reap_workers()` | Stop assigning work to retiring workers; join them after accepted work finishes |
| Experiment verification | `report_recovery()` | Keep the finite demo's required job count and adaptation assertions separate from application serving |
| Resource ownership | `finish_if_stopped()`, `close()` | Acknowledge graceful shutdown after draining; retain cleanup on partial startup and failure |

To write another flavor of application, change the computation, input contracts
and result validator together. For example, replacing integer squares with
record normalization also requires an output validator for normalized records.
Keep the readiness, admission, identity tracking and draining sequence around
that capability. `worker()` accepts a computation function, as demonstrated by
the [Natural Selection example](local-natural-selection.md).

The script's `AdaptiveService` subclasses the SDK's
[`AdaptiveService` ABC](../../pkg/polyad-sdk/README.md#subclass-contract) and
implements `adapt(change)`. Construction supplies a `FreshnessStrategy` for
profile admission; its assessment and a fresh `service.view` check gate profile
proposals. `LocalObservations` supplies a snapshot of the live
worker neighborhood without an HTTP server. `publish_observation()` calls the
inherited `refresh()` and `dispatch()` methods; the SDK validates each event,
builds immutable deltas, runs the configured strategy and invokes `adapt()` before additional hooks and cursor
advancement. The callback uses fresh `resources` deltas to propose a profile.
Elapsed cooldown and quiet windows remain inputs even when backlog is unchanged.

The demo's `run()` owns the local observation loop and process lifecycle. Its
`runtime` settings configure load and worker limits; inherited SDK `settings`
control observation freshness and inventory. For operator-connected applications,
use an authorized SDK `Client` and the SDK subscription loop to deliver upstream
observations to an application-owned supervisor.

A profile proposal changes intent. It creates no worker by itself. Admission
checks current resources and structural bounds before starting replacements,
and commit waits for every replacement to acknowledge readiness. Those separate
steps allow an application to adapt while keeping its promises to accepted work.

For a long-lived service, replace `report_recovery()`'s finite experiment
assertions with the application's health or drain reporting. Unexpected worker
death currently fails the run and enters cleanup; adding retries requires an
explicit policy for work identity, side effects and duplicate delivery.

## Cheeger bounds at two boundaries

`cheeger()` enumerates every distinct cut of these small graphs. It computes
unweighted edge expansion with directions ignored, using Polyad's
[structural definition](../graphs/graph-rules.md#cheeger-bottleneck-bounds).

| Boundary | Baseline | Loaded profile | Exact Cheeger values |
| --- | --- | --- | --- |
| Three service processes | Chain with two TCP edges | Triangle with three TCP edges | `1 → 2 → 1` |
| One service's dispatch/collect components and workers | One worker between dispatch and collect | Three parallel workers between dispatch and collect | `1 → 1.5 → 1` |

At the worker boundary, dispatch and collect are logical components inside the
service process. They connect to each eligible worker through its command/result
pipe. Retiring workers finish previously accepted work and leave the graph used
to admit new dispatches.

A fixed `hard_minimum` remains authoritative. Approved demand profiles select
separate Cheeger targets: `1.5` for batch worker routing and `2` for the service
triangle. The script checks those targets and the hard floor before admitting
a change. It also checks live child counts and a four-change budget per service.
The two boundaries compute expansion independently. Job completion and backlog
are measured separately from these structural values.

## Controls and evidence

All settings are declared together in `Settings` and exposed as CLI options:

| Option | Default | Purpose |
| --- | --- | --- |
| `--jobs` | `96` | Load jobs per service, including three warmup jobs; range 24–2000 |
| `--work-seconds` | `0.05` | Simulated overhead per worker dispatch |
| `--tick` | `0.05` | Service observation and supervision interval |
| `--high-water` | `8` | Outstanding jobs needed to count a high-demand observation |
| `--sustained` | `3` | Consecutive high-demand observations before selecting batch workers |
| `--cooldown` | `0.3` | Minimum seconds between committed worker profiles |
| `--idle-seconds` | `0.6` | Quiet interval before returning to interactive workers |
| `--worker-limit` | `4` | Live children per service, including rolling overlap; maximum 6 |
| `--hard-minimum` | `1.0` | Fixed structural floor; the initial graphs must satisfy it |
| `--timeout` | `20` | Experiment deadline in seconds |

A limit can prevent the requested demonstration. For example,
`--worker-limit 2` rejects the batch replacement instead of exceeding the ceiling.
A run only reports success after both adaptation and restoration occur; an
insufficient burst or infeasible timing configuration fails explicitly.

JSON output includes service identity, PID and monotonic time:

- `observed`: backlog, backlog delta, completed jobs and completion delta.
- `admitted` and `committed`: the worker role, generation and Cheeger evidence.
- `worker_spawned`, `worker_ready`, `worker_joined`: actual process lifecycles.
- `topology_admitted`, `edge_opened`, `peer_work_completed`, `edge_closed` and
  `topology_committed`: proposed changes and evidence of actual TCP execution.
- `baseline_restored`: a service has returned to one worker with all its jobs complete.
- `success`: all 288 default load results are verified and the process tree is joined.

The [integration tests](../../pkg/tests/test_local_soul.py) run real subprocesses
and sockets, verify readiness before role changes, exact topology transitions,
result counts, process ceilings and interruption cleanup:

```sh
poetry run pytest pkg/tests/test_local_soul.py
```

## From approved profiles to Natural Selection

Run [`python nature.py`](../../nature.py) to put a parent Natural Selection
planner above the same worker and policy machinery. The
[local Natural Selection guide](local-natural-selection.md) shows how a new
required outcome selects capabilities and routes, replaces a service, keeps
useful service PIDs alive and retires an excluded process.

This example selects among explicit worker and connection profiles. Application
engineers supply their capability implementations, work contracts and resource
budgets. The [Service Symbiosis SDK](../../pkg/polyad-sdk/README.md) supplies the
observation and control interface for applications deployed through Polyad;
`soul.py` implements its local process supervision directly.

**Automatic capability placement and composition selection extend this
foundation toward Natural Selection.** A planner would match the required outcome
to available implementations, decide which components run in each eligible
process or deployment, and choose how their inputs and outputs connect. It would
also plan readiness, state transfer and draining when those assignments change.

The [Copolyad proposal](../proposals/copolyad.md#from-local-capabilities-to-natural-selection)
defines that planning boundary. An admitted Natural Selection plan owns its
composition and overrides conflicting Soul searching choices. Soul searching
continues adapting the profile settings delegated by that plan, subject to
GraphRules, permissions and resource limits.
