"""
Model graph observations and own the structural schemas of status.metrics.
"""

from __future__ import annotations

from typing import Literal

from attrs import field, frozen

from polyad_types.resources.common import AST


@frozen(kw_only=True)
class LayerMetrics(AST):
    """
    Layer shape of an acyclic graph.

    Attributes:
        depth (int): Number of dependency layers; zero for an empty graph and one for independent nodes.
        breadth (int): Largest layer size.
        layerWidths (list[int]): Node counts by earliest topological generation, starting at the roots.
        breadthDepth (int): Breadth multiplied by depth; a shape summary, not an estimate of execution cost.
    """

    depth: int = field(
        default=0,
        metadata={
            "schema": {"minimum": 0, "description": "Number of dependency layers; zero for an empty graph and one for independent nodes."}
        },
    )
    breadth: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Largest layer size. This is layer breadth, not maximum antichain width or available concurrency.",
            }
        },
    )
    layerWidths: list[int] = field(
        factory=list,
        metadata={
            "schema": {
                "items": {"minimum": 0, "description": "Number of nodes in this generation."},
                "description": "Node counts by earliest topological generation, starting at the roots.",
            }
        },
    )
    breadthDepth: int = field(
        default=0,
        metadata={
            "schema": {"minimum": 0, "description": "Breadth multiplied by depth; a shape summary, not an estimate of execution cost."}
        },
    )


@frozen(kw_only=True)
class NodeCounts(AST):
    """
    Node counts by execution abstraction.

    Attributes:
        Workload (int): Number of Workload nodes.
        Daemon (int): Number of Daemon nodes.
        Resource (int): Number of Resource nodes.
        Graph (int): Number of Graph nodes.
        ReplicaGroup (int): Number of replication boundaries.
        PolyGraph (int): Number of PolyGraph boundaries.
    """

    Workload: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of Workload nodes."}})
    Daemon: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of Daemon nodes."}})
    Resource: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of Resource nodes."}})
    Graph: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of Graph nodes."}})
    ReplicaGroup: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of replication boundaries."}})
    PolyGraph: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of PolyGraph boundaries."}})


@frozen(kw_only=True)
class AdmissionMetrics(LayerMetrics):
    """
    Acyclic lifecycle dependencies used for admission.

    Attributes:
        edgeCount (int): Number of distinct dependency pairs, regardless of lifecycle predicate.
        rootCount (int): Nodes with no dependencies in this subset.
        leafCount (int): Nodes with no dependents in this subset.
        maxFanIn (int): Largest number of direct prerequisites.
        maxFanOut (int): Largest number of direct dependents.
    """

    edgeCount: int = field(
        default=0,
        metadata={"schema": {"minimum": 0, "description": "Number of distinct dependency pairs, regardless of lifecycle predicate."}},
    )
    rootCount: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Nodes with no dependencies in this subset."}})
    leafCount: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Nodes with no dependents in this subset."}})
    maxFanIn: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Largest number of direct prerequisites."}})
    maxFanOut: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Largest number of direct dependents."}})


@frozen(kw_only=True)
class ConnectionMetrics(AST):
    """
    Directed data-flow connections, which may contain cycles.

    Isolated nodes are included in component counts.

    Attributes:
        edgeCount (int): Number of distinct directed data-flow connections, including self-loops.
        weakComponents (int): Connected components after ignoring edge direction.
        strongComponents (int): Maximal components in which each node can reach every other node.
        cyclicComponents (int): Strong components with multiple nodes or a self-loop; this does not enumerate
            individual cycles.
        largestStrongComponent (int): Number of nodes in the largest strongly connected component.
        condensation (LayerMetrics): Layer shape after collapsing each strongly connected component to one node.
    """

    edgeCount: int = field(
        default=0,
        metadata={"schema": {"minimum": 0, "description": "Number of distinct directed data-flow connections, including self-loops."}},
    )
    weakComponents: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Connected components after ignoring edge direction."}}
    )
    strongComponents: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Maximal components in which each node can reach every other node."}}
    )
    cyclicComponents: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Strong components with multiple nodes or a self-loop; this does not enumerate individual cycles.",
            }
        },
    )
    largestStrongComponent: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Number of nodes in the largest strongly connected component."}}
    )
    condensation: LayerMetrics = field(
        factory=LayerMetrics,
        metadata={
            "schema": {
                "description": (
                    "Layer shape after collapsing each strongly connected component to one node. These depths count "
                    "components, not paths through cycles."
                )
            }
        },
    )


