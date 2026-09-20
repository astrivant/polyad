"""
Describe application adaptation lifecycle reports for one graph workload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from attrs import frozen

__all__ = ("AdaptationReport",)


@frozen
class AdaptationReport:
    """
    Fence one SDK strategy invocation to its graph and workload identity.

    Attributes:
        graph (str): Containing graph name used for authorization.
        graphUid (str): Exact containing graph incarnation.
        target (str): Reusable Workload or Daemon definition name.
        targetUid (str): Exact definition incarnation receiving status.
        targetGeneration (int): Definition generation represented by the report.
        targetKind (Literal['Workload', 'Daemon']): Definition resource kind.
        node (str): Logical node invoking the strategy.
        invocationId (str): Stable identity for one retryable strategy call.
        strategy (str): Concrete strategy class name.
        phase (Literal['Running', 'Succeeded', 'Failed']): Invocation lifecycle transition.
        observedAt (str): Timezone-aware transition timestamp.
        graphKind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Containing boundary kind.
    """

    graph: str
    graphUid: str
    target: str
    targetUid: str
    targetGeneration: int
    targetKind: Literal["Workload", "Daemon"]
    node: str
    invocationId: str
    strategy: str
    phase: Literal["Running", "Succeeded", "Failed"]
    observedAt: str
    graphKind: Literal["Graph", "PolyGraph", "ReplicaGroup"] = "Graph"

    def __attrs_post_init__(self) -> None:
        """
        Reject reports that cannot be fenced or represented in bounded status.

        Returns:
            None: Valid reports retain their exact identities and timestamp.
        """
        if not all((self.graph, self.graphUid, self.target, self.targetUid, self.node, self.invocationId, self.strategy)):
            raise ValueError("adaptation reports require graph, target, node, invocation and strategy identities")
        if (
            self.graphKind not in {"Graph", "PolyGraph", "ReplicaGroup"}
            or self.targetKind not in {"Workload", "Daemon"}
            or self.phase not in {"Running", "Succeeded", "Failed"}
        ):
            raise ValueError("adaptation report kind or phase is invalid")
        if type(self.targetGeneration) is not int or self.targetGeneration < 1:
            raise ValueError("adaptation reports require a positive target generation")
        if any(len(value) > 128 for value in (self.graphUid, self.targetUid, self.node, self.invocationId, self.strategy)):
            raise ValueError("adaptation report identities must be at most 128 characters")
        if datetime.fromisoformat(self.observedAt.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("adaptation reports require a timezone")
