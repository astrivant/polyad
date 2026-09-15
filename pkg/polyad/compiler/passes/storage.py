"""
Validate storage contracts before compiling Kubernetes workload pod templates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad.graph.storage import Persistence
from polyad.graph.topology import converter

if TYPE_CHECKING:
    from typing import Any


def storage_fields(value: Any) -> bool:
    """
    Detect storage class and persistent claim declarations inside native specifications.

    Args:
        value (Any): Native pod or resource specification to inspect.

    Returns:
        bool: Whether this specification declares persistent storage or a storage class.
    """
    if isinstance(value, dict):
        return bool({"storageClass", "storageClassName", "persistentVolumeClaim", "volumeClaimTemplates"} & value.keys()) or any(
            storage_fields(item) for item in value.values()
        )
    return isinstance(value, list) and any(storage_fields(item) for item in value)


def configure_storage(spec: dict[str, Any], *, ephemeral: bool) -> Persistence:
    """
    Mount declared persistent storage and reject it on interruptible graph boundaries.

    Args:
        spec (dict[str, Any]): Mutable workload definition after resource-name resolution.
        ephemeral (bool): Whether this workload or any containing boundary is ephemeral.

    Returns:
        Persistence: Validated contract for checking the live claim before admission.
    """
    persistence = converter.structure(spec.get("persistence", {}), Persistence)
    pod = spec["template"]["spec"]
    if ephemeral and (
        persistence.enabled or persistence.claimName is not None or storage_fields(spec.get("persistence", {})) or storage_fields(pod)
    ):
        raise ValueError("persistent storage and storage classes are invalid under Ephemeral nodes or graphs")
    if not persistence.enabled:
        return persistence
    name = "polyad-persistence"
    volumes = pod.setdefault("volumes", [])
    if any(volume.get("name") == name for volume in volumes):
        raise ValueError("polyad-persistence is a reserved volume name")
    volumes.append({"name": name, "persistentVolumeClaim": {"claimName": persistence.claimName}})
    for container in [*pod.get("initContainers", []), *pod["containers"]]:
        mounts = container.setdefault("volumeMounts", [])
        if any(mount.get("mountPath") == persistence.mountPath for mount in mounts):
            raise ValueError("persistence mountPath conflicts with a container volume mount")
        mounts.append({"name": name, "mountPath": persistence.mountPath})
    return persistence
