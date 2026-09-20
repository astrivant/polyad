"""
Validate native VerticalPodAutoscaler manifests admitted through Resource nodes.
"""

from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from kubernetes.utils.quantity import parse_quantity

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

TARGET_KINDS = {"Deployment", "StatefulSet", "DaemonSet"}
UPDATE_MODES = {"Off", "Initial", "Recreate", "InPlaceOrRecreate", "InPlace"}
CONTROLLED_RESOURCES = {"cpu", "memory"}
BOUND_ENVIRONMENT = {
    ("minAllowed", "cpu"): ("POLYAD_VPA_MIN_CPU_MILLICORES", Decimal(1000)),
    ("maxAllowed", "cpu"): ("POLYAD_VPA_MAX_CPU_MILLICORES", Decimal(1000)),
    ("minAllowed", "memory"): ("POLYAD_VPA_MIN_MEMORY_BYTES", Decimal(1)),
    ("maxAllowed", "memory"): ("POLYAD_VPA_MAX_MEMORY_BYTES", Decimal(1)),
}


def _quantity(value: Any, path: str) -> Decimal:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{path} must be a non-empty Kubernetes quantity string")
    try:
        parsed = Decimal(parse_quantity(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{path} is not a valid Kubernetes quantity") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{path} must be finite and non-negative")
    return parsed


def compile_vertical_pod_autoscaler(spec: dict[str, Any], targets: Mapping[str, str]) -> dict[str, Any]:
    """
    Validate a native VPA spec and preserve it for the external VPA controller.

    Args:
        spec (dict[str, Any]): Native VPA spec after graph-name reference resolution.
        targets (Mapping[str, str]): Generated names mapped to eligible Daemon controller kinds.

    Returns:
        dict[str, Any]: Independent validated VPA spec.
    """
    if not isinstance(spec, dict):
        raise ValueError("VerticalPodAutoscaler spec must be an object")
    target = spec.get("targetRef")
    if not isinstance(target, dict):
        raise ValueError("VerticalPodAutoscaler spec.targetRef must be an object")
    if target.get("apiVersion") != "apps/v1" or target.get("kind") not in TARGET_KINDS:
        raise ValueError("VerticalPodAutoscaler targetRef must select an apps/v1 Deployment, StatefulSet or DaemonSet")
    if target.get("name") not in targets:
        raise ValueError("VerticalPodAutoscaler targetRef.name must reference a Daemon node in the same graph")
    if target.get("kind") != targets[target["name"]]:
        raise ValueError("VerticalPodAutoscaler targetRef.kind must match the Daemon controller")

    # Require an explicit lifecycle mode: ambiguous defaults could introduce Pod
    # replacement into a workload whose adaptation plan expects in-place changes.
    update_policy = spec.get("updatePolicy", {})
    if not isinstance(update_policy, dict):
        raise ValueError("VerticalPodAutoscaler spec.updatePolicy must be an object")
    update_mode = update_policy.get("updateMode", "Auto")
    if update_mode == "Auto":
        raise ValueError("VerticalPodAutoscaler updateMode Auto is ambiguous; choose Off, Initial, Recreate, InPlaceOrRecreate or InPlace")
    if update_mode not in UPDATE_MODES:
        raise ValueError(f"unsupported VerticalPodAutoscaler updateMode: {update_mode}")

    resource_policy = spec.get("resourcePolicy", {})
    if not isinstance(resource_policy, dict):
        raise ValueError("VerticalPodAutoscaler spec.resourcePolicy must be an object")
    policies = resource_policy.get("containerPolicies", [])
    if not isinstance(policies, list):
        raise ValueError("VerticalPodAutoscaler resourcePolicy.containerPolicies must be an array")
    containers: set[str] = set()
    for index, policy in enumerate(policies):
        path = f"VerticalPodAutoscaler resourcePolicy.containerPolicies[{index}]"
        if not isinstance(policy, dict):
            raise ValueError(f"{path} must be an object")
        container = policy.get("containerName")
        if not isinstance(container, str) or not container:
            raise ValueError(f"{path}.containerName must be a non-empty string")
        if container in containers:
            raise ValueError(f"{path}.containerName must be unique")
        containers.add(container)
        if policy.get("mode", "Auto") not in {"Auto", "Off"}:
            raise ValueError(f"{path}.mode must be Auto or Off")
        if policy.get("controlledValues", "RequestsAndLimits") not in {"RequestsOnly", "RequestsAndLimits"}:
            raise ValueError(f"{path}.controlledValues must be RequestsOnly or RequestsAndLimits")
        controlled = policy.get("controlledResources", ["cpu", "memory"])
        if not isinstance(controlled, list) or not controlled or any(item not in CONTROLLED_RESOURCES for item in controlled):
            raise ValueError(f"{path}.controlledResources may contain only cpu and memory")
        if len(controlled) != len(set(controlled)):
            raise ValueError(f"{path}.controlledResources must not contain duplicates")

        # Compare normalized quantities, not strings: equivalent Kubernetes CPU
        # and memory units must produce the same ordered resource interval.
        parsed: dict[str, dict[str, Decimal]] = {}
        for bound in ("minAllowed", "maxAllowed"):
            values = policy.get(bound, {})
            if not isinstance(values, dict) or any(resource not in CONTROLLED_RESOURCES for resource in values):
                raise ValueError(f"{path}.{bound} must map cpu and/or memory to quantities")
            parsed[bound] = {resource: _quantity(value, f"{path}.{bound}.{resource}") for resource, value in values.items()}
        for resource in parsed["minAllowed"].keys() & parsed["maxAllowed"].keys():
            if parsed["minAllowed"][resource] > parsed["maxAllowed"][resource]:
                raise ValueError(f"{path}.minAllowed.{resource} must not exceed maxAllowed.{resource}")
    return copy.deepcopy(spec)


def inject_vertical_environment(pod: dict[str, Any], spec: dict[str, Any]) -> None:
    """
    Project a target container's VPA bounds beside its live Downward API resources.

    Args:
        pod (dict[str, Any]): Mutable application Pod template.
        spec (dict[str, Any]): Validated VPA spec targeting the Pod controller.

    Returns:
        None: Matching regular containers are updated in place.
    """
    policies = spec.get("resourcePolicy", {}).get("containerPolicies", [])

    # A named container policy wins over the wildcard. Project only resources
    # controlled by that policy, using the SDK's integer CPU/memory units.
    wildcard = next((item for item in policies if item["containerName"] == "*"), None)
    update_mode = spec.get("updatePolicy", {}).get("updateMode", "")
    managed_names = {"POLYAD_VPA_UPDATE_MODE", *(item[0] for item in BOUND_ENVIRONMENT.values())}
    for container in pod["spec"].get("containers", []):
        policy = next((item for item in policies if item["containerName"] == container.get("name")), wildcard)
        values = {"POLYAD_VPA_UPDATE_MODE": update_mode}
        if policy is not None and policy.get("mode", "Auto") != "Off":
            controlled = set(policy.get("controlledResources", ["cpu", "memory"]))
            for (bound, resource), (name, multiplier) in BOUND_ENVIRONMENT.items():
                if resource in controlled and resource in policy.get(bound, {}):
                    value = _quantity(policy[bound][resource], f"VerticalPodAutoscaler {bound}.{resource}") * multiplier
                    values[name] = str(int(value))

        # Replace managed names instead of appending duplicates or retaining
        # stale bounds from an earlier policy compilation.
        projected = [{"name": name, "value": value} for name, value in values.items()]
        container["env"] = projected + [item for item in container.get("env", []) if item["name"] not in managed_names]
