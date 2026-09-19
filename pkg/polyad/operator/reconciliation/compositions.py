"""
Materialize immutable composition receipts through the leased reconciliation queue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad.compiler.passes.composition import compile_composition, read_receipt, request_name
from polyad.operator.observability.graph_status import observed
from polyad.operator.policies.rules import check_rules
from polyad_types import resources as asts
from polyad_types.api.requests import COMPOSITION_KINDS

if TYPE_CHECKING:
    from typing import Any

    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.reconciliation.controller import Controller


async def inventory(api: API, namespace: str, uid: str) -> list[dict[str, Any]]:
    """
    List composition-owned definitions with both label and owner UID fences.

    Args:
        api (API): Refreshed Kubernetes read adapter.
        namespace (str): Composition namespace.
        uid (str): Persisted Composition identity.

    Returns:
        list[dict[str, Any]]: Current definitions, including terminating objects.
    """
    result = []
    for kind in sorted(COMPOSITION_KINDS):
        response = await api.request("GET", kind, namespace, query=[("labelSelector", f"{asts.GROUP}/owner={uid}")])
        for item in response.get("items", []):
            item.setdefault("kind", kind)
            if any(owner.get("uid") == uid for owner in item["metadata"].get("ownerReferences", [])):
                result.append(item)
    return result


async def reconcile_composition(controller: Controller, obj: dict[str, Any]) -> None:
    """
    Preflight policies, observe templates, then create the root and report audit identities.

    Args:
        controller (Controller): Queue-owned controller providing guarded mutations.
        obj (dict[str, Any]): Fresh immutable Composition receipt.

    Returns:
        None: No return value.
    """
    from polyad.operator.reconciliation.controller import Pending

    request = read_receipt(obj["spec"])
    if obj["metadata"]["name"] != request_name(request.requestId):
        raise ValueError("Composition name must match its requestId digest")
    meta = obj["metadata"]
    manifests = compile_composition(request, meta["namespace"], owner_uid=meta["uid"])
    root = manifests[request.rootId]
    documents = {(item.resource_type.kind, item.metadata.name or ""): asts.to_document(item) for item in manifests.values()}
    await check_rules(
        controller.api,
        meta["namespace"],
        root.resource_type.kind,
        documents[(root.resource_type.kind, root.metadata.name or "")]["spec"],
        definitions=documents,
    )
    identities = {}
    created = False
    for item_id, desired in manifests.items():
        if item_id == request.rootId:
            continue
        current = await controller.ensure(desired)
        created |= current is None
        identities[item_id] = {
            "kind": desired.resource_type.kind,
            "name": desired.metadata.name,
            "uid": current["metadata"]["uid"] if current else None,
        }
    if created:
        raise Pending("waiting for composition definitions to be observed")
    instance = await controller.ensure(root)
    identities[request.rootId] = {
        "kind": root.resource_type.kind,
        "name": root.metadata.name,
        "uid": instance["metadata"]["uid"] if instance else None,
    }
    state = observed(instance) if instance else {"ready": False, "completed": False, "failed": False}
    await controller.status(
        obj,
        {
            "requestId": request.requestId,
            "requestHash": request.digest(),
            "rootId": request.rootId,
            "objects": identities,
            "observedGeneration": meta["generation"],
            "phase": "Failed" if state["failed"] else "Completed" if state["completed"] else "Ready" if state["ready"] else "Reconciling",
            **{key: state[key] for key in ("ready", "completed", "failed")},
        },
    )


async def drain_composition(controller: Controller, obj: dict[str, Any]) -> bool:
    """
    Drain the executable root before removing its reusable definitions.

    Args:
        controller (Controller): Queue-owned controller providing guarded deletes.
        obj (dict[str, Any]): Deleting Composition receipt.

    Returns:
        bool: Whether a fresh inventory is empty.
    """
    children = await inventory(controller.api, obj["metadata"]["namespace"], obj["metadata"]["uid"])
    roots = [child for child in children if child["kind"] in asts.BOUNDARY_KINDS and not child["spec"].get("templateOnly", False)]
    for child in roots or children:
        if not child["metadata"].get("deletionTimestamp"):
            await controller.api.delete(child)
    return not children
