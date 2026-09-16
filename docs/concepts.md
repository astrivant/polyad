# Graph concepts

[Documentation](README.md) · [Visual overview](../README.md#what-polyad-abstracts)

A **graph** describes a system as nodes and the relationships between them.
For example, a data pipeline might contain three tasks: fetch records, clean
them, and publish the results. Each task is a node; dependencies describe which
tasks must finish before others can start.

In Polyad, a node can represent:

- A **workload**: a task expected to finish, such as processing a file.<sup>[\[1\]](operator.md#api-and-python-abstractions)</sup>
- A **daemon**: a service expected to keep running, such as an event consumer.<sup>[\[2\]](operator.md#daemons-change-the-graphs-contract)</sup>
- A **resource**: something the work needs, such as configuration or storage.<sup>[\[1\]](operator.md#api-and-python-abstractions)</sup><sup>[\[3\]](operator.md#workload-persistence)</sup>
- Another **graph**: a group of related nodes that forms part of a larger system.<sup>[\[4\]](operator.md#composing-graph-types-with-polygraph)</sup>

These graph nodes describe parts of an application. Kubernetes also uses the
word *node* for a worker machine; those machines provide the capacity on which
the application runs.

Relationships express different things. A **dependency** controls when work can
start: a consumer might wait for a service to become ready, while a report waits
for processing to finish. A **connection** describes data flowing between nodes,
including flows that return to an earlier node.<sup>[\[2\]](operator.md#daemons-change-the-graphs-contract)</sup>
A **gate** adds a
condition or delay before work starts.<sup>[\[1\]](operator.md#api-and-python-abstractions)</sup><sup>[\[5\]](operator.md#delay-gates)</sup>

Grouping nodes into a graph gives you one place to describe where that work
should run and observe its progress. This is the graph's **scheduling boundary**:
its placement rules can cover nested graphs, and its status summarizes the work
inside it.<sup>[\[6\]](operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[7\]](operator.md#graph-instance-status)</sup>
Polyad also tracks the resources it creates
for that graph through their cleanup.<sup>[\[8\]](operator.md#reconciliation-and-shutdown)</sup>

## Graphs of graphs

A larger application often contains several smaller workflows. For example,
data preparation and result publication can each be a graph within a processing
application. A graph inside another graph is a **subgraph**; the outermost graph
is the **root**.

`PolyGraph` represents a graph whose nodes are themselves graphs. It can combine
ordinary `Graph` workflows, `EphemeralGraph` workflows for interruptible capacity,
recurring `Feedback` workflows, and other PolyGraphs. A reusable graph definition
acts as a blueprint; each reference creates a separate instance of it.<sup>[\[4\]](operator.md#composing-graph-types-with-polygraph)</sup>

The [graphs-of-graphs diagram](../README.md#graphs-of-graphs) shows progress
summaries passing from each child to its parent, until the root has a view of
the application as a whole.

The root reports its own graph shape and a recursive summary of descendant
work. Missing or stale child observations make that summary explicitly
incomplete.<sup>[\[4\]](operator.md#composing-graph-types-with-polygraph)</sup>

## Constrained compositions

**Composition** means assembling a larger graph from reusable definitions. In
the [composition diagram](../README.md#constrained-compositions), two nodes use
the same graph definition to create separate instances, each with its own workload. IDs identify both the definitions and
their uses, so a request can be traced to the Kubernetes resources it creates.<sup>[\[9\]](composition-requests.md#durability-ordering-and-audit)</sup>

A **GraphRule** describes which graph structures an engineer will allow users to
schedule. Rules can limit size, nesting, or branching, require a shape such as a
tree, or constrain the graph's spectrum—the eigenvalues of a matrix representing
its connections. Namespace-wide rules apply to every graph in that namespace;
graphs can also reference additional rules. Recursive size limits count each
subgraph instance, including repeated uses of the same definition.<sup>[\[10\]](graph-rules.md#structural-limits)</sup>

The API's immutable `APIBuilder` configures authenticated composition services<sup>[\[11\]](composition-api.md#enable-the-service)</sup>
and exposes their OpenAPI schema at `/openapi.json`.<sup>[\[12\]](composition-api.md#openapi-schema)</sup>

## Network boundaries

A subgraph can represent a group of workloads that should communicate internally
but expose only selected connections to other groups. **Rule selection** chooses
which graphs follow a policy; **scope** chooses whether it applies at that boundary
or throughout its subtree. Transport rules can select peers across namespaces;
Istio adds HTTP method/path and service-identity checks.<sup>[\[13\]](networking.md#selection-scope-and-inheritance)</sup><sup>[\[14\]](networking.md#cross-namespace-peers-and-http-authorization)</sup>

The operator also offers a separate, authenticated event subscription service for
following graph progress across replicas.<sup>[\[15\]](networking.md#event-subscriptions)</sup>

## Graphs across node groups

A **node group** is a set of Kubernetes worker machines with shared
characteristics, such as general-purpose CPUs or accelerators. **Placement**
describes which machines are suitable for a workload or a whole graph, using
labels and other Kubernetes scheduling constraints.<sup>[\[6\]](operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>

In the [node-group diagram](../README.md#graphs-across-node-groups), two graphs
use general compute and a third uses accelerated compute.
Polyad's operator replicas run on a separate group and coordinate through a
shared cache.<sup>[\[16\]](operator.md#replicas-shared-queues-and-autoscaling)</sup>
Graph placement selects machines for the workloads;
Helm's `operator.nodeSelector` and `operator.tolerations` configure placement for the operator itself.<sup>[\[17\]](../charts/polyad/README.md#operator-and-shared-queue-parameters)</sup>

Placement is enforced by default. Set `spec.placement.enforce: false` to let a
more specific workload or subgraph replace those defaults. Enforced ancestors
remain binding. Tolerations are copied into pod templates; they permit matching
taints and do not guarantee capacity or placement by themselves.<sup>[\[6\]](operator.md#scheduling-a-graph-onto-a-resource-slice)</sup>

Workloads using `persistence.enabled: true` must specify `storageClass` and
`claimName`. Schedule these on non-spot capacity. Persistent storage and
StorageClass declarations are invalid under `Ephemeral` and `EphemeralGraph`,
including nested graphs.<sup>[\[3\]](operator.md#workload-persistence)</sup><sup>[\[18\]](operator.md#ephemeral-execution)</sup>

## Finite pipelines

A **finite pipeline** is a workflow with an intended end. The
[pipeline diagram](../README.md#finite-pipelines) prepares data, processes two partitions, then merges and reports the results. Dependencies
express the order, while the two partition tasks can run in parallel.

The partition tasks form an `EphemeralGraph`: a group of work designed to tolerate
interruption and restart on capacity such as spot instances.<sup>[\[18\]](operator.md#ephemeral-execution)</sup>
A separate subgraph
groups the tasks that publish the results.

Solid arrows show what must happen before the next task can start. A completed
finite graph still owns its resources until deletion or a topology change
requires cleanup.<sup>[\[8\]](operator.md#reconciliation-and-shutdown)</sup>
See the [finite example](../examples/finite.yaml).

## Persistent services and recurrence

A **persistent graph** describes a system intended to keep operating. Its daemons
are long-running services: an ingestion service and a processing service might
exchange events and acknowledgements continuously. Their readiness tells you
whether the system can serve work; there need not be a completion point.<sup>[\[2\]](operator.md#daemons-change-the-graphs-contract)</sup>

`Feedback` wraps a finite graph so the entire workflow can run repeatedly. Each
execution is an **epoch**. For example, a `sample → adjust` workflow can use new
measurements to update a service's settings on each pass. The application supplies
the decision logic and any state shared between epochs.<sup>[\[19\]](operator.md#feedback-epochs)</sup>

On Kubernetes, epochs run one at a time: the current graph must complete and its
resources finish cleanup before the next starts. `rounds` limits the number of
epochs; omitting it permits indefinite recurrence. `intervalSeconds` sets a
minimum wait after completion, and `suspend: true` drains active work and prevents
another epoch from starting.<sup>[\[19\]](operator.md#feedback-epochs)</sup>

Data can circulate between running services. Startup dependencies must still
allow something to start first, so they cannot form a cycle in which every node
waits for another.<sup>[\[2\]](operator.md#daemons-change-the-graphs-contract)</sup>

In the [services and recurrence diagram](../README.md#persistent-services-and-recurrence),
solid arrows show admission or epoch progression; dashed arrows show data flow
or recurrence. On Kubernetes, workloads run as Jobs and daemons as Deployments.<sup>[\[1\]](operator.md#api-and-python-abstractions)</sup>
Deletion waits for owned resources and their finalizers, which keep resources
present while cleanup is pending.<sup>[\[8\]](operator.md#reconciliation-and-shutdown)</sup>
