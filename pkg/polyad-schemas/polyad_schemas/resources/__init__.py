"""
Load Kubernetes manifest contracts for Polyad and its pinned integrations.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_schemas._catalog import load_schema

if TYPE_CHECKING:
    from typing import Any


def resource_schema(kind: str, version: str = "v1alpha1", *, group: str = "polyad.astrivant.com") -> dict[str, Any]:
    """
    Select a manifest schema generated from a local CRD or pinned upstream contract.

    Args:
        kind (str): Kubernetes kind, such as Graph, PolyGraph or Gateway.
        version (str): Served CRD version included with this package.
        group (str): API group; defaults to Polyad and disambiguates upstream kinds such as Gateway.

    Returns:
        dict[str, Any]: Draft 2020-12 schema; CEL and Kubernetes extensions remain annotations.
    """
    name = f"{kind.lower()}.{version}" if group == "polyad.astrivant.com" else f"{kind.lower()}.{group}.{version}"
    return load_schema(name)