@frozen(kw_only=True)
class TopologyMetrics(AST):
    """
    Boundary-local graph shape.

    Nested Graph, PolyGraph and ReplicaGroup instances count as single nodes. Duplicate directed edges
    are collapsed.

    Attributes:
        nodeCount (int): Number of nodes in this graph or induced observed subset.
        nodesByKind (NodeCounts): Node counts by execution abstraction.
        subgraphCount (int): Number of immediate nested Graph, PolyGraph and ReplicaGroup nodes.
        admission (AdmissionMetrics): Acyclic lifecycle dependencies used for admission.
        connections (ConnectionMetrics): Directed data-flow connections, which may contain cycles.
    """

    nodeCount: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Number of nodes in this graph or induced observed subset."}}
    )
    nodesByKind: NodeCounts = field(factory=NodeCounts, metadata={"schema": {"description": "Node counts by execution abstraction."}})
    subgraphCount: int = field(
        default=0,
        metadata={"schema": {"minimum": 0, "description": "Number of immediate nested Graph, PolyGraph and ReplicaGroup nodes."}},
    )
    admission: AdmissionMetrics = field(
        factory=AdmissionMetrics, metadata={"schema": {"description": "Acyclic lifecycle dependencies used for admission."}}
    )
    connections: ConnectionMetrics = field(
        factory=ConnectionMetrics,
        metadata={
            "schema": {
                "description": "Directed data-flow connections, which may contain cycles. Isolated nodes are included in component counts."
            }
        },
    )


@frozen(kw_only=True)
class ExecutionMetrics(AST):
    """
    Fresh observations for nodes in this boundary.

    Counts overlap: a completed Job can also be ready. Terminating or failed execution retains slots until cleanup
    or completion.

    Attributes:
        observedNodes (int): Declared nodes with an owned execution resource, including terminating resources.
        pendingNodes (int): Declared nodes with no observed execution resource; includes dependency, gate and
            capacity waits.
        activeNodes (int): Observed nodes that are not terminating, completed or failed.
        readyNodes (int): Observed nodes satisfying their resource readiness predicate.
        completedNodes (int): Observed nodes whose finite execution completed.
        failedNodes (int): Observed nodes with terminal failure.
        terminatingNodes (int): Observed nodes whose execution resources have deletion timestamps.
        slotCapacity (int): Slot budget declared by this graph.
        reservedSlots (int): Slots reserved by observed nodes that have not completed, including terminating or
            failed nodes.
        availableSlots (int): Nonnegative difference between slot capacity and observed reservations.
    """

    observedNodes: int = field(
        default=0,
        metadata={
            "schema": {"minimum": 0, "description": "Declared nodes with an owned execution resource, including terminating resources."}
        },
    )
    pendingNodes: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Declared nodes with no observed execution resource; includes dependency, gate and capacity waits.",
            }
        },
    )
    activeNodes: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Observed nodes that are not terminating, completed or failed. This is not a Pod Running count.",
            }
        },
    )
    readyNodes: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Observed nodes satisfying their resource readiness predicate."}}
    )
    completedNodes: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Observed nodes whose finite execution completed."}}
    )
    failedNodes: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Observed nodes with terminal failure."}})
    terminatingNodes: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Observed nodes whose execution resources have deletion timestamps."}}
    )
    slotCapacity: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Slot budget declared by this graph."}})
    reservedSlots: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Slots reserved by observed nodes that have not completed, including terminating or failed nodes.",
            }
        },
    )
    availableSlots: int = field(
        default=0,
        metadata={"schema": {"minimum": 0, "description": "Nonnegative difference between slot capacity and observed reservations."}},
    )


