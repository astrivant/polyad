"""
Describe bounded replication of reusable workload and graph definitions.
"""

from __future__ import annotations

from typing import Any, Literal

from attrs import field, frozen


@frozen
class ReplicaTemplate:
    """
    Select the reusable definition instantiated at each stable replica ordinal.

    Attributes:
        kind (Literal['Workload', 'Daemon', 'Ephemeral', 'Resource', 'Graph', 'PolyGraph', 'ReplicaGroup']):
            Executable definition or resource abstraction.
        ref (str): Namespaced definition name.
    """

    kind: Literal["Workload", "Daemon", "Ephemeral", "Resource", "Graph", "PolyGraph", "ReplicaGroup"]
    ref: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$"}})


@frozen
class ReplicaSource:
    """
    Bind a generated instance to its reusable group's scaling intent.

    Attributes:
        name (str): Reusable ReplicaGroup name.
        uid (str): Incarnation fence for the reusable group.
    """

    name: str = field(metadata={"schema": {"minLength": 1, "maxLength": 63}})
    uid: str = field(metadata={"schema": {"minLength": 1, "maxLength": 128}})


@frozen
class Replication:
    """
    Bound independent copies while preserving stable ordinals and graph admission.

    Attributes:
        template (ReplicaTemplate): Definition replicated by this group.
        replicas (int): Requested number of copies, exposed through the scale subresource.
        minReplicas (int): Inclusive lower bound, including zero.
        maxReplicas (int): Inclusive upper bound, at most 256 copies per group.
        templateOnly (bool): Reusable group controlling all inheriting instances.
        replicaSource (ReplicaSource | None): Compiler-provided reference to shared replica intent.
        inheritReplicas (bool): Whether a generated instance follows its reusable group.
        suspend (bool): Drain execution until resumed.
        placement (dict[str, Any] | None): Placement inherited by all copies.
        rules (tuple[str, ...]): Structural restrictions for every replicated graph.
        network (dict[str, Any] | None): Traffic restrictions across this group.
        capacity (dict[str, Any] | None): Advance capacity policy for contained work.
        shutdownPolicy (str | None): Graceful termination policy.
        activation (dict[str, Any] | None): Optional pulse policy when referenced by another graph.
    """

    template: ReplicaTemplate
    replicas: int = field(default=1, metadata={"schema": {"minimum": 0, "maximum": 256}})
    minReplicas: int = field(default=0, metadata={"schema": {"minimum": 0, "maximum": 256}})
    maxReplicas: int = field(default=32, metadata={"schema": {"minimum": 1, "maximum": 256}})
    templateOnly: bool = False
    replicaSource: ReplicaSource | None = None
    inheritReplicas: bool = True
    suspend: bool = False
    placement: dict[str, Any] | None = None
    rules: tuple[str, ...] = ()
    network: dict[str, Any] | None = None
    capacity: dict[str, Any] | None = None
    shutdownPolicy: str | None = None
    activation: dict[str, Any] | None = None

    def __attrs_post_init__(self) -> None:
        """
        Reject unbounded or contradictory replica counts.

        Returns:
            None: Invalid counts raise before admission.
        """
        if any(type(value) is not int for value in (self.replicas, self.minReplicas, self.maxReplicas)):
            raise ValueError("replica bounds must be integers")
        if not 0 <= self.minReplicas <= self.replicas <= self.maxReplicas <= 256 or self.maxReplicas < 1:
            raise ValueError("replicas must lie within minReplicas and maxReplicas, capped at 256")


def replica_topology(spec: dict[str, Any]) -> dict[str, Any]:
    """
    Project replication into ordinary graph vertices for scheduling and mathematical rules.

    Args:
        spec (dict[str, Any]): ReplicaGroup specification, including its effective count.

    Returns:
        dict[str, Any]: Persistent scheduling topology with one vertex per stable ordinal.
    """
    from polyad.graph.topology import converter

    policy = converter.structure(spec, Replication)
    result = {
        key: spec[key]
        for key in ("suspend", "placement", "rules", "network", "capacity", "shutdownPolicy", "templateOnly")
        if key in spec and spec[key] is not None
    }
    return {
        **result,
        "mode": "persistent",
        "slots": max(1, policy.maxReplicas),
        "nodes": [
            {"name": f"replica-{index}", "kind": policy.template.kind, "ref": policy.template.ref} for index in range(policy.replicas)
        ],
    }
