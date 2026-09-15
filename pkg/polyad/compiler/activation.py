"""
Represent immutable activation requests with explicit graph incarnation fences.
"""

from __future__ import annotations

import hashlib
from typing import Literal

from attrs import frozen

from polyad.compiler.composition import identity


@frozen
class ActivationRequest:
    """
    Request one execution of an activation-controlled graph vertex.

    Attributes:
        requestId (str): Idempotency identity retained for the receipt lifetime.
        graph (str): Executable graph instance name, not its reusable definition.
        graphUid (str): Kubernetes UID fencing graph deletion and recreation.
        node (str): Vertex name inside the selected graph instance.
        kind (Literal['Graph', 'EphemeralGraph', 'PolyGraph']): Target graph kind.
    """

    requestId: str
    graph: str
    graphUid: str
    node: str
    kind: Literal["Graph", "EphemeralGraph", "PolyGraph"] = "Graph"

    def __attrs_post_init__(self) -> None:
        """
        Require portable identities and a graph incarnation.

        Returns:
            None: Invalid identities raise before API access.
        """
        for value in (self.requestId, self.graph, self.node):
            identity(value)
        if not self.graphUid or len(self.graphUid) > 128 or self.kind not in {"Graph", "EphemeralGraph", "PolyGraph"}:
            raise ValueError("activation requires a valid graph kind and UID")


def activation_name(request_id: str) -> str:
    """
    Derive a durable receipt address without trusting an arbitrary resource name.

    Args:
        request_id (str): Client-chosen idempotency ID.

    Returns:
        str: Deterministic Kubernetes Activation name.
    """
    return "activation-" + hashlib.sha256(identity(request_id).encode()).hexdigest()[:32]
