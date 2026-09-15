# Polyad

Polyad<sup>[\[1\]](https://en.wikipedia.org/wiki/Polyad_%28mathematics%29)</sup> is a graph-based workload scheduler for Kubernetes. Describe tasks,
long-running services and resources as composable graphs; the operator schedules
their work and tracks progress across the application and integrations.<sup>[\[2\]](docs/operator.md#api-and-python-abstractions)</sup>

**[Get started](docs/getting-started.md)** · **[Documentation](docs/README.md)** · **[Helm chart](charts/polyad/README.md)**

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

```mermaid
flowchart BT
    job["Workload"] --> batch["Graph · batch"]
    daemon["Daemon"] --> service["Graph · service"]
    spot["EphemeralGraph · spot work"] --> group["PolyGraph · processing"]
    batch --> group
    group --> root["PolyGraph · application"]
    service --> root
    epochs["Feedback · recurring graphs"] --> root
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#fff3d6,stroke:#926000,color:#513900
    class job,daemon,batch,service,group,root execution
    class spot,epochs constraint
```

### Constrained compositions

Build workflows from reusable definitions and trace each instance to its
Kubernetes resources. `GraphRule` lets engineers constrain what users can
schedule by size, shape, nesting and mathematical properties.<sup>[\[5\]](docs/composition-api.md#mathematical-constraints)</sup><sup>[\[6\]](docs/composition-api.md#durability-ordering-and-audit)</sup>

```mermaid
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

### Network boundaries

Group workloads into subgraphs with explicit network connections. Scoped rules
control traffic across boundaries and namespaces; optional Istio integration
adds HTTP and service-identity authorization.<sup>[\[7\]](docs/networking.md#selection-scope-and-inheritance)</sup><sup>[\[8\]](docs/networking.md#cross-namespace-peers-and-http-authorization)</sup>

```mermaid
flowchart LR
    rules["GraphRule · subtree scope"] -. inherits .-> group
    subgraph group["PolyGraph · application"]
        subgraph producers["Graph · producers"]
            task["Workload"] --> sender["Daemon"]
        end
        subgraph consumers["Graph · consumers"]
            receiver["Daemon"] --> report["Workload"]
        end
        sender -->|"Explicit connection · TCP 8080"| receiver
    end
    external["Service identity · another namespace"] -->|"GET /status"| receiver
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef constraint fill:#fff3d6,stroke:#926000,color:#513900
    classDef boundary fill:#f2f4f7,stroke:#667085,color:#344054
    class task,sender,receiver,report execution
    class rules,external constraint
    class group,producers,consumers boundary
```

### Graphs across node groups

Place whole graphs on groups of Kubernetes machines, such as general compute
or accelerators. Here, three graphs share two worker groups while coordinated
operator replicas and their shared Dragonfly cache run on a third.<sup>[\[9\]](docs/operator.md#scheduling-a-graph-onto-a-resource-slice)</sup><sup>[\[10\]](docs/operator.md#replicas-shared-queues-and-autoscaling)</sup>

```mermaid
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

### Workloads calling the operator

Running workloads can submit their next graph, read its status and subscribe to
graph events through the operator's optional APIs. Services route requests to
ready replicas; explicitly authorized callers can connect from other
namespaces.<sup>[\[15\]](docs/networking.md#workload-access-to-operator-apis)</sup>

With a capacity policy, Polyad forecasts upcoming stages while earlier work
runs, giving a compatible node autoscaler advance notice. Dependencies and gates
still decide when the next stage starts.<sup>[\[16\]](docs/capacity.md)</sup>

```mermaid
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
    events -. "SSE observations" .-> caller
    events -. "SSE observations" .-> subscriber
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

### Finite pipelines

Express a workflow from preparation to publication, with parallel tasks and
gates that wait for a condition or delay. `EphemeralGraph` groups interruptible
work for capacity such as spot instances.<sup>[\[11\]](docs/concepts.md#finite-pipelines)</sup><sup>[\[12\]](docs/operator.md#delay-gates)</sup>

```mermaid
flowchart LR
    subgraph pipeline["Graph · finite pipeline"]
        direction LR
        storage[("Resource · shared storage")]
        prepare["Workload · prepare"]
        gate{"Gate · admission condition"}
        subgraph workers["EphemeralGraph · spot placement"]
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
        workers -->|completed| merge
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

### Persistent services and recurrence

Keep services running with `Daemon`, and repeat a finite workflow with
`Feedback`. Each repetition is an **epoch**: for example, sample new measurements,
then adjust a service. Polyad completes and cleans up one epoch before starting
the next; the application supplies the decision logic and shared state.<sup>[\[13\]](docs/operator.md#feedback-epochs)</sup>

Solid arrows show startup or epoch progression; dashed arrows show data flow
or repetition.

```mermaid
flowchart LR
    subgraph system["Graph · persistent service"]
        direction LR
        queue[("Resource · queue service")]
        subgraph service["Graph · persistent processing"]
            ingest["Daemon · ingest"]
            process["Daemon · process"]
            ingest -->|ready| process
            ingest -.->|events| process
            process -.->|acknowledgements| ingest
        end
        subgraph feedback["Feedback · recurring graph epochs"]
            subgraph epoch["Graph · finite epoch"]
                sample["Workload · sample"]
                adjust["Workload · adjust"]
                sample -->|completed| adjust
            end
            next["Next epoch"]
            epoch -->|completed and drained| next
            next -.->|repeat until stopped or limit reached| sample
        end
        queue -->|ready| ingest
        service -->|ready| feedback
    end
    classDef execution fill:#e3f3e8,stroke:#247047,color:#163b29
    classDef recurrence fill:#ffe3a3,stroke:#926000,color:#513900
    classDef resource fill:#eeeeee,stroke:#777777,color:#444444
    class ingest,process,sample,adjust execution
    class next recurrence
    class queue resource
    style system fill:#e2e6ec,stroke:#667085,stroke-width:2px,color:#344054
    style service fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    style feedback fill:#fff3d6,stroke:#926000,stroke-width:2px,color:#513900
    style epoch fill:#ffffff,stroke:#667085,stroke-width:2px,color:#344054
    linkStyle default stroke:#475467,stroke-width:2px
```

The operator can also [request capacity ahead of upcoming stages](docs/capacity.md),
helping node autoscalers prepare machines while upstream work runs.

Queue pressure and graph hierarchies are available through the optional
[Prometheus and JSON metrics API](docs/metrics.md).
[Argo CD](docs/argocd.md) and [Flux health checks](docs/fluxcd.md) report graph and leaf health across nested applications.

## Get started

Deploy the [Kubernetes operator](docs/getting-started.md#quick-start-kubernetes)
with the Helm chart, or try the [local Python scheduler](docs/getting-started.md#quick-start-local-work).
The [examples](docs/getting-started.md#examples) cover pipelines, services,
spot work, storage and nested graphs.

Explore [graph concepts](docs/concepts.md), the [composition API](docs/composition-api.md),
[networking and event subscriptions](docs/networking.md), or the
[development guide](docs/toolchain.md). See the [documentation index](docs/README.md)
for lifecycle, status, health and configuration references.

## License

[GNU General Public License v3.0 only](LICENSE).
