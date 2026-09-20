"""
Read the operator's projected workload context without importing operator code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING

from polyad_sdk.runtime.environment import env as sdk_environment
from polyad_types import ServiceEndpoint
from polyad_types.events.models import EventIdentity

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Literal

__all__ = (
    "ContainerResources",
    "PodContext",
    "VPAConstraints",
    "WorkloadContext",
)


@dataclass(frozen=True)
class ContainerResources:
    """
    Describe per-container resource selectors in their projected integer units.

    Limits can represent node allocatable capacity when Kubernetes supplies a
    fallback. Default budgets therefore require a positive request and respect a
    smaller positive limit. These values are startup snapshots, not live usage.

    Attributes:
        cpu_request_millicores (int | None): Requested CPU; None when unprojected.
        cpu_limit_millicores (int | None): Projected CPU limit or Kubernetes fallback.
        memory_request_bytes (int | None): Requested memory; None when unprojected.
        memory_limit_bytes (int | None): Projected memory limit or Kubernetes fallback.
    """

    cpu_request_millicores: int | None = None
    cpu_limit_millicores: int | None = None
    memory_request_bytes: int | None = None
    memory_limit_bytes: int | None = None

    def __post_init__(self) -> None:
        """
        Preserve integer units for explicit configuration as well as parsed projections.

        Returns:
            None: Invalid resource values fail before becoming an application budget.

        Raises:
            ValueError: A supplied resource value is not a nonnegative integer.
        """

        # bool is an int subclass in Python, but True must not become a one-unit
        # resource allowance; require the exact integer type for these selectors.
        for item in fields(self):
            value = getattr(self, item.name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{item.name} must be a nonnegative integer or None")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> ContainerResources:
        """
        Parse CPU and memory selectors without treating missing values as free capacity.

        Args:
            environ (Mapping[str, str] | None): Explicit environment snapshot; defaults to the SDK's exported env.

        Returns:
            ContainerResources: Typed container requests and limits.

        Raises:
            ValueError: A supplied selector is not a nonnegative decimal integer.
        """
        env = sdk_environment if environ is None else environ
        return cls(**{item.name: _integer(env, "POLYAD_" + item.name.upper()) for item in fields(cls)})

    def budget(self, resource: Literal["cpu", "memory"]) -> int | None:
        """
        Default a local application budget to a positive request within its projected limit.

        Args:
            resource (Literal['cpu', 'memory']): CPU in millicores or memory in bytes.

        Returns:
            int | None: Conservative startup allowance, or None without a positive request.

        Raises:
            ValueError: The requested resource is unsupported.
        """
        if resource == "cpu":
            request, limit = self.cpu_request_millicores, self.cpu_limit_millicores
        elif resource == "memory":
            request, limit = self.memory_request_bytes, self.memory_limit_bytes
        else:
            raise ValueError("resource must be cpu or memory")

        # A Kubernetes limit can be a node-capacity fallback. Without an explicit
        # positive request, do not treat that value as promised application capacity.
        if request is None or request <= 0:
            return None
        return min(request, limit) if limit is not None and limit > 0 else request


@dataclass(frozen=True)
class VPAConstraints:
    """
    Expose the VPA bounds Polyad compiled for this application container.

    Attributes:
        update_mode (str): Native VPA update mode, or empty when no VPA targets the container.
        min_cpu_millicores (int | None): Lower CPU bound in millicores.
        max_cpu_millicores (int | None): Upper CPU bound in millicores.
        min_memory_bytes (int | None): Lower memory bound in bytes.
        max_memory_bytes (int | None): Upper memory bound in bytes.
    """

    update_mode: str = ""
    min_cpu_millicores: int | None = None
    max_cpu_millicores: int | None = None
    min_memory_bytes: int | None = None
    max_memory_bytes: int | None = None

    def __post_init__(self) -> None:
        """
        Reject malformed or inverted projected policy intervals.

        Returns:
            None: Valid bounds remain immutable.
        """
        if self.update_mode not in {"", "Off", "Initial", "Recreate", "InPlaceOrRecreate", "InPlace"}:
            raise ValueError("POLYAD_VPA_UPDATE_MODE is unsupported")
        for item in fields(self):
            if item.name == "update_mode":
                continue
            value = getattr(self, item.name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{item.name} must be a nonnegative integer or None")

        # Validate each interval only after its units and individual values are
        # known, so a contradictory minimum/maximum cannot reach an adaptation.
        for resource in ("cpu", "memory"):
            minimum = getattr(self, f"min_{resource}_{'millicores' if resource == 'cpu' else 'bytes'}")
            maximum = getattr(self, f"max_{resource}_{'millicores' if resource == 'cpu' else 'bytes'}")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError(f"VPA minimum {resource} must not exceed its maximum")

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> VPAConstraints:
        """
        Parse projected bounds in the same units as :class:`ContainerResources`.

        Args:
            environ (Mapping[str, str] | None): Explicit environment; defaults to the SDK snapshot.

        Returns:
            VPAConstraints: Typed application policy interval.
        """
        env = sdk_environment if environ is None else environ
        return cls(
            update_mode=env.get("POLYAD_VPA_UPDATE_MODE", ""),
            min_cpu_millicores=_integer(env, "POLYAD_VPA_MIN_CPU_MILLICORES"),
            max_cpu_millicores=_integer(env, "POLYAD_VPA_MAX_CPU_MILLICORES"),
            min_memory_bytes=_integer(env, "POLYAD_VPA_MIN_MEMORY_BYTES"),
            max_memory_bytes=_integer(env, "POLYAD_VPA_MAX_MEMORY_BYTES"),
        )

    def clamp(self, resource: Literal["cpu", "memory"], value: int) -> int:
        """
        Clamp an application resource decision to the configured VPA interval.

        Args:
            resource (Literal['cpu', 'memory']): CPU in millicores or memory in bytes.
            value (int): Proposed nonnegative value in the selected resource unit.

        Returns:
            int: Value constrained to any projected lower and upper bounds.
        """
        if type(value) is not int or value < 0:
            raise ValueError("resource value must be a nonnegative integer")
        if resource not in {"cpu", "memory"}:
            raise ValueError("resource must be cpu or memory")

        # These are policy bounds, not a claim that the live cgroup has already
        # received the proposed allocation. Read container_metrics for actual limits.
        unit = "millicores" if resource == "cpu" else "bytes"
        minimum = getattr(self, f"min_{resource}_{unit}")
        maximum = getattr(self, f"max_{resource}_{unit}")
        return max(minimum or 0, min(value, maximum if maximum is not None else value))


@dataclass(frozen=True)
class PodContext:
    """
    Identify this concrete Pod and its actual placement from the Downward API.

    Attributes:
        name (str): Concrete Pod name.
        uid (str): Pod incarnation.
        namespace (str): Pod namespace.
        ip (str): Primary Pod address.
        ips (str): Kubelet-projected dual-stack addresses, retained verbatim.
        host_ip (str): Primary node address.
        host_ips (str): Kubelet-projected node addresses, retained verbatim.
        node_name (str): Actual Kubernetes node; distinct from the graph's logical node.
        service_account (str): Pod service-account name; does not grant SDK credentials.
        cluster (str): Physical placement cluster when projected, independently of root authority.
    """

    name: str = field(default="", metadata={"env": "POLYAD_POD_NAME"})
    uid: str = field(default="", metadata={"env": "POLYAD_POD_UID"})
    namespace: str = field(default="", metadata={"env": "POLYAD_POD_NAMESPACE"})
    ip: str = field(default="", metadata={"env": "POLYAD_POD_IP"})
    ips: str = field(default="", metadata={"env": "POLYAD_POD_IPS"})
    host_ip: str = field(default="", metadata={"env": "POLYAD_HOST_IP"})
    host_ips: str = field(default="", metadata={"env": "POLYAD_HOST_IPS"})
    node_name: str = field(default="", metadata={"env": "POLYAD_KUBERNETES_NODE_NAME"})
    service_account: str = field(default="", metadata={"env": "POLYAD_SERVICE_ACCOUNT_NAME"})
    cluster: str = field(default="", metadata={"env": "POLYAD_POD_CLUSTER"})


@dataclass(frozen=True)
class WorkloadContext:
    """
    Expose the operator's noncredential startup context to application strategies.

    Graph identity selects the event scope. Ancestry and request identities are
    correlation context, not permission to read parent graphs. Credentials are
    read separately by the service's clients and never retained in this object.

    Attributes:
        identity (ServiceEndpoint): Exact containing graph and logical node.
        root (EventIdentity | None): Outermost controlling graph in the same namespace.
        ancestry (tuple[EventIdentity, ...]): Verified-at-compilation root-to-containing chain.
        pod (PodContext): Concrete execution identity and actual placement.
        resources (ContainerResources): Projected container requests and limits.
        vpa (VPAConstraints): Optional VPA policy bounds for this application container.
        definition (EventIdentity | None): Reusable Workload or Daemon definition.
        definition_generation (int | None): Definition generation at compilation.
        node_id (str): Composition node ID, defaulting to the logical node name.
        node_path (str): Hierarchical audit path.
        runtime_node_name (str): Execution key, distinct for parallel activations.
        resource_name (str): Native controller name.
        resource_kind (str): Native Job, Deployment or StatefulSet kind.
        request_id (str): Originating composition request ID.
        composition_uid (str): Persisted composition receipt UID.
        activation_id (str): Current or enclosing activation receipt ID.
        activation_uid (str): Activation receipt UID.
        api_url (str): Enabled composition/action endpoint, or empty.
        events_url (str): Enabled event endpoint, or empty.
        metrics_url (str): Enabled metrics endpoint, or empty.
        connections_url (str): Enabled temporary-connection endpoint, or empty.
    """

    identity: ServiceEndpoint
    root: EventIdentity | None = None
    ancestry: tuple[EventIdentity, ...] = ()
    pod: PodContext = field(default_factory=PodContext)
    resources: ContainerResources = field(default_factory=ContainerResources)
    vpa: VPAConstraints = field(default_factory=VPAConstraints)
    definition: EventIdentity | None = None
    definition_generation: int | None = None
    node_id: str = field(default="", metadata={"env": "POLYAD_NODE_ID"})
    node_path: str = field(default="", metadata={"env": "POLYAD_NODE_PATH"})
    runtime_node_name: str = field(default="", metadata={"env": "POLYAD_RUNTIME_NODE_NAME"})
    resource_name: str = field(default="", metadata={"env": "POLYAD_RESOURCE_NAME"})
    resource_kind: str = field(default="", metadata={"env": "POLYAD_RESOURCE_KIND"})
    request_id: str = field(default="", metadata={"env": "POLYAD_REQUEST_ID"})
    composition_uid: str = field(default="", metadata={"env": "POLYAD_COMPOSITION_UID"})
    activation_id: str = field(default="", metadata={"env": "POLYAD_ACTIVATION_ID"})
    activation_uid: str = field(default="", metadata={"env": "POLYAD_ACTIVATION_UID"})
    api_url: str = field(default="", metadata={"env": "POLYAD_API_URL"})
    events_url: str = field(default="", metadata={"env": "POLYAD_EVENTS_URL"})
    metrics_url: str = field(default="", metadata={"env": "POLYAD_METRICS_URL"})
    connections_url: str = field(default="", metadata={"env": "POLYAD_CONNECTIONS_URL"})

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None, *, cluster: str = "") -> WorkloadContext:
        """
        Snapshot the complete projected workload contract with exact graph identity.

        Args:
            environ (Mapping[str, str] | None): Explicit environment; defaults to the SDK's exported import-time snapshot.
            cluster (str): Registered event authority cluster; empty retains the endpoint's local scope.

        Returns:
            WorkloadContext: Immutable typed context with empty or None optional fields.

        Raises:
            KeyError: A required containing-graph or logical-node field is absent.
            ValueError: Projected ancestry, resource numbers or partial optional identities are invalid.
        """
        env = dict(sdk_environment if environ is None else environ)
        identity = ServiceEndpoint(
            cluster,
            env["POLYAD_GRAPH_NAMESPACE"],
            env["POLYAD_GRAPH_KIND"],
            env["POLYAD_GRAPH_NAME"],
            env["POLYAD_GRAPH_UID"],
            env["POLYAD_NODE_NAME"],
        )

        # Correlation ancestry is separate from authority: retain it for context
        # without granting access to a parent graph or a different namespace.
        root = _reference(env, "ROOT_GRAPH", identity.namespace)
        ancestry = _ancestry(env, identity, root)
        strings = {item.name: env.get(item.metadata["env"], "") for item in fields(cls) if "env" in item.metadata}
        strings["node_id"] = strings["node_id"] or identity.node
        strings["runtime_node_name"] = strings["runtime_node_name"] or identity.node
        pod = PodContext(**{item.name: env.get(item.metadata["env"], "") for item in fields(PodContext)})
        return cls(
            identity=identity,
            root=root,
            ancestry=ancestry,
            pod=pod,
            resources=ContainerResources.from_environment(env),
            vpa=VPAConstraints.from_environment(env),
            definition=_reference(env, "DEFINITION", identity.namespace),
            definition_generation=_integer(env, "POLYAD_DEFINITION_GENERATION"),
            **strings,
        )


def _integer(env: Mapping[str, str], key: str) -> int | None:
    """
    Parse one optional decimal selector without echoing environment values in errors.

    Args:
        env (Mapping[str, str]): Startup environment.
        key (str): Public selector name.

    Returns:
        int | None: Nonnegative integer or None for absent/empty context.

    Raises:
        ValueError: The selector has invalid syntax or excessive size.
    """
    value = env.get(key, "")
    if not value:
        return None

    # Bound parsing work and report only the key, never the environment value.
    if len(value) > 64 or not value.isascii() or not value.isdecimal():
        raise ValueError(f"{key} must be a nonnegative decimal integer")
    return int(value)


def _reference(env: Mapping[str, str], prefix: str, namespace: str) -> EventIdentity | None:
    """
    Read an optional resource identity while rejecting an ambiguous partial projection.

    Args:
        env (Mapping[str, str]): Startup environment.
        prefix (str): Projected resource family.
        namespace (str): Containing graph namespace.

    Returns:
        EventIdentity | None: Resource incarnation or None when completely absent.

    Raises:
        ValueError: Only part of the optional resource identity was supplied.
    """
    values = {key: env.get(f"POLYAD_{prefix}_{key.upper()}", "") for key in ("kind", "name", "uid")}
    if not any(values.values()):
        return None

    # Name alone does not identify an incarnation. Either omit the reference
    # entirely or supply kind, name and UID as one complete identity.
    if not all(values.values()):
        raise ValueError(f"POLYAD_{prefix} requires kind, name and UID together")
    return EventIdentity(namespace=namespace, **values)


def _ancestry(env: Mapping[str, str], identity: ServiceEndpoint, root: EventIdentity | None) -> tuple[EventIdentity, ...]:
    """
    Validate projected ancestry against the supplied containing and root identities.

    Args:
        env (Mapping[str, str]): Startup environment.
        identity (ServiceEndpoint): Containing graph and node.
        root (EventIdentity | None): Explicit projected root, when available.

    Returns:
        tuple[EventIdentity, ...]: Immutable root-to-containing chain, or empty when unprojected.

    Raises:
        ValueError: The chain is malformed, excessive, cyclic or inconsistent with identity.
    """
    raw = env.get("POLYAD_GRAPH_ANCESTRY", "")
    if not raw:
        return ()
    if len(raw) > 65536:
        raise ValueError("projected graph ancestry exceeds 64 KiB")
    try:
        entries = json.loads(raw)
    except ValueError:
        raise ValueError("projected graph ancestry must be a JSON array") from None
    keys = {"kind", "namespace", "name", "uid"}
    if (
        not isinstance(entries, list)
        or not 1 <= len(entries) <= 32
        or any(
            not isinstance(entry, dict)
            or set(entry) != keys
            or any(not isinstance(value, str) or not value for value in entry.values())
            or entry["kind"] not in {"Graph", "PolyGraph", "ReplicaGroup"}
            or entry["namespace"] != identity.namespace
            for entry in entries
        )
    ):
        raise ValueError("projected graph ancestry requires 1 through 32 complete local graph identities")

    # Verify the chain's endpoints and uniqueness after validating each record.
    # Otherwise a plausible-looking chain could point at the wrong workload.
    result = tuple(EventIdentity(**entry) for entry in entries)
    containing = EventIdentity(kind=identity.kind, namespace=identity.namespace, name=identity.graph, uid=identity.graphUid)
    if result[-1] != containing or (root is not None and result[0] != root) or len({item.uid for item in result}) != len(result):
        raise ValueError("projected ancestry must match root and containing graph without repeated UIDs")
    return result
