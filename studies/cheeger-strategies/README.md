# Cheeger strategy selection under graph change

<!-- toc:start -->
**Table of contents**

- [Questions and figures](#questions-and-figures)
- [Findings from the published local run](#findings-from-the-published-local-run)
- [Published graphs](#published-graphs)
- [What accuracy means](#what-accuracy-means)
- [Experimental axes](#experimental-axes)
- [Churn and cache semantics](#churn-and-cache-semantics)
- [Current runtime limits exposed by the study](#current-runtime-limits-exposed-by-the-study)
- [Reproduce](#reproduce)
<!-- toc:end -->

This local study runs Polyad's production Cheeger selector on reproducible graph
snapshots. It compares exact enumeration, adjacency PCA, fresh Laplacian spectral
reduction, reused spectral partitions, and the complete automatic selector.
The archived [PCA reduction study](../cheeger-reduction-deprecated/README.md) keeps its
original dimension and compression experiments; this study adds the production
strategy transitions and administrator controls.

All figures have plain-language titles, descriptions below each subplot title,
and mathematical notation for the quantities plotted. Every PNG has a matching
SVG in [figures](figures/), and [results.json](results.json) retains individual
observations, reconstructable graph snapshots, recipes and source hashes.

## Questions and figures

| Figure | Question | Controlled variables |
| --- | --- | --- |
| [Churn](figures/churn.svg) | How accurate and expensive is each strategy as edges change? | Same snapshot, policy, dimensions and quotient size for all methods |
| [Activation](figures/activation.svg) | Which tier settles a minimum, maximum or two-sided policy? | Fixed graph family, seed and cache gate; vary churn and threshold |
| [Parameters](figures/parameters.svg) | How do dimensions, supernodes, graph size and density affect the result? | One plotted axis at a time; unplotted settings fixed |
| [Cache](figures/cache.svg) | How do churn gates and competing graph boundaries affect reuse? | Same policy grid for each churn gate; four distinct boundaries for capacity |
| [Timeline](figures/timeline.svg) | What happens during steady periods, churn, policy changes, joins and departures? | One persistent cache and an ordered event sequence |
| [Controls](figures/controls.svg) | Which budgets resolve the policy, and are decisive answers correct? | Same difficult minimum for work-limit probes |

## Findings from the published local run

The run contains **6,969 measurements across 101 distinct named graph snapshots**,
including 1,800 threshold-and-churn-gate trials. Repeated timings and isomorphic
named boundaries are not independent topologies. All sampled certificates
contained the exact reference; all 4,308 decisive answers agreed with it. The
remaining 2,661 observations were explicitly unresolved, mainly reduced-method
comparators that could not certify the requested minimum.

- **Reuse degrades under churn.** At 60% requested edge replacement, the pooled
  median cached-cut relative error was 91.7%, versus 41.7% for PCA and 7.1% for
  fresh spectral reduction. These are pooled medians, not worst-case bounds;
  the raw records retain the full spread.
- **Thresholds decide whether the cheap tiers are useful.** Across the threshold
  grid, cached cuts settled 538 calls, fresh reduction settled 666, and exact
  search finished 596. These are experimental-grid counts, not expected
  production frequencies.
- **The selector is not always faster than exact-only.** In the unchanged-graph
  comparison, the minimum policy still required exact enumeration in 33 of 36
  calls. Trying both reduced tiers first added overhead. A good upper witness
  alone cannot prove a minimum; its lower bound must also reach the threshold.
- **More dimensions need not improve clustering.** With six supernodes on the
  community cases, eight spectral dimensions worsened median cut accuracy
  compared with one, two or four. More supernodes improved these sampled cuts,
  but increased quotient-search cost exponentially.
- **Capacity matters independently of churn.** Cycling through four boundaries
  produced no warm-round cache hits with one or two entries, and all hits with
  four or eight entries, under the deliberately permissive maximum policy.
- **The current budget is not end-to-end.** A one-cut allowance still performed
  63 cuts: 31 cached, 31 fresh and one exact. See the limitations below before
  treating `maxCuts` or the cooperative timeout as a hard work ceiling.

These findings support threshold-aware escalation, not a blanket claim that
reduction is faster or that one churn setting is universally safe. Timings are
from one ARM64 macOS host; the recorded runtime section identifies the software
versions and numerical-library thread controls.

## Published graphs

![Accuracy and runtime under churn](figures/churn.png)

![Measured tier activation](figures/activation.png)

![Controlled parameter sweeps](figures/parameters.png)

![Cache gates and capacity](figures/cache.png)

![Strategy transitions over time](figures/timeline.png)

![Budget behavior and decision correctness](figures/controls.png)

## What accuracy means

The study uses Polyad's simple, undirected, unweighted projection and measures
unnormalized edge expansion:

$$h(G)=\min_{\varnothing\ne S\subsetneq V}\frac{|\partial S|}{\min(|S|,|V\setminus S|)}.$$

An independent Gray-code exhaustive implementation supplies the reference for
each graph snapshot. The production exact solver is separately timed and its
answer checked against that reference. Every reduced candidate cut is lifted
to the original graph. Its ratio is an upper bound $U$; the combinatorial
Laplacian supplies $L=\lambda_2/2$. The cached spectral lower bound is reused
only for an identical edge set; after an edge change it becomes zero until fresh
spectral work runs.

We distinguish three quantities:

- Cut error: $(U-h)/h$, measured using the independent reference.
- Certificate uncertainty: $U-L$, available without exact enumeration.
- Decision correctness: whether a decisive policy answer agrees with the oracle.
  An unresolved interval is explicitly `Unknown`, never counted as a correct pass.

A nonzero cut error can still give a correct, certified policy decision. The
selector stops when $U<\theta_{min}$ proves a violation, $L>\theta_{max}$ proves
a violation, or the whole interval fits the requested bounds. It escalates when
the interval does not settle them. Inclusive comparisons use the production
tolerance. Publication fails if any sampled interval excludes its reference or
any decisive answer disagrees with the reference. These checks validate the
sampled graphs; they do not constitute a proof about all graphs or all numerical
conditions.

Laplacian clustering itself is a heuristic partitioning method. The assurance
comes from rescoring original-graph cuts and the spectral lower bound, not from
a claimed spectral-preservation theorem for the clustering. PCA remains a
comparison method, and expander decomposition is not implemented in this study.

## Experimental axes

The complete executable recipe is [scenario.json](fixtures/scenario.json).

| Axis | Published recipe |
| --- | --- |
| Graph families | Path, cycle, small-world and two-community |
| Baseline size | 16 vertices |
| Size sweep | 8, 12, 16, 18 and 20 vertices |
| Retained dimensions $d$ | 1, 2, 4 and 8 |
| Quotient supernodes $k$ | 2, 4, 6, 8 and 10 |
| Random-graph edge probability | 0.1, 0.25, 0.5, 0.75 and 0.9 |
| Requested edge replacement | 0%, 5%, 15%, 30% and 60% |
| Cache churn gate | 0, 0.05, 0.15, 0.4 and 1 |
| Threshold relative to exact reference | 0.25, 0.5, 0.75, 1, 1.25, 1.5, 2 and 4 |
| Policy shape | Minimum, maximum and two-sided range |
| Cache capacity | 1, 2, 4 and 8 partitions across four boundary identities |
| Exact-search cut allowance | 1, 7, 31, 127 and 32,767 |
| Cooperative timeout | 1 ms, 10 ms and 1 second |
| Additional controls | Reduction disabled, cache disabled, spectral size cap, boundary size cap and a priority cut |
| Graph seeds / timing repeats | Three seeds; three repeats for matched method comparisons |

Dimension and quotient settings are crossed in the raw data. The parameter plots
hold one fixed when showing the other's effect. Density plots use random graphs
with a connecting edge added between disconnected components; achieved density
and those actual edges are retained. The graph-size plot holds $d=4$, $k=6$ and
the community family fixed. The churn figure pools graph families to show
variability; it must not be read as a topology-specific accuracy guarantee.

The activation grid sets thresholds relative to an already enumerated reference
to probe both sides of each decision boundary. That is an experimental technique;
the production selector never receives the reference. Each threshold call starts
with the same baseline cache state. The timeline and cache-capacity sweep instead
preserve cache history across calls. Their distinction matters when interpreting
how often a refresh occurs.

For the timeline, `baselineId` and the top-level `edgeChurn` describe the previous
event, not necessarily the older snapshot that created a cached partition.
Each recorded reducer attempt separately retains its returned certificate's
`edgeChurn`; a cache miss has no certificate and reports null. Fresh reduction is
never labeled a cache hit.

## Churn and cache semantics

The production gate uses Jaccard edge distance:

$$\rho_E=\frac{|E_0\triangle E_t|}{|E_0\cup E_t|}.$$

Replacing a fraction $r$ of edges is not the same quantity. For feasible disjoint
replacements at fixed edge count, $\rho_E=2r/(1+r)$. Finite edge counts round the
requested number of changes, and a saturated graph may permit fewer. The study
adds a replacement edge before removing an original edge, so trees can also
change while remaining connected. Every record contains both the request and
the achieved distance.

The raw cached comparator always rescores the baseline partition on current
edges, even if it would exceed a deployment's churn gate. This measures whether
that partition remains useful. The **Selector** line enforces the configured
gate and reflects what the actual runtime would do. Cached and fresh measurements
are never substituted for one another.

Cache priming and the independent oracle are outside the comparison timing.
Fresh reduction includes eigendecomposition, clustering and quotient search;
selector timing includes every attempted tier and lightweight study tracing.
The cold start in the timeline includes its initial reduction. The cache-capacity
chart excludes the first filling round. Method order is deterministically shuffled
between repeats to reduce ordering bias. Timing bands show the middle 50% of
measurements, not confidence intervals; repeated deterministic graphs add timing
samples, not independent evidence of accuracy.

## Current runtime limits exposed by the study

The cut-budget plot counts all evaluated quotient cuts plus exact-search work.
The current production `maxCuts` allowance limits the exact search, while cached
and fresh quotient work is additional. Likewise, the cooperative deadline does
not interrupt dense eigendecomposition or quotient loops. These probes make
budget overshoot visible rather than silently counting only the final tier.
Keep quotient sizes bounded; a 64-supernode search is still exponential.

Dense spectral work is capped separately by `reduction.maxVertices`. The overall
boundary vertex cap is checked first. Without a policy threshold, numeric
measurement callers continue to request exact values; this study's selector
experiments use bounded GraphRule-style decisions. It does not claim that Soul's
throughput measurements or SLA improve merely because a structural computation
becomes cheaper. Nested PolyGraph boundaries are evaluated independently; no
global Cheeger bound is inferred by adding child results.

Results are local algorithm measurements on small graphs that can be enumerated
exactly. They are not Kubernetes benchmarks or cloud SLA forecasts. Three seeds
and four graph families cannot establish universal churn defaults. Consult the
[configuration guide](../../docs/graphs/cheeger-tuning.md) for administrator gates.

## Reproduce

From this checkout with the root development environment installed:

```sh
export PYTHONPATH="$PWD/pkg/polyad-benchmarks:$PWD"
export MPLCONFIGDIR="$PWD/.cache/matplotlib"
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1

python -m polyad_benchmarks.refresh --ci-phase prepare --suite cheeger \
  --root .cache/benchmarks/cheeger-RUN
python -m polyad_benchmarks.refresh --ci-phase study --study cheeger-strategies \
  --root .cache/benchmarks/cheeger-RUN
python -m polyad_benchmarks.refresh --ci-phase finish --publish \
  --root .cache/benchmarks/cheeger-RUN
```

Choose a fresh run directory. The selector experiment requires the operator's
`polyad` package or checkout as well as `polyad-benchmarks` and Matplotlib. It
uses no Kubernetes API, cloud account or service process. The prepare/finish
protocol hashes the production graph solver along with study code and inputs,
then verifies all twelve figure artifacts before publication. Runtime versions
and configured numerical-library thread limits are recorded with the results.
