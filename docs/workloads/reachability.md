# Reachability and symbiosis models

An application can have spare capacity and still be unable to move work to it
before a queue fills. Reachability analysis asks whether its permitted actions
can achieve the required behavior in time. The SDK supplies explicit service
interaction models, compact queue envelopes and a guard for checking a routing
choice against one of those envelopes.

## Table of contents

- [Viability and the adaptation envelope](#viability-and-the-adaptation-envelope)
- [Describe a service relationship](#describe-a-service-relationship)
- [Model a producer and its consumers](#model-a-producer-and-its-consumers)
- [Use a reachability guard](#use-a-reachability-guard)
- [Compute a numerical comparison](#compute-a-numerical-comparison)
- [Resource requirements](#resource-requirements)
- [Study the state variables](#study-the-state-variables)
- [References](#references)

## Viability and the adaptation envelope

The [adaptation envelope](adaptive-microservices.md#define-the-adaptation-envelope)
describes the operating contracts an application can accommodate from a specified
state within its constraints. **Viability** asks whether an allowed strategy can
keep it within those constraints for the declared horizon. **Constrained
reachability** asks whether an allowed transition can reach its required outcome
before the deadline while respecting the constraints along the way.

For example, a queue has 12 seconds of remaining capacity. Starting a new consumer
takes 20 seconds; an already authorized peer can accept redirected work within
four. Current queue limits still pass, but only the second action can complete
before the queue fills under those assumptions. Backpressure may offer another
permitted response. Timing, readiness and capacity are parts of the contract.

The initial SDK model evaluates a fixed arrival split across fluid queues. A
fluid queue measures divisible units of work and never becomes negative. Its
closed-form bound checks the peak backlog throughout a finite horizon and a
backlog target at the horizon's endpoint. These checks establish this model's
finite contract. A sustained latency objective, discrete job ownership or an
arbitrary sequence of process replacements needs its own model and checks.

## Describe a service relationship

`Interaction` makes the cost or benefit of a relationship explicit in useful
processing capacity per second. The order of the two services determines the
direction of its effects. A positive effect increases capacity; a negative effect
reduces it. Supply measured bounds for the workload and shared resources involved.

| Relationship | First / second effect | Application example |
| --- | --- | --- |
| Neutralism | `0 / 0` | Independent consumers on separately allocated resources |
| Mutualism | `+ / +` | Services sharing reusable computation, with measured savings for both |
| Commensalism | `+ / 0` | One service using a result already published by another, within reserved delivery capacity |
| Parasitism | `+ / -` | One workload gaining processing capacity while consuming a peer's allocated budget |
| Competition | `- / -` | Two services contending for a shared database or CPU allocation |
| Amensalism | `- / 0` | A high-priority workload retaining its allocation while reducing another's capacity |

These categories describe measured effects, including undesirable interactions.
They confer no permissions. Name the work unit, calibrate both effects and
include shared downstream limits. A parasitic model does not discover abusive
workloads or authorize taking resources from another service.

```python
from polyad_sdk.symbiosis.reachability import Interaction, Relationship

relationship = Interaction(Relationship.PARASITISM, effects=(10, -10))
# First service gains 10 records/s; second loses 10 records/s.
```

The [symbiosis study](../../studies/symbiosis/README.md) compares all six categories
under the same demand and queue limits. Effects remain constant for each model
revision. Change the revision and recompute when those assumptions change.

## Model a producer and its consumers

Install the SDK normally for the models and runtime guard. Numerical HJ studies
add an optional dependency:

```sh
pip install 'polyad-sdk[reachability]'
# From this checkout:
pip install ./pkg/polyad-types './pkg/polyad-sdk[reachability]'
```

Define two consumers with a shared `jobs` unit:

```python
from polyad_sdk.symbiosis.reachability import QueueModel, compile_envelope

model = QueueModel(
    names=("busy", "spare"),
    capacities=(10, 35),       # Guaranteed jobs/s after shared budgets are allocated.
    limits=(20, 20),          # Maximum queued jobs.
    targets=(20, 20),         # Required backlog ceilings after the horizon.
    arrival_bounds=(25, 30),  # Total incoming jobs/s, before splitting.
    shares=(0.25, 0.75),      # Approved fixed routing action.
    unit="jobs",
)
envelope = compile_envelope(model, "pipeline-revision-7", horizon=2, lifetime=300)
assert envelope.assess((0, 0))[0]
document = envelope.dumps()
```

Sending all 30 jobs/s to the 10-job/s consumer would grow its queue by 40 jobs
over two seconds. Splitting demand 25/75 gives arrival rates of 7.5 and 22.5
jobs/s, within each consumer's declared capacity. The bound evaluates the highest
arrival rate throughout, including any allowed variation below it.

`warmup_max` adds a third state axis: seconds until the second consumer becomes
ready. That consumer processes no work until the clock reaches zero. Its peak
before readiness is checked even if the final backlog later drains. Consumers
with shared resources must have non-overlapping capacity allocations in the
model; summing independently advertised capacity does not establish that budget.

The fingerprint covers model axes, units, rates, relationship effects, targets
and routing shares. The application revision also identifies topology, capacity
allocations and policy. Publish artifacts through authenticated configuration;
the fingerprint checks consistency and is not a signature.

## Use a reachability guard

`ReachabilityStrategy` implements the SDK's existing
[`ConstraintStrategy`](adaptation-strategies.md#compose-independent-constraints).
Provide it to `AdaptiveService` alongside the other service-specific strategies.
Load artifacts outside the observation callback and supply cheap measurement
callbacks:

```python
import time

from polyad_sdk.symbiosis.reachability import Observation, ReachabilityStrategy

# envelope and model come from the preceding example.
# app owns queue measurements, the current revision and its proposed route.
guard = ReachabilityStrategy(
    "routing-envelope",
    app.record_constraint,
    artifact=lambda: envelope,
    observe=lambda view: Observation(
        state=app.queue_depths(),
        uncertainty=(1, 1),  # Work that may be missing from the measurement.
        observed_at=app.queue_measurement_time(),  # Actual sample time.
        revision=app.current_revision,
        fingerprint=app.proposed_model.fingerprint,
    ),
    max_age_seconds=1,
)

# Recheck before executing the action; all other guards and reservations must pass.
assessment = guard.evaluate(service.view)
if assessment.satisfied and app.other_guards_pass():
    app.reserve_and_apply_route()
```

`app` and `service` above represent the application's scheduler and its
`AdaptiveService`. Its scheduler must serialize competing choices and recheck
the revision during reservation. An observation snapshot is not a reservation.
The proposed fingerprint must describe the action actually being applied.

The guard returns `satisfied`, `blocked` or `unknown`, with a readable reason:

- **Satisfied:** the worst-case queue bound meets this model's contract.
- **Blocked:** this fixed route cannot meet the robust queue bound. Another
  approved route or admission policy may work.
- **Unknown:** evidence is missing, expired, malformed or belongs to another
  revision, routing choice or set of state variables.

Observation uncertainty consumes headroom. So does observation age: before the
proposed route is active, the guard conservatively allows all bounded arrivals
since measurement to have reached any one queue. An unavailable SDK context also
returns `unknown`. The application must honor the assessment; the guard does not
change routes, grant connections or scale Pods itself.

## Compute a numerical comparison

The optional backend uses Stanford ASL's `hj_reachability` in a spawned process.
It computes a backward reachable tube for queue overflow with fixed routing and
the worst permitted arrival rate. The numerical grid extends beyond queue limits
so unsafe states are represented. The runtime guard uses the analytic bound;
numerical diagnostics measure approximation behavior independently.

```python
from polyad_sdk.symbiosis.reachability.hj import AnalysisBudget, analyze

# model comes from the earlier example. Run this in a script's main guard.
if __name__ == "__main__":
    result = analyze(
        model,
        horizon=2,
        budget=AnalysisBudget(
            points_per_axis=17,
            max_points=100_000,
            max_workspace_bytes=256 * 1024 * 1024,
            timeout_seconds=60,
            accuracy="low",
        ),
        samples=((0, 0), (15, 15)),
    )
    print(result)
```

Importing the SDK, models, guard or analysis entry point does not import JAX.
Only the numerical child imports JAX, Flax and the upstream solver. This initial
backend selects CPU and float32 to match its workspace estimate. The parent
enforces the wall deadline, terminates timed-out workers and joins every child.
Results contain versions, model fingerprint, computational domain, sample
comparisons, import/solve/total time, CPU time and peak process RSS where supported.

The numerical safety study covers queue overflow. `Envelope.assess` additionally
checks terminal targets. Neither result silently proves the full application's
latency, delivery, GraphRules or permission contract.

## Resource requirements

| Operation | CPU and memory requirement |
| --- | --- |
| Compile the supported analytic envelope | Scalar configuration validation; no numerical grid |
| Load and run the service guard | Small JSON artifact plus a few scalar operations per state variable; standard library only |
| Compute an HJ comparison | Separate Python/JAX process, JIT compilation, grid arrays and numerical temporaries; CPU backend works without a GPU |

The first implementation supports one to three state variables. At 17 points
per axis, a three-dimensional grid has 4,913 points and a float32 value array uses
19,652 bytes. The workspace estimate reserves additional arrays; it excludes JAX
runtime and compilation overhead. It is a preflight estimate, not an enforced
RSS ceiling. A container memory limit supplies that ceiling in deployment.

At 100 points across six axes, one float32 value array alone would consume
4 TB. The backend rejects dimensions above three and checks point/workspace
budgets before starting a child. More CPU or a GPU does not remove the
exponential growth in grid size.

Measure the deployed service's baseline memory separately from the incremental
guard cost. Use study measurements for `artifactBytes`, guard `meanSeconds`,
analysis `peakRssBytes` and compilation/solve times to choose resources for the
analysis worker. Do not reserve a JAX worker inside every application replica.

The [initial recorded local run](../../studies/symbiosis/results.json) used
Python 3.13.12 on macOS ARM64, `hj_reachability` 0.7.0 and JAX 0.11.2:

| Measurement across the small study models | Observed range |
| --- | --- |
| Serialized runtime envelope | 433 to 483 bytes |
| Mean guard evaluation, 1,000 calls per case | 17 to 51 microseconds |
| Numerical child peak resident memory | Approximately 209 to 243 MiB |
| Child CPU time | 1.23 to 1.97 seconds |
| Total analysis wall time, including startup | 1.49 to 2.58 seconds |

These measurements describe the supplied small grids and local machine. Measure
the intended models on the deployment hardware before assigning worker limits.
The separate process experiment completed all 60 jobs with the admitted split
in both repetitions; the first-consumer baseline rejected 21 or 22 jobs at its
queue limit. Raw records retain the workload, timing and model assumptions.

## Study the state variables

The [state-variable study](../../studies/reachability-state/README.md) compares a
single pooled queue, two separately routed queues, and two queues with a readiness
clock. It measures numerical resource use and identifies cases where omitting a
variable produces an optimistic decision. Aggregation is a modeling assumption;
a result from the pooled model cannot authorize a different full-model action.

The [rerouting study](../../studies/reachability-routing/README.md) then exercises
real producer and consumer processes with the same workload and processing
budget. It records predictions, actual admission, completion, queue peaks,
latency and cleanup. Discrete jobs and OS scheduling provide measured evidence
about the fluid approximation. Results include refusals instead of losing
accepted work to make a prediction look successful.

## References

- [Stanford ASL hj_reachability](https://github.com/StanfordASL/hj_reachability):
  JAX-based HJ solvers and continuous-system modeling interfaces.
- [Hamilton-Jacobi Reachability: A Brief Overview and Recent Advances](https://arxiv.org/abs/1709.07523):
  reachability under bounded disturbances and the cost of higher-dimensional analysis.
- [Scalable Safety-Preserving Robust Control Synthesis](https://arxiv.org/abs/1312.3399):
  viability, reachable sets and constrained control under bounded disturbances.
