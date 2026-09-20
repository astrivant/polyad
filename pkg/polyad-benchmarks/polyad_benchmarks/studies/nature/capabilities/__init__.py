"""
Declare mutable and fixed service capabilities for the larger Nature population.
"""

from __future__ import annotations

import nature


def catalog() -> tuple[nature.Placement, ...]:
    """
    Offer six initial square services and two additional increment capabilities.

    Returns:
        tuple[nature.Placement, ...]: A and F can mutate; C is relatively expensive.
    """
    initial = tuple(nature.Placement(name, nature.SQUARE, 2 if name == "C" else 1) for name in ("A", "B", "C", "E", "F", "H"))

    # Offer fused and staged alternatives so selection can trade process cost against capability.
    return initial + (
        nature.Placement("A", nature.FUSED),
        nature.Placement("F", nature.FUSED),
        nature.Placement("D", nature.INCREMENT),
        nature.Placement("G", nature.INCREMENT),
    )