@frozen(kw_only=True)
class ResourceCounts(AST):
    """
    Directly owned resource counts by Kubernetes kind.

    Attributes:
        Activation (int): Number of durable activation receipts.
        TemporaryConnection (int): Number of durable temporary connection receipts.
        NetworkPolicy (int): Number of owned transport policies.
        AuthorizationPolicy (int): Number of owned Istio authorization policies.
        PeerAuthentication (int): Number of owned mutual TLS policies.
        Job (int): Number of owned Job resources.
        Deployment (int): Number of owned Deployment resources.
        StatefulSet (int): Number of owned StatefulSet resources.
        DaemonSet (int): Number of owned DaemonSet resources.
        Dragonfly (int): Observed cache instances owned by the upstream Dragonfly operator.
        Cluster (int): Observed PostgreSQL clusters owned by CloudNativePG.
        Service (int): Number of owned Service resources.
        ConfigMap (int): Number of owned ConfigMap resources.
        PersistentVolumeClaim (int): Number of owned PersistentVolumeClaim resources.
        Graph (int): Number of owned Graph resources.
        ReplicaGroup (int): Number of owned replication boundaries.
        PolyGraph (int): Number of PolyGraph boundaries.
        Pod (int): Owned capacity placeholder Pods.
        PodTemplate (int): Owned scheduling templates for capacity plans.
        ProvisioningRequest (int): Owned autoscaler capacity requests.
    """

    Activation: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Durable activation receipts."}})
    TemporaryConnection: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Temporary connection receipts."}})
    NetworkPolicy: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Owned transport policies."}})
    AuthorizationPolicy: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Owned Istio authorization policies."}})
    PeerAuthentication: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Owned mutual TLS policies."}})
    Job: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned Job resources."}})
    Deployment: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned Deployment resources."}})
    StatefulSet: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned StatefulSet resources."}})
    DaemonSet: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned DaemonSet resources."}})
    Dragonfly: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Observed upstream Dragonfly instances."}})
    Cluster: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Observed CloudNativePG clusters."}})
    Service: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned Service resources."}})
    ConfigMap: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned ConfigMap resources."}})
    PersistentVolumeClaim: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned PersistentVolumeClaim resources."}}
    )
    Graph: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of owned Graph resources."}})
    ReplicaGroup: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of replication boundaries."}})
    PolyGraph: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Number of PolyGraph boundaries."}})

    Pod: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Owned capacity placeholder Pods."}})
    PodTemplate: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Owned scheduling templates for capacity plans."}}
    )
    ProvisioningRequest: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Owned autoscaler capacity requests."}})


@frozen(kw_only=True)
class ResourceMetrics(AST):
    """
    All directly owned resources, including obsolete and terminating children awaiting cleanup.

    Attributes:
        total (int): Total directly owned resource count.
        terminating (int): Directly owned resources awaiting deletion.
        byKind (ResourceCounts): Directly owned resource counts by Kubernetes kind.
    """

    total: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Total directly owned resource count."}})
    terminating: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Directly owned resources awaiting deletion."}})
    byKind: ResourceCounts = field(
        factory=ResourceCounts, metadata={"schema": {"description": "Directly owned resource counts by Kubernetes kind."}}
    )


@frozen(kw_only=True)
class SubgraphMetrics(AST):
    """
    Identity and freshness of a nested graph boundary.

    Attributes:
        kind (str): Nested boundary kind.
        name (str): Nested instance name in the parent namespace.
        uid (str): Persisted nested instance identity.
        node (str): Parent node name.
        phase (str): Last reported lifecycle phase; consult current before using it.
        generation (int): Current child spec generation.
        observedGeneration (int | None): Child generation evaluated by its metrics.
        current (bool): Whether child lifecycle and metrics observations match its generation and it is not
            terminating.
        topology (TopologyMetrics | None): Boundary-local graph shape.
        execution (ExecutionMetrics | None): Fresh observations for nodes in this boundary.
    """

    kind: str = field(default="", metadata={"schema": {"description": "Nested boundary kind."}})
    name: str = field(default="", metadata={"schema": {"description": "Nested instance name in the parent namespace."}})
    uid: str = field(default="", metadata={"schema": {"description": "Persisted nested instance identity."}})
    node: str = field(default="", metadata={"schema": {"description": "Parent node name."}})
    phase: str = field(default="", metadata={"schema": {"description": "Last reported lifecycle phase; consult current before using it."}})
    generation: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Current child spec generation."}})
    observedGeneration: int | None = field(
        default=None, metadata={"schema": {"minimum": 0, "description": "Child generation evaluated by its metrics."}, "emit_none": True}
    )
    current: bool = field(
        default=False,
        metadata={
            "schema": {
                "description": (
                    "Whether child lifecycle and metrics observations match its generation and it is not "
                    "terminating. This is generation freshness, not a heartbeat."
                )
            }
        },
    )
    topology: TopologyMetrics | None = field(
        default=None,
        metadata={
            "schema": {
                "description": (
                    "Boundary-local graph shape. Nested Graph, PolyGraph and ReplicaGroup instances "
                    "count as single nodes. Duplicate directed edges are collapsed."
                )
            },
            "emit_none": True,
        },
    )
    execution: ExecutionMetrics | None = field(
        default=None,
        metadata={
            "schema": {
                "description": (
                    "Fresh observations for nodes in this boundary. Counts overlap: a completed Job can also be "
                    "ready. Terminating or failed execution retains slots until cleanup or completion."
                )
            },
            "emit_none": True,
        },
    )


