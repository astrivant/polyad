"""
Compose typed graph references while preserving specialized boundary kinds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, assert_type

from attrs import frozen

from polyad.graph import GraphNode, PolyGraph

if TYPE_CHECKING:
    from polyad.graph import Topology


@frozen(kw_only=True)
class BatchGraph(GraphNode):
    """
    Restrict a reference to a reusable Graph definition.

    Attributes:
        kind (Literal['Graph']): Referenced boundary kind.
    """

    kind: Literal["Graph"] = "Graph"


batch = BatchGraph(name="batch", ref="batch-template")
application = PolyGraph[BatchGraph](nodes=(batch,))
assert_type(application.nodes[0], BatchGraph)
assert_type(PolyGraph(nodes=(batch,)).nodes[0], BatchGraph)

# The default accepts a mixture of supported graph boundary kinds.
mixed: PolyGraph = PolyGraph(
    nodes=(batch, GraphNode(name="followup", kind="Graph", ref="followup-template")),
)
assert_type(mixed.nodes[0], GraphNode)

# Frozen specializations can be passed to APIs accepting more general graphs.
general: PolyGraph[GraphNode] = application
topology: Topology = application

# Composition remains a reference to a reusable definition, even for PolyGraphs.
root = PolyGraph(nodes=(GraphNode(name="application", kind="PolyGraph", ref="application-template"),))
