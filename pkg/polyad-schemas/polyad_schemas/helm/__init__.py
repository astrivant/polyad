"""
Load full and partial Helm configuration contracts with locally bundled references.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_schemas._catalog import load_schema

if TYPE_CHECKING:
    from typing import Any


def values_schema(*, partial: bool = False) -> dict[str, Any]:
    """
    Select merged Helm values or an administrator's partial overlay contract.

    Args:
        partial (bool): Allow omitted mapping fields while retaining complete replacement array entries.

    Returns:
        dict[str, Any]: Independent Draft 7 schema with all references resolved locally.
    """
    return load_schema("helm-reference" if partial else "helm-values")
