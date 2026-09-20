"""
Compile persistent workloads with selectable Kubernetes controllers and native storage.
"""

from __future__ import annotations

import copy
import re
from typing import TYPE_CHECKING

from polyad_types import resources as asts

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "STATEFUL_OPTIONS",
    "compile_daemon",
    "execution_pod",
)


STATEFUL_OPTIONS = frozenset(
    {
        "serviceName",
        "volumeClaimTemplates",
        "podManagementPolicy",
        "updateStrategy",
        "persistentVolumeClaimRetentionPolicy",
        "minReadySeconds",
        "revisionHistoryLimit",
        "ordinals",
    }
)


def compile_daemon(spec: dict[str, Any], selector: dict[str, str]) -> asts.DeploymentSpec | asts.StatefulSetSpec | asts.DaemonSetSpec:
    """
    Preserve application storage while reserving controller identity fields for Polyad.

    Args:
        spec (dict[str, Any]): Resolved Daemon definition with its configured Pod template.
        selector (dict[str, str]): Operator-assigned labels identifying this execution's Pods.

    Returns:
        asts.DeploymentSpec | asts.StatefulSetSpec | asts.DaemonSetSpec: Typed native controller specification.
    """
    kind = spec.get("controller", "Deployment")
    replicas = spec.get("replicas", 1)
    if type(replicas) is not int or replicas < 1:
        raise ValueError("daemon replicas must be a positive integer")
    if type(spec.get("reloadOnSecretChange", False)) is not bool:
        raise ValueError("daemon reloadOnSecretChange must be a boolean")

    # Polyad owns these identity-bearing fields for every controller kind;
    # controller-specific options may not silently override their values.
    common = {"template": spec["template"], "selector": {"matchLabels": selector}, "replicas": replicas}
    if kind == "DaemonSet":
        if replicas != 1 or "statefulSet" in spec or spec.get("activation"):
            raise ValueError("DaemonSet uses node eligibility, requires replicas: 1, and does not support statefulSet or activation")
        common.pop("replicas")
        return asts.converter.structure(common, asts.DaemonSetSpec)
    if kind == "Deployment":
        if "statefulSet" in spec:
            raise ValueError("statefulSet options require controller: StatefulSet")
        return asts.converter.structure({**common, "strategy": {"type": "Recreate"}}, asts.DeploymentSpec)
    if kind != "StatefulSet":
        raise ValueError("daemon controller must be Deployment, StatefulSet or DaemonSet")
    options = spec.get("statefulSet", {})
    if not isinstance(options, dict) or set(options) - STATEFUL_OPTIONS:
        raise ValueError("unknown StatefulSet options; replicas, selector and template are managed by Polyad")
    service = options.get("serviceName")
    if not isinstance(service, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", service):
        raise ValueError("StatefulSet requires statefulSet.serviceName naming a headless Service")
    if options.get("podManagementPolicy", "OrderedReady") not in {"OrderedReady", "Parallel"}:
        raise ValueError("StatefulSet podManagementPolicy must be OrderedReady or Parallel")
    for field in ("minReadySeconds", "revisionHistoryLimit"):
        value = options.get(field, 0)
        if type(value) is not int or value < 0:
            raise ValueError(f"StatefulSet {field} must be a nonnegative integer")
    ordinals = options.get("ordinals", {})
    if (
        not isinstance(ordinals, dict)
        or set(ordinals) - {"start"}
        or type(ordinals.get("start", 0)) is not int
        or ordinals.get("start", 0) < 0
    ):
        raise ValueError("StatefulSet ordinals.start must be a nonnegative integer")
    strategy = options.get("updateStrategy", {})
    if (
        not isinstance(strategy, dict)
        or set(strategy) - {"type", "rollingUpdate"}
        or strategy.get("type", "RollingUpdate") not in {"RollingUpdate", "OnDelete"}
    ):
        raise ValueError("StatefulSet updateStrategy must be RollingUpdate or OnDelete")
    rolling = strategy.get("rollingUpdate", {})
    if not isinstance(rolling, dict) or set(rolling) - {"partition", "maxUnavailable"}:
        raise ValueError("invalid StatefulSet rollingUpdate options")
    if type(rolling.get("partition", 0)) is not int or rolling.get("partition", 0) < 0:
        raise ValueError("StatefulSet rollingUpdate.partition must be a nonnegative integer")
    if strategy.get("type") == "OnDelete" and rolling:
        raise ValueError("OnDelete does not accept rollingUpdate options")
    retention = options.get("persistentVolumeClaimRetentionPolicy", {})
    if (
        not isinstance(retention, dict)
        or set(retention) - {"whenDeleted", "whenScaled"}
        or any(value not in {"Retain", "Delete"} for value in retention.values())
    ):
        raise ValueError("StatefulSet claim retention values must be Retain or Delete")
    claims = options.get("volumeClaimTemplates", [])
    if not isinstance(claims, list):
        raise ValueError("StatefulSet volumeClaimTemplates must be a list")

    # Claim templates create volumes by name, so reject collisions with either
    # another claim template or an explicitly configured Pod volume.
    names = set()
    volumes = {volume["name"] for volume in spec["template"]["spec"].get("volumes", [])}
    for claim in claims:
        if not isinstance(claim, dict) or not isinstance(claim.get("metadata"), dict) or not isinstance(claim.get("spec"), dict):
            raise ValueError("each volumeClaimTemplate requires metadata and a native PVC spec")
        name = claim["metadata"].get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", name):
            raise ValueError("volumeClaimTemplate metadata.name must be a DNS label")
        if name in names or name in volumes:
            raise ValueError("volumeClaimTemplate names must be unique and must not shadow Pod volumes")
        names.add(name)
    return asts.converter.structure({**copy.deepcopy(options), **common}, asts.StatefulSetSpec)


def execution_pod(document: dict[str, Any]) -> dict[str, Any]:
    """
    Materialize the first StatefulSet ordinal's volumes for Pod admission dry runs.

    Args:
        document (dict[str, Any]): Compiled Job, Deployment or StatefulSet document.

    Returns:
        dict[str, Any]: Independent Pod template including implicit StatefulSet claim volumes.
    """
    pod: dict[str, Any] = copy.deepcopy(document["spec"]["template"])

    # Model the starting StatefulSet ordinal's actual PVC names when evaluating
    # its execution Pod; claim templates are not ordinary inline Pod volumes.
    if document["kind"] == "StatefulSet":
        ordinal = document["spec"].get("ordinals", {}).get("start", 0)
        for claim in document["spec"].get("volumeClaimTemplates", []):
            name = claim["metadata"]["name"]
            pod["spec"].setdefault("volumes", []).append(
                {
                    "name": name,
                    "persistentVolumeClaim": {"claimName": f"{name}-{document['metadata']['name']}-{ordinal}"},
                }
            )
    return pod
