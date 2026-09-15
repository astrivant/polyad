"""
Compile inert capacity demand from fully admitted workload Pod templates.
"""

from __future__ import annotations

import copy
from decimal import Decimal
from typing import TYPE_CHECKING

from kubernetes.utils.quantity import parse_quantity

if TYPE_CHECKING:
    from typing import Any

    from polyad.graph.topology import Topology


def requests(spec: dict[str, Any], *, overhead: bool = True) -> dict[str, str]:
    """
    Calculate scheduler requests including sequential init containers and native sidecars.

    Args:
        spec (dict[str, Any]): Admission-defaulted Pod spec.
        overhead (bool): Include RuntimeClass overhead in totals.

    Returns:
        dict[str, str]: Effective resource requests as Kubernetes decimal quantities.
    """

    def resources(container: dict[str, Any]) -> dict[str, Decimal]:
        values = container.get("resources", {})
        return {key: parse_quantity(value) for key, value in {**values.get("limits", {}), **values.get("requests", {})}.items()}

    def add(target: dict[str, Decimal], values: dict[str, Decimal]) -> None:
        for key, value in values.items():
            target[key] = target.get(key, Decimal(0)) + value

    regular: dict[str, Decimal] = {}
    sidecars: dict[str, Decimal] = {}
    peak: dict[str, Decimal] = {}
    for container in spec.get("containers", []):
        add(regular, resources(container))
    for container in spec.get("initContainers", []):
        current = dict(sidecars)
        add(current, resources(container))
        if container.get("restartPolicy") == "Always":
            sidecars = current
        for key, value in current.items():
            peak[key] = max(peak.get(key, Decimal(0)), value)
    add(regular, sidecars)
    for key, value in peak.items():
        regular[key] = max(regular.get(key, Decimal(0)), value)
    for key, value in spec.get("resources", {}).get("requests", {}).items():
        regular[key] = parse_quantity(value)
    if overhead:
        add(regular, {key: parse_quantity(value) for key, value in spec.get("overhead", {}).items()})
    if any(not value.is_finite() or value < 0 for value in regular.values()):
        raise ValueError("capacity requests must be finite and nonnegative")
    return {key: str(value) for key, value in regular.items() if value}


def frontier(graph: Topology, present: set[str]) -> list[str]:
    """
    Select a bounded number of uncreated dependency layers in deterministic order.

    Args:
        graph (Topology): Validated acyclic admission graph with a capacity plan.
        present (set[str]): Nodes already represented by execution resources.

    Returns:
        list[str]: Missing nodes ordered by distance from existing execution.
    """
    assert graph.capacity is not None
    depths: dict[str, int] = {}
    pending = {node.name: node for node in graph.nodes}
    while pending:
        for name, node in list(pending.items()):
            if all(edge.node in depths for edge in node.requires):
                depths[name] = -1 if name in present else max((depths[edge.node] + 1 for edge in node.requires), default=0)
                del pending[name]
    return sorted(
        (name for name, depth in depths.items() if 0 <= depth < graph.capacity.lookaheadStages), key=lambda name: (depths[name], name)
    )


def placeholder(spec: dict[str, Any], *, image: str, priority_class: str, priority: int) -> dict[str, Any]:
    """
    Build an inert Pod without application labels, credentials or executable code.

    Args:
        spec (dict[str, Any]): Admission-defaulted workload Pod spec.
        image (str): Administrator-selected pause image.
        priority_class (str): Lower-priority class selected by the administrator.
        priority (int): Expected numeric priority for the placeholder class.

    Returns:
        dict[str, Any]: Placeholder Pod spec with equivalent supported scheduling requests.
    """
    affinity = spec.get("affinity", {})
    if (
        any(key in affinity for key in ("podAffinity", "podAntiAffinity"))
        or spec.get("topologySpreadConstraints")
        or spec.get("resourceClaims")
        or spec.get("nodeName")
        or spec.get("schedulingGates")
        or spec.get("schedulerName", "default-scheduler") != "default-scheduler"
        or any("persistentVolumeClaim" in volume or "ephemeral" in volume for volume in spec.get("volumes", []))
        or any(port.get("hostPort") for container in spec.get("containers", []) for port in container.get("ports", []))
    ):
        raise ValueError(
            "placeholder capacity cannot model storage, Pod affinity, topology spread, "
            "host ports or custom scheduling; use ProvisioningRequest"
        )
    if spec.get("priority", 0) <= priority:
        raise ValueError("workload priority must exceed the placeholder PriorityClass value")
    demand = requests(spec, overhead=False)
    if not demand:
        raise ValueError("capacity planning requires nonzero resource requests")
    result = {key: copy.deepcopy(spec[key]) for key in ("nodeSelector", "affinity", "tolerations", "runtimeClassName") if key in spec}
    result.update(
        priorityClassName=priority_class,
        preemptionPolicy="Never",
        restartPolicy="Never",
        terminationGracePeriodSeconds=0,
        automountServiceAccountToken=False,
        securityContext={"runAsNonRoot": True, "runAsUser": 65532, "seccompProfile": {"type": "RuntimeDefault"}},
        containers=[
            {
                "name": "capacity",
                "image": image,
                "resources": {
                    "requests": demand,
                    "limits": {key: value for key, value in demand.items() if key not in {"cpu", "memory", "ephemeral-storage"}},
                },
                "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
            }
        ],
    )
    return result
