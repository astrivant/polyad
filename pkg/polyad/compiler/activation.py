"""
Represent immutable activation requests with explicit graph incarnation fences.
"""

from __future__ import annotations

import hashlib

from polyad_types.api.requests import identity


def activation_name(request_id: str) -> str:
    """
    Derive a durable receipt address without trusting an arbitrary resource name.

    Args:
        request_id (str): Client-chosen idempotency ID.

    Returns:
        str: Deterministic Kubernetes Activation name.
    """
    return "activation-" + hashlib.sha256(identity(request_id).encode()).hexdigest()[:32]