@frozen(kw_only=True)
class PhaseCounts(AST):
    """
    Known boundaries by their generation-current lifecycle phase; unavailable summaries count as Unknown.

    Attributes:
        Reconciling (int): Boundaries in phase Reconciling.
        Ready (int): Boundaries in phase Ready.
        Running (int): Boundaries in phase Running.
        Waiting (int): Boundaries in phase Waiting.
        Draining (int): Boundaries in phase Draining.
        Suspended (int): Boundaries in phase Suspended.
        Stopped (int): Boundaries in phase Stopped.
        Completed (int): Boundaries in phase Completed.
        Failed (int): Boundaries in phase Failed.
        Invalid (int): Boundaries in phase Invalid.
        Unknown (int): Boundaries in phase Unknown.
    """

    Reconciling: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Reconciling."}})
    Ready: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Ready."}})
    Running: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Running."}})
    Waiting: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Waiting."}})
    Draining: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Draining."}})
    Suspended: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Suspended."}})
    Stopped: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Stopped."}})
    Completed: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Completed."}})
    Failed: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Failed."}})
    Invalid: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Invalid."}})
    Unknown: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Boundaries in phase Unknown."}})


@frozen(kw_only=True)
class RollupMetrics(AST):
    """
    Recursive status fold across mixed graph types.

    Local leaves are combined with each child subtree exactly once.
    Missing or stale summaries are omitted and flagged.

    Attributes:
        scope (Literal['subtree']): This boundary and all currently observed descendant boundaries.
        observedGeneration (int): Parent generation evaluated for this aggregate.
        observationsComplete (bool): All expected immediate graph instances and recursively composed summaries were
            observed at matching generations.
        graphCount (int): Known graph boundaries including this one.
        unobservedGraphs (int): Known missing instances or unavailable/stale child summaries.
        nestingDepth (int): Maximum known nesting layers including this boundary as layer one; independent of
            admission dependency depth.
        graphsByPhase (PhaseCounts): Known boundaries by their generation-current lifecycle phase; unavailable
            summaries count as Unknown.
        leafNodes (int): Declared non-boundary nodes across observed graph specs.
        observedLeafNodes (int): Declared non-boundary nodes with owned execution resources.
        pendingLeafNodes (int): Known leaf nodes with no observed execution resource.
        activeLeafNodes (int): Observed leaf resources that are not completed, failed or terminating; not a Pod
            Running count.
        readyLeafNodes (int): Leaf nodes satisfying readiness.
        completedLeafNodes (int): Observed finite leaf nodes that completed.
        failedLeafNodes (int): Leaf nodes reporting terminal failure.
        terminatingLeafNodes (int): Declared leaf executions awaiting deletion.
        resourceCount (int): All known owned resources across descendants, including nested boundary CRs and
            obsolete resources.
        terminatingResources (int): Known owned resources awaiting deletion across descendants.
        capacityPlans (int): Known active capacity plans across observed graph instances.
        capacityRequestedPods (int): Future Pods in active capacity plans.
        capacityReadyPods (int): Future Pods with last-observed usable capacity.
        capacityFailedPlans (int): Failed or expired capacity plans requiring a retry.
    """

    scope: Literal["subtree"] = field(
        default="subtree", metadata={"schema": {"description": "This boundary and all currently observed descendant boundaries."}}
    )
    observedGeneration: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Parent generation evaluated for this aggregate."}}
    )
    observationsComplete: bool = field(
        default=False,
        metadata={
            "schema": {
                "description": (
                    "All expected immediate graph instances and recursively composed summaries were observed at "
                    "matching generations. This is eventual generation freshness, not a heartbeat or atomic cluster "
                    "snapshot."
                )
            }
        },
    )
    graphCount: int = field(
        default=0,
        metadata={"schema": {"minimum": 0, "description": "Known graph boundaries including this one. Each boundary counts once."}},
    )
    unobservedGraphs: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": (
                    "Known missing instances or unavailable/stale child summaries. Descendant totals are lower bounds when nonzero."
                ),
            }
        },
    )
    nestingDepth: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": (
                    "Maximum known nesting layers including this boundary as layer one; independent of admission dependency depth."
                ),
            }
        },
    )
    graphsByPhase: PhaseCounts = field(
        factory=PhaseCounts,
        metadata={
            "schema": {
                "description": "Known boundaries by their generation-current lifecycle phase; unavailable summaries count as Unknown."
            }
        },
    )
    leafNodes: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Declared non-boundary nodes across observed graph specs. Uninstantiated subgraph contents are unknown.",
            }
        },
    )
    observedLeafNodes: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Declared non-boundary nodes with owned execution resources."}}
    )
    pendingLeafNodes: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Known leaf nodes with no observed execution resource."}}
    )
    activeLeafNodes: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Observed leaf resources that are not completed, failed or terminating; not a Pod Running count.",
            }
        },
    )
    readyLeafNodes: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Leaf nodes satisfying readiness."}})
    completedLeafNodes: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Observed finite leaf nodes that completed.",
            }
        },
    )
    failedLeafNodes: int = field(default=0, metadata={"schema": {"minimum": 0, "description": "Leaf nodes reporting terminal failure."}})
    terminatingLeafNodes: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Declared leaf executions awaiting deletion."}}
    )
    resourceCount: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": (
                    "All known owned resources across descendants, including nested boundary CRs and obsolete "
                    "resources. Excludes this root CR; each owned resource counts once."
                ),
            }
        },
    )
    terminatingResources: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Known owned resources awaiting deletion across descendants."}}
    )

    capacityPlans: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Known active capacity plans across observed graph instances."}}
    )
    capacityRequestedPods: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Future Pods in active capacity plans."}}
    )
    capacityReadyPods: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Future Pods with last-observed usable capacity."}}
    )
    capacityFailedPlans: int = field(
        default=0, metadata={"schema": {"minimum": 0, "description": "Failed or expired capacity plans requiring a retry."}}
    )


