# Service-level objectives for adaptive Daemons

<!-- toc:start -->
**Table of contents**

- [Declare the contract](#declare-the-contract)
- [Report observations from the SDK](#report-observations-from-the-sdk)
- [Interpret status](#interpret-status)
<!-- toc:end -->

Polyad evaluates customer-visible service quality independently from deployment
and adaptation progress. A Daemon can therefore be `Progressing` while its
service is `Compliant`, `Degraded` or `Unavailable`. An adaptation is not counted
as downtime unless the application reports loss of its minimum service contract.

## Declare the contract

Configure the minimum contract on the reusable Daemon definition:

```yaml
apiVersion: polyad.astrivant.com/v1alpha1
kind: Daemon
metadata:
  name: consumer
spec:
  serviceLevel:
    requiredCapabilities: [consume]
    availability: 0.999
    latencyP99Seconds: 0.75
    minimumThroughputPerSecond: 30
    windowSeconds: 2592000
    sampleMaxAgeSeconds: 60
    adaptation:
      maximumDurationSeconds: 60
      maximumUnavailableSeconds: 5
      maximumFailedAttempts: 2
  template:
    # ...
```

`availability` is a ratio from zero through one. Accounting uses bounded fixed
windows, not an unbounded event history. Generation changes and expired windows
start a new period. Reports must be strictly time-ordered and represent
non-overlapping application measurement windows.

## Report observations from the SDK

Applications report external outcomes rather than inferring them from Pod
health. Counters use one application-defined eligible-request population.

```python
from datetime import UTC, datetime

from polyad_sdk import ServiceLevelReport

report = ServiceLevelReport(
    graph="pipeline",
    graphUid="...",
    target="consumer",
    targetUid="...",
    targetGeneration=3,
    node="source",
    observedAt=datetime.now(UTC).isoformat(),
    durationSeconds=10,
    serving=True,
    capabilities=("consume",),
    eligibleRequests=1000,
    successfulRequests=999,
    requestsWithinLatencyObjective=995,
    latencyP99Seconds=0.42,
    completedPerSecond=54,
)
service.report_service_level(report)
```

The SDK checks the report against its projected graph and Daemon identities.
The operator repeats those UID and generation checks, authorizes the containing
graph, rejects stale or replayed observations, and evaluates the Daemon-owned
objectives.

## Interpret status

`status.serviceLevel` includes the current state, violations, the latest sample,
window counters, availability, latency compliance and remaining availability
error-budget fraction. `sampleDeadline` makes report freshness explicit;
`polyad_service_level_sample_fresh` becomes zero after that deadline without
rewriting the last known classification. Its states are:

- `Compliant`: the latest service and accumulated availability meet the contract.
- `Degraded`: the endpoint serves its required capabilities but violates a
  latency, throughput, availability or adaptation budget.
- `Unavailable`: the endpoint is not serving or a required capability is absent.

`status.adaptation.statistics` separately records attempted, successful and
failed strategy invocations plus their total duration for the current Daemon
generation. Active transitions are checked against `maximumDurationSeconds`.
Argo CD and Flux report a current-generation `Degraded` or `Unavailable` service
as failed even while an adaptation invocation is active; a compliant active
adaptation remains progressing.

These measurements describe application outcomes. Kubernetes readiness, VPA
bounds and container usage remain supporting evidence and must not be substituted
for successful requests or capability availability.

For the exact counter fold, formulas, state precedence and worked examples, see
the [service-level computation reference](service-level-computations.md). For
controller ownership, feedback loops and operational tradeoffs, see
[resilience control loops](../operations/resilience-control-loops.md).
