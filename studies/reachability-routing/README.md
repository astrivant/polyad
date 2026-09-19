# Reachability: predicted and measured rerouting

Exercise the SDK queue guard with one producer process and two consumer
processes. Compare sending everything to the first consumer with an approved
25/75 split that uses the second consumer's spare capacity. The workload and
number of consumer processes stay fixed.

## Table of contents

- [Process experiment](#process-experiment)
- [Run](#run)
- [Read the evidence](#read-the-evidence)

## Process experiment

[`fixtures/scenario.json`](fixtures/scenario.json) sets 60 jobs at 30 jobs/s,
consumer capacities of 10 and 35 jobs/s, and 20-job outstanding-work limits.
Each consumer checks an integer's square after a service delay. A coordinator
owns routing, retains job identities until verified completion and records
explicit rejections when admission is unavailable. Bounded multiprocessing
channels carry the actual jobs and results.

The baseline sends to the first consumer and enforces its queue limit. It records
the guard's prediction without using it to alter the baseline. The guarded
variant evaluates `ReachabilityStrategy` before admitting each job and applies
the approved weighted split. Both verify every accepted result and drain and
join all processes. A failed or timed-out run terminates its process tree.

Two repetitions alternate execution order. The same queue models first predict
whether each route can avoid overflow under the bounded fluid arrival rate.
The process experiment measures the difference introduced by discrete jobs,
scheduling and messaging. It builds on the [Soul example](../../docs/workloads/local-soul-searching.md)
with a controlled two-consumer experiment rather than changing that demo's protocol.

## Run

Use the [local suite refresh commands](../symbiosis/README.md#run), selecting
`--study reachability-routing` for this study phase. Its execution code lives in
`polyad_benchmarks.routing_study`; recipe changes are fingerprinted at preparation.
This experiment itself uses the standard-library analytic guard and real processes;
the other suite members add optional HJ comparisons.

## Read the evidence

The [recorded initial results](results.json) completed 60 of 60 jobs in both
guarded trials, with no rejections. The first-consumer trials completed 38 and
39 jobs, rejecting 22 and 21 at the queue limit. All process trees joined.

Each record includes the predicted result, exact envelope artifact, route counts,
actual completions and rejections, maximum outstanding work per consumer, elapsed
time, mean completion latency, guard assessments and `allJoined` cleanup evidence.
The invariant is `offered = completed + rejected`, with every accepted work ID
accounted for exactly once in this local experiment.

The baseline's refusal to accept excess work demonstrates bounded admission.
Compare its rejected count and latency with the admitted split. A guard can also
reject work when scheduling delays consume modeled headroom; keep those outcomes
visible. Report disagreement between the fluid assumptions and process behavior.
This study measures local runtime behavior, not Kubernetes deployment latency or
a durable distributed delivery protocol.
