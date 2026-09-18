"""
Load full and partial Helm configuration contracts with locally bundled references.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_schemas._catalog import load_schema

if TYPE_CHECKING:
    from typing import Any


def values_schema(*, partial: bool = False, chart: str = "polyad") -> dict[str, Any]:
    """
    Select merged Helm values or an administrator's partial overlay contract.

    Args:
        partial (bool): Allow omitted mapping fields while retaining complete replacement array entries.
        chart (str): Select the operator chart polyad or the named-resource chart polyad-crds.

    Returns:
        dict[str, Any]: Independent Draft 7 schema with all references resolved locally.
    """
    if chart not in {"polyad", "polyad-crds"}:
        raise ValueError("chart must be polyad or polyad-crds")
    prefix = "helm-crds" if chart == "polyad-crds" else "helm"
    return load_schema(prefix + ("-reference" if partial else "-values"))
