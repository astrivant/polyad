# Polyad

Polyad<sup>[\[1\]](https://en.wikipedia.org/wiki/Polyad_%28mathematics%29)</sup> is a graph-based workload scheduler for Kubernetes. Describe tasks,
long-running services and resources as composable graphs; the operator schedules
their work and tracks progress across the application and integrations.<sup>[\[2\]](docs/operator.md#api-and-python-abstractions)</sup>

**[Get started](docs/getting-started.md)** · **[Documentation](docs/README.md)** · **[Helm chart](charts/polyad/README.md)**

## Table of contents

- [Polyad](#polyad)
  - [Table of contents](#table-of-contents)
  - [What Polyad abstracts](#what-polyad-abstracts)
    - [Graphs of graphs](#graphs-of-graphs)
    - [Replica connections](#replica-connections)
    - [Autoscaling the hierarchy](#autoscaling-the-hierarchy)
    - [Constrained compositions](#constrained-compositions)
    - [Network boundaries](#network-boundaries)
    - [Graphs across node groups](#graphs-across-node-groups)
    - [Workloads calling the operator](#workloads-calling-the-operator)
    - [Finite pipelines](#finite-pipelines)
    - [Persistent services and recurrence](#persistent-services-and-recurrence)
  - [What Polyad is not](#what-polyad-is-not)
  - [Get started](#get-started)
  - [License](#license)

## What Polyad abstracts

A **graph** groups related work and describes how its parts depend on each other.
Its **nodes** can be tasks, services, resources or other graphs. A data pipeline
might fetch records, process partitions in parallel, then publish the results;
a service graph might keep consumers and their supporting resources running.<sup>[\[3\]](docs/concepts.md)</sup>

Green marks work and graph summaries, amber marks constraints or recurrence,
and gray marks resources and containing boundaries.

### Graphs of graphs

Compose smaller workflows into an application with `PolyGraph`. Each child
reports progress to its parent, giving the root a combined view of the work.<sup>[\[4\]](docs/concepts.md#graphs-of-graphs)</sup>

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

Read about [graphs of graphs](docs/concepts.md#graphs-of-graphs).

</details>

### Replica connections

Each ReplicaGroup can choose its own [connection mode](docs/replication.md#connections-between-copies).
Here, one subgraph connects whole graph replicas in a [Ring](docs/replication.md#ring);
another combines daemon replicas in a [Star](docs/replication.md#star) with
[bidirectional connections](docs/replication.md#bidirectional-connections) and a
[FullMesh](docs/replication.md#fullmesh). Edges between enclosing graphs and groups
are declared separately at their [network boundaries](docs/networking.md#isolating-a-subgraph).

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
| `pipelines` | Graph | [Ring](docs/replication.md#ring) | TCP 8080 |
| `routers` | Daemon | [Star](docs/replication.md#star), [`bidirectional: true`](docs/replication.md#bidirectional-connections) | TCP 9000 |
| `caches` | Daemon | [FullMesh](docs/replication.md#fullmesh) | TCP 6379 |

Arrows show directed data-flow connections; double arrows declare both directions.
The application boundary connects processing to serving on TCP 9000; the serving
boundary connects routers to caches on TCP 6379. These connections
become transport grants when [graph networking](docs/networking.md) is enabled,
subject to inherited restrictions. Copies without a configured mode remain
[Independent](docs/replication.md#independent), with no inter-copy edges. Scaling rebuilds the selected pattern and
notifies workloads through [topology events](docs/workload-events.md).

</details>

### Autoscaling the hierarchy

[KEDA can autoscale different levels of the hierarchy](docs/replication.md#connect-keda)
by targeting a ReplicaGroup's Kubernetes `/scale` subresource. The group's
template determines what each additional replica creates:

- **[Daemon replicas](docs/replication.md#example-daemon-replicas):** another service instance, backed by its selected Deployment
  or StatefulSet. Set the Daemon's own `replicas: 1` when each group copy should
  represent one desired Pod.
- **[Graph replicas](docs/replication.md#example-graph-replicas):** another complete workflow or service graph, including its
  workloads, resources and internal connections.
- **[PolyGraph](docs/replication.md#example-polygraph-replicas) or [nested ReplicaGroup replicas](docs/replication.md#example-nested-replicagroups):** another composition of graphs or
  replica groups, allowing scaling at multiple levels of the same application.

For example, scale a worker pool inside a processing graph as its queue grows,
and scale copies of the whole processing graph as demand for complete pipelines
grows. Scale one group instance independently, or scale a shared definition to
update every inheriting instance; see [instance and shared scaling](docs/replication.md#independent-instances-and-all-uses-of-a-definition).

Before creating or retiring copies, Polyad refreshes the owning graph family's
topology and checks replica bounds and applicable GraphRules, including structural
limits and Cheeger constraints. KEDA supplies the requested count;
constraints can block its application. Target the ReplicaGroup to use these
checks: directly autoscaling a generated Deployment or StatefulSet bypasses graph
admission. See [constraints before scaling](docs/replication.md#constraints-before-scaling).

### Constrained compositions

Build workflows from reusable definitions and trace each instance to its
Kubernetes resources. `GraphRule` lets engineers constrain what users can
schedule by size, shape, nesting and mathematical properties.<sup>[\[5\]](docs/graph-rules.md#structural-limits)</sup><sup>[\[6\]](docs/composition-requests.md#durability-ordering-and-audit)</sup>

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

Read about [composition requests](docs/composition-requests.md) and [GraphRule constraints](docs/graph-rules.md).

</details>

### Network boundaries

Group workloads into subgraphs with explicit network connections. Scoped rules
control traffic across boundaries and namespaces; optional Istio integration
adds HTTP and service-identity authorization.<sup>[\[7\]](docs/networking.md#selection-scope-and-inheritance)</sup><sup>[\[8\]](docs/networking.md#cross-namespace-peers-and-http-authorization)</sup>

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

Read about [network scope and inheritance](docs/networking.md#selection-scope-and-inheritance) and [cross-namespace authorization](docs/networking.md#cross-namespace-peers-and-http-authorization).

</details>

### Graphs across node groups

Place whole graphs on groups of Kubernetes machines, such as general compute
or accelerators. Here, three graphs share two worker groups while coordinated
operator replicas and their shared Dragonfly cache run on a third.<sup>[\[9\]](docs/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[10\]](docs/operator.md#replicas-shared-queues-and-autoscaling)</sup>

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

Read about [graph placement](docs/operator.md#scheduling-a-graph-onto-a-resource-slice) and [operator replicas and shared queues](docs/operator.md#replicas-shared-queues-and-autoscaling).

</details>

### Workloads calling the operator

Running workloads can submit their next graph, read its status and subscribe to
graph events through the operator's optional APIs. Services route requests to
ready replicas; explicitly authorized callers can connect from other
namespaces.<sup>[\[15\]](docs/networking.md#workload-access-to-operator-apis)</sup>

A running service can also pulse downstream workloads or daemon replica groups,
with explicit concurrency and frequency policies.<sup>[\[17\]](docs/activation.md)</sup>

With a capacity policy, Polyad forecasts upcoming stages while earlier work
runs, giving a compatible node autoscaler advance notice. Dependencies and gates
still decide when the next stage starts.<sup>[\[16\]](docs/capacity.md)</sup>

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

Read about [workload API access](docs/networking.md#workload-access-to-operator-apis), [event subscriptions](docs/workload-events.md), and [advance capacity requests](docs/capacity.md).

</details>

### Finite pipelines

Express a workflow from preparation to publication, with parallel tasks and
gates that wait for a condition or delay. Ordinary Graph placement can select
spot capacity; applications choose how to handle interruption and storage.<sup>[\[11\]](docs/concepts.md#finite-pipelines)</sup><sup>[\[12\]](docs/operator.md#delay-gates)</sup>

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
            left["Ephemeral · partition A"]
            right["Ephemeral · partition B"]
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

Read about [finite pipelines](docs/concepts.md#finite-pipelines) and [admission and delay gates](docs/operator.md#delay-gates).

</details>

### Persistent services and recurrence

Keep services running with `Daemon`, and repeat finite Graphs with activation
requests. A producer or timer supplies each pulse; the application owns iteration
limits, stop conditions and durable shared state.<sup>[\[13\]](docs/operator.md#repeated-execution)</sup>

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

Read about [repeated execution](docs/operator.md#repeated-execution) and [activation pulses](docs/activation.md).

</details>

The operator can also [request capacity ahead of upcoming stages](docs/capacity.md),
helping node autoscalers prepare machines while upstream work runs.

Queue pressure and graph hierarchies are available through the optional
[Prometheus and JSON metrics API](docs/metrics.md).
[KEDA can scale services or whole graph compositions](docs/replication.md) through
`ReplicaGroup`, using workload metrics served by the operator.

[Argo CD](docs/argocd.md) and [Flux health checks](docs/fluxcd.md) report graph and leaf health across nested applications.

## What Polyad is not

Polyad coordinates application graphs alongside existing cluster components.

- **A general-purpose policy engine such as OPA.** `GraphRule` constrains the
  graphs Polyad admits and the resources it compiles. It does not evaluate Rego,
  replace application authorization, or enforce policy on every Kubernetes API
  request. Cluster-wide admission policy remains a separate concern.<sup>[\[18\]](https://www.openpolicyagent.org/docs)</sup><sup>[\[19\]](docs/operator.md#structural-policy-and-composition-api)</sup>
- **A replacement for the Kubernetes scheduler or node autoscaler.** Polyad
  controls when graph work is admitted and propagates placement constraints.
  Kubernetes places Pods; the configured autoscaler provisions machines.
  Grouping work does not guarantee that every Pod starts together.<sup>[\[9\]](docs/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[20\]](docs/capacity.md#scheduling-demand-and-placement)</sup>
- **A service mesh or network transport.** Polyad generates network and Istio
  policy resources. The cluster's networking implementation and mesh enforce
  them; drawing a graph connection does not transport application data.<sup>[\[21\]](docs/networking.md#enforcement-and-lifecycle)</sup>
- **Automatic process checkpointing or exactly-once execution.** Restarting
  containers with persistent storage requires application recovery logic.
  Workloads must handle retries and duplicate effects; graph ownership and
  ordered API writes do not make application operations transactional.<sup>[\[22\]](docs/operator.md#workload-persistence)</sup><sup>[\[10\]](docs/operator.md#replicas-shared-queues-and-autoscaling)</sup>

## Get started

Deploy the [Kubernetes operator](docs/getting-started.md#quick-start-kubernetes)
with the Helm chart, or try the [local Python scheduler](docs/getting-started.md#quick-start-local-work).
The [examples](docs/getting-started.md#examples) cover pipelines, services,
spot work, storage and nested graphs.

Explore [graph concepts](docs/concepts.md), [graph rules](docs/graph-rules.md),
[composition requests](docs/composition-requests.md), the [composition API](docs/composition-api.md),
[networking and event subscriptions](docs/networking.md), or the
[development guide](docs/toolchain.md). See the [documentation index](docs/README.md)
for lifecycle, status, health and configuration references.

## License

[GNU General Public License v3.0 only](LICENSE).
