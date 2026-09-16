"""
Describe service and operator credentials without embedding secret values.
"""

from __future__ import annotations

import re
from enum import StrEnum
from urllib.parse import urlsplit

from attrs import frozen


@frozen
class GraphAccess:
    """
    Grant access to one graph incarnation and optionally its descendants.

    Attributes:
        name (str): Boundary name in its destination namespace.
        namespace (str): Destination namespace.
        kind (str): Graph, PolyGraph or ReplicaGroup.
        cluster (str): Registered cluster identity; empty selects the local cluster.
        uid (str): Optional incarnation fence; empty follows the named boundary.
        descendants (bool): Include owned child boundaries.
    """

    name: str
    namespace: str
    kind: str = "Graph"
    cluster: str = ""
    uid: str = ""
    descendants: bool = True

    def __attrs_post_init__(self) -> None:
        """
        Require an explicit namespace and supported boundary identity.

        Returns:
            None: Invalid scopes raise before credentials become usable.
        """
        if not self.name or not self.namespace or self.kind not in {"Graph", "PolyGraph", "ReplicaGroup"}:
            raise ValueError("graph access requires a namespace, name and boundary kind")


@frozen
class CredentialAssignment:
    """
    Inject a Secret reference into an administrator-selected workload definition.

    Attributes:
        name (str): Reusable Workload or Daemon definition name.
        namespace (str): Namespace containing both the definition and its Secret.
        env (str): Environment variable receiving the bearer credential.
        kind (str): Workload or Daemon definition kind.
        cluster (str): Destination cluster; empty selects local execution.
        secret (str): Namespace-local Secret override; empty uses the key's existingSecret.
        containers (tuple[str, ...]): Selected containers; empty includes all ordinary containers.
    """

    name: str
    namespace: str
    env: str
    kind: str = "Daemon"
    cluster: str = ""
    secret: str = ""
    containers: tuple[str, ...] = ()

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous targets and invalid environment variable names.

        Returns:
            None: References carry no plaintext credential values.
        """
        if not self.name or not self.namespace or self.kind not in {"Daemon", "Workload"}:
            raise ValueError("credential assignments require a named Workload or Daemon and namespace")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.env):
            raise ValueError("credential assignment env must be a shell-compatible variable name")


class KeyDirection(StrEnum):
    """
    Declare which direction may use a credential.

    Attributes:
        INBOUND: Authenticate calls into Polyad.
        OUTBOUND: Authenticate calls from Polyad to configured destinations.
        BIDIRECTIONAL: Permit both directions through the same quota lane.
    """

    INBOUND = "Inbound"
    OUTBOUND = "Outbound"
    BIDIRECTIONAL = "Bidirectional"


@frozen
class APIKey:
    """
    Configure one independently limited credential lane.

    Attributes:
        name (str): Stable lane identity within its group, preserved on token rotation.
        direction (KeyDirection): Permitted authentication direction.
        existingSecret (str): Kubernetes Secret containing the bearer token.
        secretKey (str): Key within the Secret.
        requestsPerMinute (int): Combined inbound and outbound request budget across replicas.
        maxConcurrentRequests (int): Combined in-flight request budget across replicas.
        endpoints (tuple[str, ...]): Inbound APIs this credential can access.
        baseUrl (str): Pinned outbound origin and optional path prefix.
        graphs (tuple[GraphAccess, ...]): Explicit graph trees visible through events and application telemetry.
        workloads (tuple[CredentialAssignment, ...]): Administrator-approved Secret environment assignments.
    """

    name: str
    direction: KeyDirection
    existingSecret: str
    secretKey: str = "token"
    requestsPerMinute: int = 60
    maxConcurrentRequests: int = 8
    endpoints: tuple[str, ...] = ()
    baseUrl: str = ""
    graphs: tuple[GraphAccess, ...] = ()
    workloads: tuple[CredentialAssignment, ...] = ()

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous direction, unbounded quotas and destinations containing credentials.

        Returns:
            None: Valid credentials contain only references and nonsecret policy.
        """
        if not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", self.name):
            raise ValueError("API key names must be DNS labels")
        if not self.existingSecret or not self.secretKey:
            raise ValueError("API keys require a Secret reference")
        if self.direction not in set(KeyDirection):
            raise ValueError("API key direction must be Inbound, Outbound or Bidirectional")
        if any(type(value) is not int or value < 1 for value in (self.requestsPerMinute, self.maxConcurrentRequests)):
            raise ValueError("API key limits must be positive integers")
        supported = {"composition", "activations", "throughput", "events", "topology", "metrics", "observations"}
        if set(self.endpoints) - supported or len(set(self.endpoints)) != len(self.endpoints):
            raise ValueError("API key endpoints must be unique supported API names")
        if (self.direction != KeyDirection.OUTBOUND) != bool(self.endpoints):
            raise ValueError("inbound credentials require endpoints; outbound-only credentials cannot grant endpoints")
        if (self.direction != KeyDirection.INBOUND) != bool(self.baseUrl):
            raise ValueError("outbound credentials require baseUrl; inbound-only credentials cannot declare a destination")
        if self.baseUrl:
            url = urlsplit(self.baseUrl)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError("outbound baseUrl requires an HTTP(S) destination without credentials, query or fragment")
            if any(ord(char) <= 32 for char in self.baseUrl):
                raise ValueError("outbound baseUrl must not contain whitespace or control characters")


@frozen
class Authentication:
    """
    Separate arbitrary numbers of service and operator credentials.

    Attributes:
        services (tuple[APIKey, ...]): Application service credentials.
        operators (tuple[APIKey, ...]): Peer operator credentials.
    """

    services: tuple[APIKey, ...] = ()
    operators: tuple[APIKey, ...] = ()

    def __attrs_post_init__(self) -> None:
        """
        Keep lane names unique within each group.

        Returns:
            None: Equal names in different groups remain separate identities.
        """
        for entries in (self.services, self.operators):
            if len({entry.name for entry in entries}) != len(entries):
                raise ValueError("API key names must be unique within each credential group")
