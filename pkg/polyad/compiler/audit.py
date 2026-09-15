"""Carry request, definition and node-instance identities into native workload manifests."""

from __future__ import annotations

from typing import TYPE_CHECKING

from attrs import evolve

from polyad.compiler import asts
from polyad.compiler.composition import request_name

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.topology import Node


def trace_child(child: asts.Resource, parent: dict[str, Any], node: Node, definition: dict[str, Any]) -> asts.Resource:
    """
    Attach provenance to an owned child and to its pod template when present.

    Args:
        child (asts.Resource): Compiled owned resource.
        parent (dict[str, Any]): Instantiating graph boundary with its inherited audit path.
        node (Node): Graph node and optional client composition ID.
        definition (dict[str, Any]): Refreshed reusable definition used by the compiler.

    Returns:
        asts.Resource: Resource with request lineage and exact definition UID/generation references.
    """
    inherited = parent["metadata"].get("annotations", {})
    source = definition["metadata"]
    prefix = asts.GROUP
    values = {key: inherited[key] for key in (f"{prefix}/request-id", f"{prefix}/composition-uid") if key in inherited}
    node_id = node.id or node.name
    path = inherited.get(f"{prefix}/node-path", inherited.get(f"{prefix}/object-id", parent["metadata"]["name"]))
    values.update(
        {
            f"{prefix}/node-id": node_id,
            f"{prefix}/node-path": f"{path}/{node_id}",
            f"{prefix}/object-id": source.get("annotations", {}).get(f"{prefix}/object-id", source["name"]),
            f"{prefix}/definition-uid": source["uid"],
            f"{prefix}/definition-generation": str(source.get("generation", 1)),
        }
    )
    labels = dict(child.metadata.labels or {})
    if f"{prefix}/request-id" in values:
        labels[f"{prefix}/request"] = request_name(values[f"{prefix}/request-id"])[12:]
    result = evolve(child, metadata=evolve(child.metadata, labels=labels, annotations={**(child.metadata.annotations or {}), **values}))
    if isinstance(result, (asts.Job, asts.Deployment)):
        pod = result.spec.template
        meta = pod.metadata or asts.ObjectMeta()
        pod_labels = dict(meta.labels or {})
        if f"{prefix}/request" in labels:
            pod_labels[f"{prefix}/request"] = labels[f"{prefix}/request"]
        traced = evolve(pod, metadata=evolve(meta, labels=pod_labels, annotations={**(meta.annotations or {}), **values}))
        if isinstance(result, asts.Job):
            result = evolve(result, spec=evolve(result.spec, template=traced))
        else:
            result = evolve(result, spec=evolve(result.spec, template=traced))
    return result
