"""
Validate storage contracts before compiling Kubernetes workload pod templates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from polyad_types.resources.storage import Persistence
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from typing import Any

__all__ = ("configure_storage",)


def configure_storage(spec: dict[str, Any]) -> Persistence:
    """
    Mount declared persistent storage while preserving native volume configuration.

    Args:
        spec (dict[str, Any]): Mutable workload definition after resource-name resolution.

    Returns:
        Persistence: Validated contract for checking the live claim before admission.
    """
    persistence = converter.structure(spec.get("persistence", {}), Persistence)
    pod = spec["template"]["spec"]
    if not persistence.enabled:
        return persistence
    name = "polyad-persistence"
    volumes = pod.setdefault("volumes", [])
    if any(volume.get("name") == name for volume in volumes):
        raise ValueError("polyad-persistence is a reserved volume name")
    volumes.append({"name": name, "persistentVolumeClaim": {"claimName": persistence.claimName}})

    # Initialization and steady-state containers share the same claim and collision checks.
    for container in [*pod.get("initContainers", []), *pod["containers"]]:
        mounts = container.setdefault("volumeMounts", [])
        if any(mount.get("mountPath") == persistence.mountPath for mount in mounts):
            raise ValueError("persistence mountPath conflicts with a container volume mount")
        mounts.append({"name": name, "mountPath": persistence.mountPath})
    return persistence
