# Graph concepts

<!-- toc:start -->
**Table of contents**

- [Conditions and admission](#conditions-and-admission)
- [Graphs of graphs](#graphs-of-graphs)
- [Constrained compositions](#constrained-compositions)
- [Network boundaries](#network-boundaries)
- [Graphs across node groups](#graphs-across-node-groups)
- [Finite pipelines](#finite-pipelines)
- [Persistent services and recurrence](#persistent-services-and-recurrence)
<!-- toc:end -->

[Documentation](../README.md) · [Visual overview](../../README.md#what-polyad-abstracts)

A **graph** describes a system as nodes and the relationships between them.
For example, a data pipeline might contain three tasks: fetch records, clean
them, and publish the results. Each task is a node; dependencies describe which
tasks must finish before others can start.

In Polyad, a node can represent:

- A **workload**: a task expected to finish, such as processing a file.<sup>[\[1\]](../deployment/operator.md#api-and-python-abstractions)</sup>
- A **daemon**: a service expected to keep running, such as an event consumer.<sup>[\[2\]](../deployment/operator.md#daemons-change-the-graphs-contract)</sup>
- A **resource**: something the work needs, such as configuration or storage.<sup>[\[1\]](../deployment/operator.md#api-and-python-abstractions)</sup><sup>[\[3\]](../deployment/operator.md#workload-persistence)</sup>
- Another **graph**: a group of related nodes that forms part of a larger system.<sup>[\[4\]](../deployment/operator.md#composing-graph-types-with-polygraph)</sup>

These graph nodes describe parts of an application. Kubernetes also uses the
word *node* for a worker machine; those machines provide the capacity on which
the application runs.

Relationships express different things. A **dependency** controls when work can
start: a consumer might wait for a service to become ready, while a report waits
for processing to finish. A **connection** describes data flowing between nodes,
including flows that return to an earlier node.<sup>[\[2\]](../deployment/operator.md#daemons-change-the-graphs-contract)</sup>
A **gate** adds a
condition or delay before work starts.<sup>[\[1\]](../deployment/operator.md#api-and-python-abstractions)</sup><sup>[\[5\]](../deployment/operator.md#delay-gates)</sup>

Grouping nodes into a graph gives you one place to describe where that work
should run and observe its progress. This is the graph's **scheduling boundary**:
its placement rules can cover nested graphs, and its status summarizes the work
inside it.<sup>[\[6\]](../deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[7\]](../deployment/operator.md#graph-instance-status)</sup>
Polyad also tracks the resources it creates
for that graph through their cleanup.<sup>[\[8\]](../deployment/operator.md#reconciliation-and-shutdown)</sup>

[**Service Symbiosis**](../workloads/adaptive-microservices.md) describes how the
services inside these graphs cooperate: discover compatible peers, react to
relationship and capacity changes, share work within admission budgets and
report useful completion. The [Python SDK](../../pkg/polyad-sdk/README.md)
provides the observation and control interface for this application model.

## Conditions and admission

A **predicate** is a condition with a true-or-false answer, such as “has the
validation task completed?” A Boolean [Gate](../deployment/operator.md#api-and-python-abstractions)
can combine conditions: for example, start publication only when validation has
completed **and** the storage service is ready. Missing observations can leave
the answer unknown; an unknown answer is not permission to start work.

**Admission** is Polyad's decision that a particular piece of work may start.
A true gate condition is one check in that decision. Dependencies, placement,
available execution slots, capacity and applicable [GraphPolicies](../graphs/graph-policies.md)
must also allow it. With [activation](../workloads/activation.md), the node also
needs an explicit request to start; becoming ready or satisfying a condition
does not itself supply that request.

## Graphs of graphs

A larger application often contains several smaller workflows. For example,
data preparation and result publication can each be a graph within a processing
application. A graph inside another graph is a **subgraph**; the outermost graph
is the **root**.

`PolyGraph` represents a graph whose nodes are themselves graphs. It can combine
ordinary `Graph` workflows, scalable `ReplicaGroup` boundaries, and other PolyGraphs. A reusable graph definition
acts as a blueprint; each reference creates a separate instance of it.<sup>[\[4\]](../deployment/operator.md#composing-graph-types-with-polygraph)</sup>

A Graph's execution stays within one Kubernetes cluster. PolyGraphs can
optionally compose across registered clusters and nest to represent higher
levels. Destination operators retain execution authority; optional shared
observers provide read-only snapshots. See [multicluster configuration](../deployment/multicluster.md).

The [graphs-of-graphs diagram](../../README.md#graphs-of-graphs) shows ownership
across three clusters. Progress summaries return from each child to its parent,
until the root has a view of the application as a whole. The
[execution and observation diagram](../deployment/multicluster.md#execution-and-observation)
shows the operators responsible for those boundaries.

The root reports its own graph shape and a recursive summary of descendant
work. Missing or stale child observations make that summary explicitly
incomplete.<sup>[\[4\]](../deployment/operator.md#composing-graph-types-with-polygraph)</sup>

## Constrained compositions

**Composition** means assembling a larger graph from reusable definitions. In
the [composition diagram](../../README.md#constrained-compositions), two nodes use
the same graph definition to create separate instances, each with its own workload. IDs identify both the definitions and
their uses, so a request can be traced to the Kubernetes resources it creates.<sup>[\[9\]](../apis/composition-requests.md#durability-ordering-and-audit)</sup>

A **GraphPolicy** describes which graph structures an engineer will allow users to
schedule. Rules can limit size, nesting, or branching, require a shape such as a
tree, or constrain the graph's spectrum: the eigenvalues of a matrix representing
its connections. Namespace-wide rules apply to every graph in that namespace;
graphs can also reference additional rules. Recursive size limits count each
subgraph instance, including repeated uses of the same definition.<sup>[\[10\]](../graphs/graph-policies.md#structural-limits)</sup>

The API's immutable `APIBuilder` configures authenticated composition services<sup>[\[11\]](../apis/composition-api.md#enable-the-service)</sup>
and exposes their OpenAPI schema at `/openapi.json`.<sup>[\[12\]](../apis/composition-api.md#openapi-schema)</sup>

## Network boundaries

A subgraph can represent a group of workloads that should communicate internally
but expose only selected connections to other groups. **Rule selection** chooses
which graphs follow a policy; **scope** chooses whether it applies at that boundary
or throughout its subtree. Transport rules can select peers across namespaces;
Istio adds HTTP method/path and service-identity checks.<sup>[\[13\]](../deployment/networking.md#selection-scope-and-inheritance)</sup><sup>[\[14\]](../deployment/networking.md#cross-namespace-peers-and-http-authorization)</sup>

The operator also offers a separate, authenticated event subscription service for
following graph progress across replicas.<sup>[\[15\]](../deployment/networking.md#event-subscriptions)</sup>

## Graphs across node groups

A **node group** is a set of Kubernetes worker machines with shared
characteristics, such as general-purpose CPUs or accelerators. **Placement**
describes which machines are suitable for a workload or a whole graph, using
labels and other Kubernetes scheduling constraints.<sup>[\[6\]](../deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>

In the [node-group diagram](../../README.md#graphs-across-node-groups), two graphs
use general compute and a third uses accelerated compute.
Polyad's operator replicas run on a separate group and coordinate through a
shared cache.<sup>[\[16\]](../deployment/operator.md#replicas-shared-queues-and-autoscaling)</sup>
Graph placement selects machines for the workloads;
Helm's `operator.nodeSelector` and `operator.tolerations` configure placement for the operator itself.<sup>[\[17\]](../../charts/polyad/README.md#operator-and-shared-queue-parameters)</sup>

Placement is enforced by default. Set `spec.placement.enforce: false` to let a
more specific workload or subgraph replace those defaults. Enforced ancestors
remain binding. Tolerations are copied into pod templates; they permit matching
taints and do not guarantee capacity or placement by themselves.<sup>[\[6\]](../deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>

Workloads using `persistence.enabled: true` must specify `storageClass` and
`claimName`. Users choose capacity compatible with storage and recovery needs.
Graphs leave storage policy to their users, including on interruptible capacity.<sup>[\[3\]](../deployment/operator.md#workload-persistence)</sup><sup>[\[18\]](../deployment/operator.md#interruptible-execution)</sup>

## Finite pipelines

A **finite pipeline** is a workflow with an intended end. The
[pipeline diagram](../../README.md#finite-pipelines) prepares data, processes two partitions, then merges and reports the results. Dependencies
express the order, while the two partition tasks can run in parallel.

The partition tasks form a regular `Graph` with explicit spot placement.
Their implementations must tolerate interruption and restart.<sup>[\[18\]](../deployment/operator.md#interruptible-execution)</sup>
A separate subgraph
groups the tasks that publish the results.

Solid arrows show what must happen before the next task can start. A completed
finite graph still owns its resources until deletion or a topology change
requires cleanup.<sup>[\[8\]](../deployment/operator.md#reconciliation-and-shutdown)</sup>
See the [finite example](../../examples/finite.yaml).

## Persistent services and recurrence

A **persistent graph** describes a system intended to keep operating. Its daemons
are long-running services: an ingestion service and a processing service might
exchange events and acknowledgements continuously. Their readiness tells you
whether the system can serve work; there need not be a completion point.<sup>[\[2\]](../deployment/operator.md#daemons-change-the-graphs-contract)</sup>

Activation pulses can instantiate a finite Graph repeatedly inside a persistent
graph. Queue mode serializes requests; parallel mode allows bounded overlapping
runs. A producer or timer supplies the pulses, and the application owns iteration
limits, termination and shared state.<sup>[\[19\]](../deployment/operator.md#repeated-execution)</sup>

Data can circulate between running services. Startup dependencies must still
allow something to start first, so they cannot form a cycle in which every node
waits for another.<sup>[\[2\]](../deployment/operator.md#daemons-change-the-graphs-contract)</sup>

In the [services and recurrence diagram](../../README.md#persistent-services-and-recurrence),
solid arrows show admission or execution progression; dashed arrows show data flow
or recurrence. On Kubernetes, workloads run as Jobs and daemons as Deployments or StatefulSets.<sup>[\[1\]](../deployment/operator.md#api-and-python-abstractions)</sup>
Deletion waits for owned resources and their finalizers, which keep resources
present while cleanup is pending.<sup>[\[8\]](../deployment/operator.md#reconciliation-and-shutdown)</sup>
