# Polyad

Polyad (named after [*polyads*](https://en.wikipedia.org/wiki/Polyad_%28mathematics%29)) is a Kubernetes operator for deploying, connecting and scaling applications as graphs.
Compose batch jobs, persistent services and supporting resources into reusable
Graphs and PolyGraphs. Define how work starts, how components communicate and
which structural constraints must hold as the application changes—from a workflow
in one cluster to a hierarchy spanning multiple clusters.<sup>[\[2\]](docs/deployment/operator.md#api-and-python-abstractions)</sup>

**[Get started](docs/introduction/getting-started.md)** · **[Documentation](docs/README.md)** · **[Helm chart](charts/polyad/README.md)**

- **Compose workloads and resources.** Run finite pipelines and persistent services
  using Jobs, Deployments, StatefulSets or DaemonSets, with dependencies, activation policies,
  [storage](docs/workloads/workload-storage.md) and [placement](#graphs-across-node-groups).
- **Scale within graph constraints.** [KEDA and ReplicaGroups](docs/graphs/replication.md)
  scale individual services, whole graphs or nested compositions. Polyad refreshes
  live graph state and enforces [GraphRules](docs/graphs/graph-rules.md), including size,
  shape and structural Cheeger bounds, before applying scaling changes.
  Optional [throughput feedback](docs/graphs/throughput-feedback.md) maps application demand
  to separate Cheeger targets and recommends or applies approved connection layouts.
- **Make connectivity explicit.** Choose replica connection patterns, including
  custom edges, and enforce [network boundaries](docs/deployment/networking.md) with optional
  NetworkPolicy and Istio integration.
- **Let services participate.** Through the [standalone Python client](pkg/client/README.md),
  workloads can submit compositions, activate work, request [TTL-bound connections](docs/apis/temporary-connections.md)
  and discover neighbors through [topology events](docs/workloads/workload-events.md).
  [Shared types](pkg/polyad-types/README.md) are also available separately from the operator.
- **Coordinate across clusters.** A [root operator](docs/deployment/root-control-plane.md) can
  run in a dedicated management cluster, deploy graphs and execution replicas into
  registered workload clusters, and collect their observations centrally.
- **Choose the control-plane layout.** Select a [single-replica or HA Helm profile](docs/deployment/deployment-profiles.md).
  HA can run dense operators or
  [separate gateway, executor and telemetry components](docs/deployment/components.md)
  managed through the operator's own Graph. Optionally persist graph state and
  tracked measurements in [PostgreSQL](docs/deployment/postgresql.md).

## Table of contents

- [Polyad](#polyad)
  - [Table of contents](#table-of-contents)
  - [Get started](#get-started)
  - [What Polyad abstracts](#what-polyad-abstracts)
    - [Motivation](#motivation)
    - [Graphs of graphs](#graphs-of-graphs)
    - [Replica connections](#replica-connections)
    - [Autoscaling the hierarchy](#autoscaling-the-hierarchy)
    - [Constrained compositions](#constrained-compositions)
    - [Network boundaries](#network-boundaries)
    - [Graphs across clusters](#graphs-across-clusters)
    - [Graphs across node groups](#graphs-across-node-groups)
    - [Workloads calling the operator](#workloads-calling-the-operator)
    - [Finite pipelines](#finite-pipelines)
    - [Persistent services and recurrence](#persistent-services-and-recurrence)
    - [The operator as a Graph](#the-operator-as-a-graph)
  - [What Polyad is not](#what-polyad-is-not)
  - [License](#license)
  - [References](#references)

## Get started

Deploy the [Kubernetes operator](docs/introduction/getting-started.md#quick-start-kubernetes)
with the Helm chart, or try the [local Python scheduler](docs/introduction/getting-started.md#quick-start-local-work).
The [examples](docs/introduction/getting-started.md#examples) cover pipelines, services,
spot work, storage and nested graphs.

Applications can install the [Python client](pkg/client/README.md) or just the
[shared types](pkg/polyad-types/README.md) without installing the operator. From a
checkout, use `pip install ./pkg/polyad-types`; the standalone distribution is
named `polyad-types` and exposes `polyad_types`.

Explore [graph concepts](docs/introduction/concepts.md), [graph rules](docs/graphs/graph-rules.md),
[composition requests](docs/apis/composition-requests.md), the [composition API](docs/apis/composition-api.md),
[networking and event subscriptions](docs/deployment/networking.md),
[temporary connections](docs/apis/temporary-connections.md),
[API keys and shared request lanes](docs/operations/api-keys.md), or the
[development guide](docs/development/toolchain.md). See the [documentation index](docs/README.md)
for lifecycle, status, health and configuration references.

## What Polyad abstracts

A **graph** groups related work and describes how its parts depend on each other.
Its **nodes** can be tasks, services, resources or other graphs. A data pipeline
might fetch records, process partitions in parallel, then publish the results;
a service graph might keep consumers and their supporting resources running.<sup>[\[3\]](docs/introduction/concepts.md)</sup>

### Motivation

Deploying a distributed application means deciding how its services connect,
which work can run together, and how those relationships should change as demand
grows. Polyad makes that topology an explicit, reusable part of the deployment,
with [rules](docs/graphs/graph-rules.md) that constrain its size, structure and permitted
connections across nested graphs.

This lets teams scale individual services, complete pipelines or compositions of
graphs while preserving the application's topology requirements.
[KEDA requests replica counts](docs/graphs/replication.md#connect-keda) through
ReplicaGroups; Polyad recomputes the live graph family's measurements and checks
applicable constraints before creating or retiring copies. A scaling decision
must fit the surrounding application's rules as well as the group's own limits.

For data pipelines, [Cheeger bounds](docs/graphs/graph-rules.md#cheeger-bottleneck-bounds)
provide a structural assurance against bottlenecks: a minimum requires enough
edges across every split relative to the size of its smaller side, at each
configured boundary. This supports throughput goals by rejecting topologies with
overly sparse connections between stages or replicas. Actual throughput still
depends on processing capacity, bandwidth and workload; the Cheeger measurement
counts connections and does not guarantee a data rate.

[Application throughput feedback](docs/graphs/throughput-feedback.md) connects those
structural measurements to workload-specific demand tiers. Keep hard GraphRules
bounds separate from calibrated targets, use Observe mode to inspect recommendations,
and opt into Adapt for bounded changes with stabilization and cooldowns.
See [comparing Cheeger policies](docs/graphs/cheeger-orchestration.md) for diagrams of
their different orchestration decisions and interaction with replica scaling.

Services can also participate in changing their own topology. By installing the
[Python client](pkg/client/README.md), applications can submit
[compositions](docs/apis/composition-requests.md), activate work and request
[temporary connections](docs/apis/temporary-connections.md) with a bounded lifetime
through enabled, authorized APIs. Polyad checks requested changes against the
applicable rules and removes temporary connection grants after expiry.
[Topology events](docs/workloads/workload-events.md) let workloads discover their current
neighbors as connections and replica membership change. This gives applications
room to adapt while keeping deployment constraints under operator control.

### Graphs of graphs

Compose smaller workflows into an application with `PolyGraph`. Each child
reports progress to its parent, giving the root a combined view of the work.<sup>[\[4\]](docs/introduction/concepts.md#graphs-of-graphs)</sup>

In the diagrams below, green marks work and graph summaries, amber marks
constraints or recurrence, and gray marks resources and containing boundaries.

<details open>
<summary>Example: nested graphs reporting to an application root</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart BT
    job["Workload"] --> batch["Graph · batch"]
    daemon["Daemon"] --> service["Graph · service"]
    spot["Graph · spot work"] --> group["PolyGraph · processing"]
    batch --> group
    group --> root["PolyGraph · application"]
    service --> root
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#fff3d6,stroke:#926000,color:#513900
    class job,daemon,batch,service,group,root execution
    class spot constraint
```

Read about [graphs of graphs](docs/introduction/concepts.md#graphs-of-graphs).

</details>

### Replica connections

Each ReplicaGroup can choose its own [connection mode](docs/graphs/replication.md#connections-between-copies).
Here, one subgraph connects whole graph replicas in a [Ring](docs/graphs/replication.md#ring);
another combines daemon replicas in a [Star](docs/graphs/replication.md#star) with
[bidirectional connections](docs/graphs/replication.md#bidirectional-connections) and a
[FullMesh](docs/graphs/replication.md#fullmesh). Edges between enclosing graphs and groups
are declared separately at their [network boundaries](docs/deployment/networking.md#isolating-a-subgraph).

<details open>
<summary>Example: three replica layouts and connections across subgraphs</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    subgraph application["PolyGraph · application"]
        direction TB
        subgraph processing["Graph · processing"]
            subgraph pipelines["pipelines · Ring"]
                direction LR
                p0["replica-0<br/>Graph instance"]
                p1["replica-1<br/>Graph instance"]
                p2["replica-2<br/>Graph instance"]
                p0 --> p1 --> p2 --> p0
            end
        end

        subgraph serving["Graph · serving"]
            direction TB
            subgraph routers["routers · Star"]
                direction LR
                s0["replica-0<br/>Daemon · hub"]
                s1["replica-1<br/>Daemon"]
                s2["replica-2<br/>Daemon"]
                s3["replica-3<br/>Daemon"]
                s0 <--> s1
                s0 <--> s2
                s0 <--> s3
            end

            subgraph caches["caches · FullMesh"]
                direction LR
                m0["replica-0<br/>Daemon"]
                m1["replica-1<br/>Daemon"]
                m2["replica-2<br/>Daemon"]
                m0 <--> m1
                m1 <--> m2
                m2 <--> m0
            end

            routers -->|"TCP 6379"| caches
        end

        processing -->|"TCP 9000"| serving
    end

    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef boundary fill:#f2f4f7,stroke:#667085,color:#344054
    classDef replicas fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    class p0,p1,p2,s0,s1,s2,s3,m0,m1,m2 execution
    class processing,serving boundary
    class pipelines,routers,caches replicas
    style application fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    linkStyle default stroke:#475467,stroke-width:2px
```

The three inner boxes are ReplicaGroups. Their internal edges use these settings:

| ReplicaGroup | Copies | Connection configuration | Internal ports |
| --- | --- | --- | --- |
| `pipelines` | Graph | [Ring](docs/graphs/replication.md#ring) | TCP 8080 |
| `routers` | Daemon | [Star](docs/graphs/replication.md#star), [`bidirectional: true`](docs/graphs/replication.md#bidirectional-connections) | TCP 9000 |
| `caches` | Daemon | [FullMesh](docs/graphs/replication.md#fullmesh) | TCP 6379 |

Arrows show directed data-flow connections; double arrows declare both directions.
The application boundary connects processing to serving on TCP 9000; the serving
boundary connects routers to caches on TCP 6379. These connections
become transport grants when [graph networking](docs/deployment/networking.md) is enabled,
subject to inherited restrictions. Copies without a configured mode remain
[Independent](docs/graphs/replication.md#independent), with no inter-copy edges. Scaling rebuilds the selected pattern and
notifies workloads through [topology events](docs/workloads/workload-events.md).

This example fits within one cluster. For remote Graph workloads, see the
[east-west gateway diagram](docs/deployment/multicluster.md#istio-across-different-networks)
and [direct Pod routing diagram](docs/deployment/multicluster.md#same-network-clusters).
Remote placement and data-flow edges need separately configured traffic policies.

</details>

### Autoscaling the hierarchy

[KEDA can autoscale different levels of the hierarchy](docs/graphs/replication.md#connect-keda)
by targeting a ReplicaGroup's Kubernetes `/scale` subresource. The group's
template determines what each additional replica creates:

- **[Daemon replicas](docs/graphs/replication.md#example-daemon-replicas):** another service instance, backed by its selected Deployment
  or StatefulSet. Set the Daemon's own `replicas: 1` when each group copy should
  represent one desired Pod.
- **[Graph replicas](docs/graphs/replication.md#example-graph-replicas):** another complete workflow or service graph, including its
  workloads, resources and internal connections.
- **[PolyGraph](docs/graphs/replication.md#example-polygraph-replicas) or [nested ReplicaGroup replicas](docs/graphs/replication.md#example-nested-replicagroups):** another composition of graphs or
  replica groups, allowing scaling at multiple levels of the same application.

For example, scale a worker pool inside a processing graph as its queue grows,
and scale copies of the whole processing graph as demand for complete pipelines
grows. Scale one group instance independently, or scale a shared definition to
update every inheriting instance; see [instance and shared scaling](docs/graphs/replication.md#independent-instances-and-all-uses-of-a-definition).

Before creating or retiring copies, Polyad refreshes the owning graph family's
topology and checks replica bounds and applicable GraphRules, including structural
limits and Cheeger constraints. KEDA supplies the requested count;
constraints can block its application. Target the ReplicaGroup to use these
checks: directly autoscaling a generated Deployment or StatefulSet bypasses graph
admission. See [constraints before scaling](docs/graphs/replication.md#constraints-before-scaling).

A ReplicaGroup of PolyGraphs can also scale a complete cross-cluster composition.
Each destination can independently scale its own local groups. The
[multicluster scaling diagram](docs/deployment/multicluster.md#graphrules-cheeger-bounds-and-scaling)
shows where each cluster refreshes live values and enforces its own rules.

### Constrained compositions

Build workflows from reusable definitions and trace each instance to its
Kubernetes resources. `GraphRule` lets engineers constrain what users can
schedule by size, shape, nesting and mathematical properties.<sup>[\[5\]](docs/graphs/graph-rules.md#structural-limits)</sup><sup>[\[6\]](docs/apis/composition-requests.md#durability-ordering-and-audit)</sup>

<details>
<summary>Example: reusable graph definitions with structural constraints</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart LR
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#fff3d6,stroke:#926000,color:#513900
    classDef resource fill:#eeeeee,stroke:#777777,color:#444444

    request["Composition request<br/>IDs and references"]:::resource
    rules["GraphRule<br/>size, shape, spectrum"]:::constraint
    root["PolyGraph root<br/>aggregate status"]:::execution
    request --> root
    rules -. "constrains each boundary" .-> root
    subgraph left["Graph instance: left"]
        a["Workload instance"]:::execution
    end
    subgraph right["Graph instance: right"]
        b["Workload instance"]:::execution
    end
    root --> a
    root --> b
    definition["Shared graph definition"]:::resource
    definition -. "instantiates" .-> a
    definition -. "instantiates" .-> b
```

Read about [composition requests](docs/apis/composition-requests.md) and [GraphRule constraints](docs/graphs/graph-rules.md).

</details>

### Network boundaries

Group workloads into subgraphs with explicit network connections. Scoped rules
control traffic across boundaries and namespaces; optional Istio integration
adds HTTP and service-identity authorization.<sup>[\[7\]](docs/deployment/networking.md#selection-scope-and-inheritance)</sup><sup>[\[8\]](docs/deployment/networking.md#cross-namespace-peers-and-http-authorization)</sup>

<details>
<summary>Example: subgraph connections and cross-namespace authorization</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    rules["GraphRule · subtree scope"] -. inherits .-> group
    subgraph group["PolyGraph · application"]
        direction TB
        subgraph producers["Graph · producers"]
            task["Workload"] --> sender["Daemon"]
        end
        subgraph consumers["Graph · consumers"]
            receiver["Daemon"] --> report["Workload"]
        end
        producers -->|"TCP 8080"| consumers
    end
    external["Service identity · another namespace"] -->|"GET /status"| consumers
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#fff3d6,stroke:#926000,color:#513900
    classDef boundary fill:#f2f4f7,stroke:#667085,color:#344054
    class task,sender,receiver,report execution
    class rules,external constraint
    class group,producers,consumers boundary
```

Read about [network scope and inheritance](docs/deployment/networking.md#selection-scope-and-inheritance) and [cross-namespace authorization](docs/deployment/networking.md#cross-namespace-peers-and-http-authorization).
This diagram shows one cluster; [remote traffic rules](docs/deployment/multicluster.md#remote-traffic-rules)
are configured separately at each end of a cross-cluster connection.

</details>

### Graphs across clusters

Graphs execute within one cluster. PolyGraphs can optionally place child Graphs
and nested PolyGraphs in registered remote clusters, composing regions and higher
levels. Optional shared observers expose read-only graph snapshots while each
cluster's operator enforces its own rules and executes its workloads. See
[cross-cluster placement, Istio transport and observers](docs/deployment/multicluster.md).

A [root operator control plane](docs/deployment/root-control-plane.md) can manage this entire
hierarchy from a separate management cluster. It installs remote execution replicas,
collects their observations centrally and coordinates KEDA through root-local scale
targets. See the [deployment architecture](docs/deployment/root-control-plane.md#authority-and-execution)
and [complete configuration example](examples/root-control-plane/values.yaml).

<details open>
<summary>Example: nested PolyGraphs composing three clusters</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    subgraph east["Cluster east"]
        root["PolyGraph<br/>Global application"]
        region["PolyGraph<br/>East region"]
        batch["Graph · batch<br/>Local Jobs"]
        root --> region --> batch
    end
    subgraph west["Cluster west"]
        group["PolyGraph<br/>Western regions"]
        service["Graph · service<br/>Local Deployments or StatefulSets"]
        group --> service
    end
    subgraph north["Cluster north"]
        analytics["Graph · analytics<br/>Local Jobs and resources"]
    end
    root -->|"cluster: west"| group
    group -->|"cluster: north"| analytics
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    class root,region,batch,group,service,analytics execution
```

Arrows show declared parent-child ownership; child status rolls back up to the
root. Each Graph's workloads stay inside its cluster. The east operator manages
the western PolyGraph's intent; the west operator manages that PolyGraph's
children, including the Graph in north. Each destination operator executes its
local work. The same hierarchy can be entirely local by omitting `cluster`.

Read about [graphs of graphs](docs/introduction/concepts.md#graphs-of-graphs),
[remote placement and ownership](docs/deployment/multicluster.md#placement-and-ownership),
and the [execution architecture](docs/deployment/multicluster.md#execution-and-observation).
The [two-cluster example](examples/multicluster/application.yaml) demonstrates
local nesting in east with a remote Graph in west; the diagram extends this
pattern with another remote PolyGraph and cluster.

</details>

### Graphs across node groups

Place whole graphs on groups of Kubernetes machines, such as general compute
or accelerators. Here, three graphs share two worker groups while coordinated
operator replicas and their shared Dragonfly cache run on a third.<sup>[\[9\]](docs/deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[10\]](docs/deployment/operator.md#replicas-shared-queues-and-autoscaling)</sup>

<details>
<summary>Example: three graphs across two worker groups</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    subgraph control["Node group · operators"]
        operator["Polyad operator replicas"]
        cache[("Shared Redis / Dragonfly cache")]
    end
    api["Kubernetes API"]
    operator <-->|"queues and coordination"| cache
    operator -->|"ordered resource writes"| api
    subgraph groupA["Node group · general compute"]
        subgraph graphA["Graph · ingest"]
            fetch["Fetch"] --> normalize["Normalize"]
        end
        subgraph graphB["Graph · publish"]
            package["Package"] --> publish["Publish"]
        end
    end
    subgraph groupB["Node group · accelerated compute"]
        subgraph graphC["Graph · processing"]
            compute["Compute"] --> aggregate["Aggregate"]
        end
    end
    api -->|"graph placement"| graphA
    api -->|"graph placement"| graphB
    api -->|"graph placement"| graphC
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#fff3d6,stroke:#926000,color:#513900
    classDef resource fill:#eeeeee,stroke:#777777,color:#444444
    class fetch,normalize,package,publish,compute,aggregate execution
    class operator constraint
    class api,cache resource
    style control fill:#fff3d6,stroke:#926000,color:#513900
    style groupA fill:#eeeeee,stroke:#777777,color:#444444
    style groupB fill:#eeeeee,stroke:#777777,color:#444444
```

Read about [graph placement](docs/deployment/operator.md#scheduling-a-graph-onto-a-resource-slice) and [operator replicas and shared queues](docs/deployment/operator.md#replicas-shared-queues-and-autoscaling).

</details>

### Workloads calling the operator

Running workloads can submit their next graph, read its status and subscribe to
graph events through the operator's optional APIs. Services route requests to
ready replicas; explicitly authorized callers can connect from other
namespaces.<sup>[\[15\]](docs/deployment/networking.md#workload-access-to-operator-apis)</sup>

A running service can also pulse downstream workloads or daemon replica groups,
with explicit concurrency and frequency policies.<sup>[\[17\]](docs/workloads/activation.md)</sup>

With a capacity policy, Polyad forecasts upcoming stages while earlier work
runs, giving a compatible node autoscaler advance notice. Dependencies and gates
still decide when the next stage starts.<sup>[\[16\]](docs/graphs/capacity.md)</sup>

<details>
<summary>Example: API access, event streams and advance capacity requests</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    subgraph local["Workloads · operator namespace"]
        caller["Managed workload"]
    end
    subgraph remote["Workloads · another namespace"]
        subscriber["Authorized workload"]
    end
    access["Explicit network access<br/>Optional Istio identity authorization"]
    composition["Composition Service · 8090<br/>Submit graphs and read status"]
    events["Event Service · 8091<br/>Subscribe to graph observations"]
    subgraph control["Node group · operators"]
        operator["Ready operator replicas<br/>APIs and graph scheduling"]
        cache[("Shared Dragonfly cache<br/>Queues and event history")]
    end
    demand["Kubernetes API<br/>ProvisioningRequest or placeholder Pods"]
    autoscaler["Node autoscaler"]
    machines["Target worker node group<br/>Capacity for upcoming stages"]
    caller -->|"Authenticated requests"| access
    subscriber -->|"Cross-namespace requests"| access
    access --> composition
    access -->|"GET /v1/events"| events
    composition --> operator
    events --> operator
    operator <-->|"Coordination and observations"| cache
    events -. "SSE observations" .-> local
    events -. "SSE observations" .-> remote
    operator -->|"Forecast before next stage"| demand
    demand -->|"Upcoming resource demand"| autoscaler
    autoscaler -->|"Provision nodes when supported"| machines
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#ffe3a3,stroke:#926000,color:#513900
    classDef resource fill:#eeeeee,stroke:#777777,color:#444444
    class caller,subscriber execution
    class operator,access constraint
    class composition,events,cache,demand,autoscaler,machines resource
    style local fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    style remote fill:#e2e6ec,stroke:#667085,stroke-width:2px,color:#344054
    style control fill:#fff3d6,stroke:#926000,stroke-width:2px,color:#513900
    linkStyle default stroke:#475467,stroke-width:2px
```

Read about [workload API access](docs/deployment/networking.md#workload-access-to-operator-apis), [event subscriptions](docs/workloads/workload-events.md), and [advance capacity requests](docs/graphs/capacity.md).

</details>

### Finite pipelines

Express a workflow from preparation to publication, with parallel tasks and
gates that wait for a condition or delay. Ordinary Graph placement can select
spot capacity; applications choose how to handle interruption and storage.<sup>[\[11\]](docs/introduction/concepts.md#finite-pipelines)</sup><sup>[\[12\]](docs/deployment/operator.md#delay-gates)</sup>

<details>
<summary>Example: parallel spot workloads behind an admission gate</summary>

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    subgraph pipeline["Graph · finite pipeline"]
        direction TB
        storage[("Resource · shared storage")]
        prepare["Workload · prepare"]
        gate{"Gate · admission condition"}
        subgraph workers["Graph · spot placement"]
            direction TB
            left["Workload · partition A"]
            right["Workload · partition B"]
        end
        subgraph publish["Graph · publish results"]
            merge["Workload · merge"]
            report["Workload · report"]
            merge -->|completed| report
        end
        storage -->|ready| prepare
        prepare -->|completed| gate
        gate -->|allowed| left
        gate -->|allowed| right
        workers -->|completed| publish
    end
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#ffe3a3,stroke:#926000,color:#513900
    classDef resource fill:#eeeeee,stroke:#777777,color:#444444
    class prepare,left,right,merge,report execution
    class gate constraint
    class storage resource
    style pipeline fill:#e2e6ec,stroke:#667085,stroke-width:2px,color:#344054
    style workers fill:#fff3d6,stroke:#926000,stroke-width:2px,color:#513900
    style publish fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    linkStyle default stroke:#475467,stroke-width:2px
```

Read about [finite pipelines](docs/introduction/concepts.md#finite-pipelines) and [admission and delay gates](docs/deployment/operator.md#delay-gates).

</details>

### Persistent services and recurrence

Keep services running with `Daemon`, and repeat finite Graphs with activation
requests. A producer or timer supplies each pulse; the application owns iteration
limits, stop conditions and durable shared state.<sup>[\[13\]](docs/deployment/operator.md#repeated-execution)</sup>

<details>
<summary>Example: persistent services with repeated graph activations</summary>

Solid arrows show startup or execution progression; dashed arrows show data flow
or a new activation request.

```mermaid
---
config:
  theme: base
  htmlLabels: false
  themeVariables:
    primaryTextColor: "#163b29"
    secondaryTextColor: "#513900"
    tertiaryTextColor: "#344054"
    clusterBkg: "#f2f4f7"
    clusterBorder: "#667085"
    titleColor: "#344054"
    edgeLabelBackground: "#f2f4f7"
    lineColor: "#667085"
  flowchart:
    subGraphTitleMargin:
      top: 8
      bottom: 20
---
flowchart TB
    subgraph system["Graph · persistent service"]
        direction TB
        queue[("Resource · queue service")]
        subgraph service["Graph · persistent processing"]
            direction LR
            ingest["Daemon · ingest"]
            process["Daemon · process"]
            ingest -->|ready| process
            ingest <-.->|"events / acknowledgements"| process
        end
        subgraph activations["Graph · finite activation target"]
            direction LR
            sample["Workload · sample"]
            adjust["Workload · adjust"]
            sample -->|completed| adjust
        end
        next["Producer / timer pulse"]
        activations -->|"completion observed"| next
        next -.->|"activation request"| activations
        queue -->|ready| service
        service -->|ready| activations
    end
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef recurrence fill:#ffe3a3,stroke:#926000,color:#513900
    classDef resource fill:#eeeeee,stroke:#777777,color:#444444
    class ingest,process,sample,adjust execution
    class next recurrence
    class queue resource
    style system fill:#e2e6ec,stroke:#667085,stroke-width:2px,color:#344054
    style service fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    style activations fill:#fff3d6,stroke:#926000,stroke-width:2px,color:#513900
    linkStyle default stroke:#475467,stroke-width:2px
```

Read about [repeated execution](docs/deployment/operator.md#repeated-execution) and [activation pulses](docs/workloads/activation.md).

</details>

The operator can also [request capacity ahead of upcoming stages](docs/graphs/capacity.md),
helping node autoscalers prepare machines while upstream work runs.

Queue pressure and graph hierarchies are available through the optional
[Prometheus and JSON metrics API](docs/operations/metrics.md).

Optional [OpenTelemetry tracing](docs/operations/tracing.md) exports API request,
reconciliation and Kubernetes operation spans to an OTLP/HTTP collector, with
configurable sampling and Secret-backed exporter credentials.
[KEDA can scale services or whole graph compositions](docs/graphs/replication.md) through
`ReplicaGroup`, using workload metrics served by the operator.

[Argo CD](docs/operations/argocd.md) and [Flux health checks](docs/operations/fluxcd.md) report graph and leaf health across nested applications.

### The operator as a Graph

Polyad can run as a compact HA Deployment or manage its own service components
in a Graph. With `architecture.mode: Distributed`, gateway, executor and telemetry
ReplicaGroups scale independently through KEDA and fresh GraphRule checks. A
separate root bootstrap Deployment retains planning and recovery responsibility.

```mermaid
flowchart TB
    root["Root bootstrap Deployment<br/>planning and recovery"]
    subgraph self["Polyad control-plane Graph"]
        direction LR
        gateway["Gateway replicas<br/>APIs and event subscriptions"]
        executor["Executor replicas<br/>graph admission and workloads"]
        telemetry["Telemetry replicas<br/>observations and metrics"]
        gateway --> executor --> telemetry
    end
    root -->|"manage and recover"| self
    subgraph remote["Reserved PolyGraph · remote operators"]
        subgraph west["Graph · west workload cluster"]
            workers["DaemonSet · execution workers<br/>One Pod per eligible node"]
        end
    end
    root -->|"bootstrap and manage"| remote
    workers -->|"root coordination and observations"| root
    rules["GraphRule<br/>Cheeger ≥ 1; recursive size bound"] -. constrains .-> self
    keda["KEDA"] -->|"scrape demand"| telemetry
    keda -->|"request replica counts"| root
    root -. "optional state storage" .-> pg["PostgreSQL<br/>single instance or HA"]
    telemetry -. "persist graph state and parameters" .-> pg
```

The arrows inside the Graph describe logical stages; actual coordination uses
Kubernetes and Dragonfly. Cheeger constrains topology, while queue backlog and
HTTP demand drive capacity decisions. See the
[component and networking diagrams](docs/deployment/components.md#the-operators-own-graph)
and [deployable example](examples/components/values.yaml).

Optional [DaemonSet worker pools](docs/deployment/root-control-plane.md#reserved-graphs-for-node-workers)
extend the reserved topology with a PolyGraph containing a remote Graph and its
DaemonSet. Deployment pools support KEDA replica scaling; DaemonSet capacity follows
node eligibility. Application event streams exclude these operator trees.

[PostgreSQL is optional](docs/deployment/postgresql.md), disabled by default, and stores
graph observations and tracked parameters when enabled. Its optional
[KEDA configuration](examples/postgresql/keda.yaml) scales CloudNativePG instances
from operator connection counts, still scraped from the operator. Additional
database instances provide standby/read capacity; writes continue through the
primary.

## What Polyad is not

Polyad coordinates application graphs alongside existing cluster components.

- **A general-purpose policy engine such as OPA.** `GraphRule` constrains the
  graphs Polyad admits and the resources it compiles. It does not evaluate Rego,
  replace application authorization, or enforce policy on every Kubernetes API
  request. Cluster-wide admission policy remains a separate concern.<sup>[\[18\]](https://www.openpolicyagent.org/docs)</sup><sup>[\[19\]](docs/deployment/operator.md#structural-policy-and-composition-api)</sup>
- **A replacement for the Kubernetes scheduler or node autoscaler.** Polyad
  controls when graph work is admitted and propagates placement constraints.
  Kubernetes places Pods; the configured autoscaler provisions machines.
  Grouping work does not guarantee that every Pod starts together.<sup>[\[9\]](docs/deployment/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[20\]](docs/graphs/capacity.md#scheduling-demand-and-placement)</sup>
- **A service mesh or network transport.** Polyad generates network and Istio
  policy resources. The cluster's networking implementation and mesh enforce
  them; drawing a graph connection does not transport application data.<sup>[\[21\]](docs/deployment/networking.md#enforcement-and-lifecycle)</sup>
- **Automatic process checkpointing or exactly-once execution.** Restarting
  containers with persistent storage requires application recovery logic.
  Workloads must handle retries and duplicate effects; graph ownership and
  ordered API writes do not make application operations transactional.<sup>[\[22\]](docs/deployment/operator.md#workload-persistence)</sup><sup>[\[10\]](docs/deployment/operator.md#replicas-shared-queues-and-autoscaling)</sup>

## License

[GNU General Public License v3.0 only](LICENSE).

## References

Selected background reading for Polyad's architecture and less common design
choices.

- **Infrastructure at scale:** Eduardo Barth, Medium Engineering,
  [Kubernetes Infrastructure At Medium](https://medium.engineering/kubernetes-infrastructure-at-medium-d9e2444932ef)
  (February 14, 2023). A case study covering separate clusters, gradual
  infrastructure rollouts, resource sizing and spare capacity for traffic bursts.
- **Structural bottlenecks:** Shlomo Hoory, Nathan Linial and Avi Wigderson,
  [Expander Graphs and Their Applications](https://www.math.ias.edu/~avi/PUBLICATIONS/MYPAPERS/HLW06/hlw06.pdf#page=14)
  (2006, Section 2.1). The edge-expansion definition used by Polyad's
  [Cheeger bounds](docs/graphs/graph-rules.md#cheeger-bottleneck-bounds), which measure
  graph structure rather than application throughput.
- **Rewriting and composition:** Dimitri Ara et al.,
  [Polygraphs: From Rewriting to Higher Categories](https://arxiv.org/abs/2312.00429).
  Background for the rewriting, confluence and higher-dimensional diagrams in
  the [mutation documentation](docs/development/mutation-diagrams.md).
- **Advance capacity:** The [Cluster Autoscaler ProvisioningRequest FAQ](https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md#how-can-i-use-provisioningrequest-to-run-batch-workloads).
  Reserving capacity before a workload stage becomes runnable; see Polyad's
  [advance-capacity design](docs/graphs/capacity.md).
- **Pending delivery recovery:** Dragonfly's
  [`XAUTOCLAIM`](https://www.dragonflydb.io/docs/command-reference/stream/xautoclaim).
  Recovering unacknowledged stream entries when a queue consumer loses ownership.
- **GitOps health:** [Argo CD custom health checks](https://argo-cd.readthedocs.io/en/stable/operator-manual/health/#custom-health-checks)
  and [Flux health expressions](https://fluxcd.io/flux/components/kustomize/kustomizations/#health-check-expressions).
  Reporting graph and descendant health to deployment controllers.