@frozen(kw_only=True)
class GraphMetrics(AST):
    """
    Graph instance metrics refreshed through the ordered, ownership-guarded API queue.

    Counts describe direct owned resources, not a flattened descendant tree.

    Attributes:
        scope (Literal['boundary']): Metrics cover this scheduling boundary only.
        observedGeneration (int): Parent spec generation used to compute these metrics, independently of lifecycle
            observedGeneration.
        topology (TopologyMetrics | None): Desired shape of the current graph spec.
        observedTopology (TopologyMetrics | None): Induced graph of declared nodes with observed child resources,
            including terminating or superseded execution.
        execution (ExecutionMetrics | None): Fresh observations for nodes in this boundary.
        topologyError (str): Validation error if graph shape cannot be computed; empty for a valid topology.
        resources (ResourceMetrics): All directly owned resources, including obsolete and terminating children
            awaiting cleanup.
        subgraphs (list[SubgraphMetrics]): Immediate graph instances, sorted by kind and name.
        rollup (RollupMetrics): Recursive status fold across mixed graph types.
    """

    scope: Literal["boundary"] = field(
        default="boundary", metadata={"schema": {"description": "Metrics cover this scheduling boundary only."}}
    )
    observedGeneration: int = field(
        default=0,
        metadata={
            "schema": {
                "minimum": 0,
                "description": "Parent spec generation used to compute these metrics, independently of lifecycle observedGeneration.",
            }
        },
    )
    topology: TopologyMetrics | None = field(
        default=None,
        metadata={
            "schema": {"description": "Desired shape of the current graph spec. Null when invalid."},
            "emit_none": True,
        },
    )
    observedTopology: TopologyMetrics | None = field(
        default=None,
        metadata={
            "schema": {
                "description": (
                    "Induced graph of declared nodes with observed child resources, including terminating or "
                    "superseded execution. Null when the topology is invalid."
                )
            },
            "emit_none": True,
        },
    )
    execution: ExecutionMetrics | None = field(
        default=None,
        metadata={
            "schema": {
                "description": (
                    "Fresh observations for nodes in this boundary. Counts overlap: a completed Job can also be "
                    "ready. Terminating or failed execution retains slots until cleanup or completion."
                )
            },
            "emit_none": True,
        },
    )
    topologyError: str = field(
        default="", metadata={"schema": {"description": "Validation error if graph shape cannot be computed; empty for a valid topology."}}
    )
    resources: ResourceMetrics = field(
        factory=ResourceMetrics,
        metadata={"schema": {"description": "All directly owned resources, including obsolete and terminating children awaiting cleanup."}},
    )
    subgraphs: list[SubgraphMetrics] = field(
        factory=list,
        metadata={
            "schema": {
                "description": "Immediate graph instances, sorted by kind and name. Summaries do not recursively embed descendant metrics."
            }
        },
    )
    rollup: RollupMetrics = field(
        factory=RollupMetrics,
        metadata={
            "schema": {
                "description": (
                    "Recursive status fold across mixed graph types. Local leaves are combined with each child "
                    "subtree exactly once. Missing or stale "
                    "summaries are omitted and flagged."
                )
            }
        },
    )
