"""
Compile owned child resources while retaining stable identity and revision hashes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import TYPE_CHECKING

from polyad.compiler.asts import (
    GROUP,
    ConfigMap,
    Deployment,
    DeploymentSpec,
    Job,
    JobSpec,
    ObjectMeta,
    OwnerReference,
    converter,
    to_document,
)
from polyad.compiler.asts.resources import RESOURCE_REGISTRY, SpecResource

if TYPE_CHECKING:
    from typing import Any

    from polyad.compiler.asts import (
        Resource,
    )


def child_name(parent: ObjectMeta, node: str) -> str:
    """
    Keep addresses stable across revisions and distinct within a boundary.

    Args:
        parent (ObjectMeta): Persisted parent identity and ownership boundary.
        node (str): Name of the node within its graph boundary.

    Returns:
        str: Stable child name derived from the parent UID and node name.
    """
    if not parent.name or not parent.uid:
        raise ValueError("a child requires a named parent with a persisted UID")
    suffix = hashlib.sha256(node.encode()).hexdigest()[:8]
    return f"{parent.name[:20]}-{node[:16]}-{parent.uid[:8]}-{suffix}"


def owned_child(
    parent: Resource,
    node_name: str,
    kind: str,
    spec: dict[str, Any] | JobSpec | DeploymentSpec,
    *,
    extra: dict[str, Any] | None = None,
) -> Resource:
    """
    Create typed resource identity, ownership and payload without mutating graph definitions.

    Args:
        parent (Resource): Persisted parent identity and ownership boundary.
        node_name (str): Node name used for child identity and ownership labels.
        kind (str): Kubernetes resource kind.
        spec (dict[str, Any] | JobSpec | DeploymentSpec): Desired resource configuration.
        extra (dict[str, Any] | None): Unmodeled native fields preserved during serialization.

    Returns:
        Resource: Owned resource AST ready for serialization.
    """
    meta = parent.metadata
    if not meta.namespace or not meta.name or not meta.uid:
        raise ValueError("a child requires a namespaced parent with a persisted UID")
    if kind not in RESOURCE_REGISTRY:
        raise ValueError(f"unsupported child kind: {kind}")
    extension = copy.deepcopy(extra or {})
    if {"apiVersion", "kind", "metadata", "spec", "status"} & extension.keys():
        raise ValueError("child extension fields cannot override identity, ownership, spec or status")
    raw_spec = to_document(spec) if isinstance(spec, (JobSpec, DeploymentSpec)) else copy.deepcopy(spec)
    # Preserve the pre-AST hash contract: this refactor must not replace existing workloads.
    digest = hashlib.sha256(json.dumps([kind, raw_spec, extra], sort_keys=True).encode()).hexdigest()[:12]
    annotations = {f"{GROUP}/desired-hash": digest}
    if f"{GROUP}/lineage" in (meta.annotations or {}):
        annotations[f"{GROUP}/lineage"] = (meta.annotations or {})[f"{GROUP}/lineage"]
    if (meta.annotations or {}).get(f"{GROUP}/ephemeral") == "true":
        annotations[f"{GROUP}/ephemeral"] = "true"
    for key in ("request-id", "composition-uid", "object-id", "node-path"):
        if f"{GROUP}/{key}" in (meta.annotations or {}):
            annotations[f"{GROUP}/{key}"] = (meta.annotations or {})[f"{GROUP}/{key}"]
    if f"{GROUP}/request-id" in annotations:
        path = annotations.get(f"{GROUP}/node-path", annotations.get(f"{GROUP}/object-id", meta.name))
        annotations[f"{GROUP}/node-path"] = f"{path}/{node_name}"
    metadata = ObjectMeta(
        name=child_name(meta, node_name),
        namespace=meta.namespace,
        annotations=annotations,
        labels={
            f"{GROUP}/owner": meta.uid,
            f"{GROUP}/node": node_name,
            **({f"{GROUP}/request": (meta.labels or {})[f"{GROUP}/request"]} if f"{GROUP}/request" in (meta.labels or {}) else {}),
        },
        ownerReferences=(
            OwnerReference(
                apiVersion=parent.resource_type.api_version,
                kind=parent.resource_type.kind,
                name=meta.name,
                uid=meta.uid,
                controller=True,
                blockOwnerDeletion=True,
            ),
        ),
    )
    if kind == "Job":
        return Job(metadata=metadata, spec=converter.structure(raw_spec, JobSpec), extra=extension)
    if kind == "Deployment":
        return Deployment(metadata=metadata, spec=converter.structure(raw_spec, DeploymentSpec), extra=extension)
    if kind == "ConfigMap":
        if raw_spec:
            raise ValueError("ConfigMap has no spec")
        return ConfigMap(
            metadata=metadata,
            data=extension.pop("data", None),
            binaryData=extension.pop("binaryData", None),
            immutable=extension.pop("immutable", None),
            extra=extension,
        )
    cls = RESOURCE_REGISTRY[kind]
    if not issubclass(cls, SpecResource):
        raise ValueError(f"child kind requires a dedicated compiler: {kind}")
    return cls(metadata=metadata, spec=raw_spec, extra=extension)
