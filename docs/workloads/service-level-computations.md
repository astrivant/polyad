# Service-level computation reference

<!-- toc:start -->
**Table of contents**

- [Inputs and trust boundary](#inputs-and-trust-boundary)
- [Fixed-window fold](#fixed-window-fold)
- [Error budget](#error-budget)
- [Objective evaluation and state precedence](#objective-evaluation-and-state-precedence)
- [Adaptation accounting](#adaptation-accounting)
- [Multiple graph nodes and freshness](#multiple-graph-nodes-and-freshness)
- [Worked example](#worked-example)
- [Metrics and GitOps health](#metrics-and-gitops-health)
<!-- toc:end -->

Polyad evaluates a Daemon's application-reported service outcomes separately
from Kubernetes readiness, graph adaptation progress and structural health. This
reference defines the implemented computation. It is intentionally precise
enough to reproduce a `status.serviceLevel` result from accepted reports.

The configured object is an SLO. Whether a violation has contractual or legal
SLA consequences remains an operator and customer concern; Polyad supplies the
measurement and classification, not that interpretation.

## Inputs and trust boundary

`Daemon.spec.serviceLevel` owns the objectives. Each SDK
`ServiceLevelReport` supplies one application measurement interval ending at
`observedAt` and spanning `durationSeconds`. The report must use one consistent
eligible-request population for all three counters:

- `eligibleRequests` (`E`) is every request admitted to SLO accounting.
- `successfulRequests` (`S`) is the subset that completed successfully.
- `requestsWithinLatencyObjective` (`R`) is the subset that met the declared
  latency objective. It need not be a subset of `successfulRequests` in the
  schema, so reporters should enforce that relationship when their SLO requires it.

The operator accepts a report only when its graph and Daemon UID still identify
the same incarnations, its Daemon generation is current, its graph is authorized,
and its timestamp is neither in the future nor older than
`sampleMaxAgeSeconds`. Reports for an instance must advance strictly in time and
their intervals must not overlap. A report is scoped by `graphUid/node`, so two
nodes that reference the same Daemon keep independent accounting periods.

## Fixed-window fold

Polyad stores bounded counters rather than an event history. For one instance,
a new period begins when there is no prior state, the Daemon generation changes,
or the elapsed time from `periodStart` reaches `windowSeconds`. A reset period
starts at `observedAt - durationSeconds`; otherwise the accepted sample is added
to the current counters.

```text
E = sum(eligibleRequests)
S = sum(successfulRequests)
R = sum(requestsWithinLatencyObjective)
T = sum(durationSeconds)
U = sum(unavailableSeconds)

availability       = S / E                  when E > 0
availability       = 1 if serving else 0   when E = 0

latencyCompliance  = R / E                  when E > 0
latencyCompliance  = 1 if serving else 0   when E = 0
```

`T` and `U` are exposed as `observedSeconds` and `unavailableSeconds` for
evidence. They do **not** change request availability. In particular, do not
report time availability through `unavailableSeconds` and expect it to alter
`S / E`; use request counts for the availability objective.

This is a fixed, report-driven period, not a rolling window. A long gap does not
continuously age old samples out. The next accepted report resets the period
when the configured duration has elapsed.

## Error budget

For an availability objective `A_target` and observed availability `A`, Polyad
computes the remaining fraction of the configured failure allowance:

```text
allowed failure ratio = max(10^-12, 1 - A_target)
consumed fraction     = (1 - A) / allowed failure ratio
remaining fraction    = max(0, 1 - consumed fraction)
```

For example, an objective of `0.999` permits a failure ratio of `0.001`. An
observed availability of `0.9995` has consumed half of that allowance, so
`errorBudgetRemaining` is `0.5`. An objective of exactly `1` uses the numerical
floor shown above: any observed failure exhausts the budget.

The value is a fraction of the request-failure allowance, not remaining request
count or remaining downtime. It is also not an automatic adaptation command.

## Objective evaluation and state precedence

Every accepted sample evaluates the following conditions. Inclusive objectives
pass at equality.

| Objective | Violation condition | Evidence scope |
| --- | --- | --- |
| Serving | `serving` is false | Latest sample |
| Required capabilities | Any configured capability is absent | Latest sample |
| Availability | Accumulated `S / E` is below the objective | Current fixed period |
| p99 latency | Value is absent or above the objective when configured | Latest sample |
| Throughput | Value is absent or below the objective when configured | Latest sample |
| Adaptation unavailability | `unavailableSeconds` exceeds the budget while an invocation is active | Latest sample |
| Failed adaptations | Current-generation failure count exceeds the budget | Daemon generation |
| Adaptation duration | Any active invocation is older than the budget at `observedAt` | Active invocation |

The classifier then applies this precedence:

1. `Unavailable` when the latest report says the service is not serving or a
   required capability is absent.
2. `Degraded` when it remains minimally usable but any other objective is
   violated, including an adaptation budget.
3. `Compliant` when there are no violations.

`contractSatisfied` is true only when there are no violations. Consequently an
`Unavailable` result is never contract-satisfied, and a serving endpoint can be
`Degraded` without being unavailable.

## Adaptation accounting

The SDK reports each strategy invocation as `Running`, followed by `Succeeded`
or `Failed`. Starting a new invocation increments `attempts`. A terminal report
removes it from the active set, increments the corresponding counter and adds
the interval from its recorded start to the terminal observation to cumulative
`durationSeconds`. Counters reset with a new Daemon generation.

The service-level evaluator reads that status as follows:

- `maximumDurationSeconds` compares the SLA report's `observedAt` with each
  active invocation's start. It is not the cumulative duration statistic.
- `maximumFailedAttempts` is violated only when failures are greater than the
  configured maximum. A maximum of two therefore permits two failures.
- `maximumUnavailableSeconds` applies only while `adaptation.inProgress` is true
  and compares against this report's unavailable seconds, not the accumulated `U`.

An adaptation by itself does not make service unavailable. Its invocation sets
the Daemon's general `progressing` signal, while customer-visible results still
determine `Compliant`, `Degraded` or `Unavailable`.

## Multiple graph nodes and freshness

Each `graphUid/node` value is evaluated independently. The Daemon-level state is
the worst current instance state using this order:

```text
Compliant < Degraded < Unavailable
```

Daemon-level `contractSatisfied` is the logical AND of every retained instance.
At most 256 instance records may be retained for one Daemon generation. A new
generation discards the old instance map when its first report is accepted.

`sampleDeadline = observedAt + sampleMaxAgeSeconds` records when the latest
sample stops being fresh. Expiry does not rewrite the last classification;
`polyad_service_level_sample_fresh` becomes zero so alerts can distinguish an
old `Compliant` value from current evidence.

## Worked example

Assume availability `0.99`, latency p99 at most `0.5` seconds, throughput at
least `40/s`, and a required `consume` capability.

| Sample | Counters or measurement | Result |
| --- | --- | --- |
| 1 | `E=1,000`, `S=995`, `R=990`, p99 `0.4`, `45/s`, serving `consume` | `Compliant`; availability `0.995`, error budget remaining `0.5` |
| 2 | Adds `E=1,000`, `S=980`, `R=970`, p99 `0.6`, `38/s` | `Degraded`; period availability `0.9875`, plus current latency and throughput violations |
| 3 | Adds no requests, but `serving=false` | `Unavailable`; accumulated request availability remains `0.9875` |

The third result illustrates why reporters must preserve the distinction between
the latest service state and accumulated request ratios.

## Metrics and GitOps health

`polyad_service_level_state` exposes the classification,
`polyad_service_level_sample_fresh` exposes freshness, and
`polyad_service_level` exposes ratios and counters. Adaptation totals are under
`polyad_adaptation`.

Argo CD and Flux use the same precedence: a current-generation `Degraded` or
`Unavailable` service is failed even during adaptation; a compliant active
adaptation is progressing. GitOps health is a projection of these statuses, not
another SLO calculation.

See [declaring and reporting objectives](service-level-objectives.md), the
[interaction and failure-mode guide](../operations/resilience-control-loops.md),
and the [metrics inventory](../operations/metrics.md).
