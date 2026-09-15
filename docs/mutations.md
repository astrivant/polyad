# Mutation plans

A workload graph describes what should run. A mutation plan describes changes to
that graph or its resources, and which changes may execute together. For example,
scaling ingestion and reporting can share a batch when their effects are independent
and their combined demand fits the parent's capacity limit.

The library provides attrs models in `polyad.compiler.asts.mutations`, a pure
`compiler.passes.mutations.compile_mutations` pass, and an async
`operator.mutations.execute_mutations` executor. These are Python extension APIs;
they do not add a CRD or accept executable callbacks through the composition API.

See [mutation diagram patterns](mutation-diagrams.md) for independence squares,
retry and refinement triangles, joinability diamonds, commuting cubes and compiler
preservation, with their assumptions and current implementation limits.

## Describe effects and shared bounds

```python
from polyad.compiler.asts.mutations import Budget, BudgetDelta, Mutation, Precondition, Scope
from polyad.compiler.passes.mutations import compile_mutations

ingestion = Scope(("kubernetes", "apps/v1", "Deployment", "production", "ingestion"))
reporting = Scope(("kubernetes", "apps/v1", "Deployment", "production", "reporting"))

changes = (
    Mutation(
        name="scale-ingestion",
        writes=(ingestion,),
        preconditions=(Precondition(Scope((*ingestion.path, "metadata", "resourceVersion")), "42"),),
        deltas=(BudgetDelta("application-replicas", 2),),
        effects_complete=True,
    ),
    Mutation(
        name="scale-reporting",
        writes=(reporting,),
        preconditions=(Precondition(Scope((*reporting.path, "metadata", "resourceVersion")), "19"),),
        deltas=(BudgetDelta("application-replicas", 2),),
        effects_complete=True,
    ),
)

plan = compile_mutations(
    changes,
    budgets=(Budget("application-replicas", current=5, minimum=0, maximum=10),),
    max_parallelism=2,
)
assert plan.batches == (("scale-ingestion", "scale-reporting"),)
assert len(plan.independences) == 1
```

This example describes increasing replicas from 2 to 4 and 3 to 5. The application
ends with nine replicas. Reducing the maximum to eight rejects the plan before any
operation runs, even though either increase would fit on its own.

`effects_complete=True` is a contract supplied by a trusted adapter, not an
inference from node names. The example assumes no additional shared constraints or
service dependencies. Real adapters must include policy versions, readiness
requirements, shared resources, external effects and transport conflicts where
relevant. The default is `False`, which orders the operation against all others.

## Ordering and evidence

- **Scope overlap:** paths use segments, not string prefixes. A write to a whole
  object conflicts with reads or writes anywhere below it. Read/read overlap is safe.
- **Preconditions:** an expected value is also a read dependency. Missing observations
  fail closed; `None` explicitly means the observer established absence.
- **Explicit ordering:** `after=("replacement-ready",)` waits for that operation to
  finish. Its adapter must actually wait for readiness, or a subsequent precondition
  must check readiness. A Kubernetes write acknowledgement alone is insufficient.
- **Unknown effects:** keep deterministic serial order. Explicit dependencies take
  precedence over input order. Cycles and unknown predecessors are rejected.
- **Shared counters:** integer deltas must fit declared bounds for every possible
  completion order within a batch. A concurrent release cannot fund an increase;
  put the release in an earlier batch with an explicit dependency. The planner
  rejects unsafe batches rather than inventing a migration order.
- **Audit:** `plan.orderings` explains required sequencing; `plan.independences`
  records assumptions for every concurrent pair. Serialize plans with
  `polyad.compiler.asts.converter.unstructure(plan)` and restore them with
  `converter.structure(document, MutationPlan)`.

Represent a shared nonnumeric invariant with a common scope in the affected
operations' reads/writes. For Kubernetes updates, include a whole-object write scope
even when changing different fields if they share a resource-version fence.

## Execute a plan

`execute_mutations(changes, observe=..., apply=..., budgets=...,
observe_budgets=..., max_parallelism=2)` compiles the plan and executes its batches.

The adapter supplies:

| Callback | Contract |
| --- | --- |
| `observe(mutation)` | Refresh and return a mapping from each precondition's `Scope` to its string value or explicit `None` |
| `apply(mutation)` | Perform the named operation with server-side preconditions and return only once its declared completion condition holds |
| `observe_budgets()` | Return fresh integer counter values; required whenever budgets are declared |

All preconditions in a batch are checked before any of its operations dispatch.
Counters are refreshed before each batch and must match the projected usage from
previous successful batches. Drift stops execution and requires replanning.
Concurrency defaults to one and never exceeds `max_parallelism`.

If a callback fails, other in-flight operations settle before the error is raised;
later batches do not start. Multiple failures are reported as an exception group.
Cancellation also waits for in-flight callbacks, so adapters need bounded transport
timeouts. There is no automatic rollback or retry: refresh state and use durable
receipts to recover partially applied plans.

The caller must retain coordination covering **all shared scopes and counters** for
the execution, and callbacks must use optimistic server-side fences. An observation
is not a lock. This executor does not reserve cluster quota, coordinate independent
callers, or grant permission to bypass Polyad's existing write queue.

## Operator integration and limits

The existing `Rewrite` controller now uses the executor for its topology replacement.
It refreshes target UID, generation, resource version and deletion state before
dispatch, then writes through the existing guarded API queue with a resource-version
fence. Atomic rewrite receipt annotations still recover lost acknowledgements.
Whole-topology replacement retains incomplete effects and remains serialized within
its root graph family. This change does not enable concurrent sibling reconciliation
or replace shard leases.

Independence records describe the declared state model. They do not establish
equivalent downtime, traffic exposure or application side effects. Adapters must
account for transient capacity within each operation as well as its final delta.
Nested `PolyGraph` composition remains distinct from a mathematical polygraph;
these plans provide explicit mutation relations without claiming general confluence
or higher-category coherence proofs.
