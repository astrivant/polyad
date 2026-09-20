"""
Describe application endpoints and their Kubernetes and Istio port contracts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from polyad_types.networking.access import NetworkPort

if TYPE_CHECKING:
    from polyad_types import ServiceEndpoint

__all__ = ("WorkloadEndpoint",)


# TLS here belongs to the application. Istio can separately encrypt any of these
# TCP connections with mesh mTLS, including the plaintext application protocols.
_PORTS = {
    "http": 80,
    "https": 443,
    "ws": 80,
    "wss": 443,
    "grpc": 50051,
    "grpcs": 50051,
    "amqp": 5672,
    "amqps": 5671,
    "redis": 6379,
    "rediss": 6379,
    "tcp": None,
    "tls": None,
}
_ISTIO = {
    "http": "http",
    "https": "https",
    "ws": "http",
    "wss": "https",
    "grpc": "grpc",
    "grpcs": "tls",
    "amqp": "tcp",
    "amqps": "tls",
    "redis": "tcp",
    "rediss": "tls",
    "tcp": "tcp",
    "tls": "tls",
}


@dataclass(frozen=True)
class WorkloadEndpoint:
    """
    Associate a logical destination with an application-supplied network address.

    Discovery does not attest that this URI belongs to the supplied identity.
    Deployment configuration must make that association; network policies and
    mesh identities remain the enforcement boundary. Credentials are never URLs.

    Attributes:
        identity (ServiceEndpoint): Exact destination graph incarnation and node.
        uri (str): Explicit application URI; TCP and TLS require a port.
        target_port (int | None): Pod port when a Kubernetes Service remaps the URI port.
    """

    identity: ServiceEndpoint
    uri: str
    target_port: int | None = None

    def __post_init__(self) -> None:
        """
        Reject ambiguous addresses and hidden authentication or client options.

        Returns:
            None: Invalid endpoints raise ValueError before any network activity.
        """
        if not isinstance(self.uri, str) or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in self.uri):
            raise ValueError("workload URI must not contain whitespace or control characters")
        parsed = urlsplit(self.uri)
        if parsed.scheme not in _PORTS or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError("use a supported workload scheme and host, without URI credentials")
        if parsed.query or parsed.fragment or "?" in self.uri or "#" in self.uri or "%" in parsed.netloc or "\\" in self.uri:
            raise ValueError("workload URI must not contain query options, fragments or ambiguous hosts")
        if parsed.scheme not in {"http", "https", "ws", "wss"} and parsed.path not in {"", "/"}:
            raise ValueError("pass AMQP virtual hosts and Redis databases as method arguments, not URI paths")
        port = parsed.port if parsed.port is not None else _PORTS[parsed.scheme]
        if port is None or not 1 <= port <= 65535 or parsed.netloc.endswith(":"):
            raise ValueError("workload URI requires a valid port; TCP and TLS have no default")
        if self.target_port is not None and (type(self.target_port) is not int or not 1 <= self.target_port <= 65535):
            raise ValueError("target_port must be an integer from 1 through 65535")

    @property
    def scheme(self) -> str:
        """
        Read the validated application protocol.

        Returns:
            str: Lowercase URI scheme.
        """
        return urlsplit(self.uri).scheme

    @property
    def host(self) -> str:
        """
        Read the host used for dialing and application TLS verification.

        Returns:
            str: Hostname or unbracketed IP address.
        """
        return str(urlsplit(self.uri).hostname)

    @property
    def port(self) -> int:
        """
        Read the dialed port, which can differ from the destination Pod port.

        Returns:
            int: Explicit URI port or protocol default.
        """
        parsed = urlsplit(self.uri)
        return int(parsed.port or _PORTS[parsed.scheme] or 0)

    @property
    def network_port(self) -> NetworkPort:
        """
        Describe the destination Pod port for graph edges and connection requests.

        Returns:
            NetworkPort: TCP grant port after any Service-to-Pod remapping.
        """
        return NetworkPort(port=self.target_port or self.port, protocol="TCP")

    def service_port(self, name: str = "workload") -> dict[str, str | int]:
        """
        Emit explicit Istio protocol selection for a Kubernetes Service port.

        Args:
            name (str): DNS-label suffix; the protocol prefix is supplied automatically.

        Returns:
            dict[str, str | int]: A Service spec.ports entry with appProtocol and targetPort.

        Raises:
            ValueError: The generated Service port name is not a valid DNS label.
        """
        protocol = _ISTIO[self.scheme]
        label = f"{protocol}-{name}"
        if not re.fullmatch(r"[a-z]([-a-z0-9]*[a-z0-9])?", label) or len(label) > 63:
            raise ValueError("Service port name must be a DNS label of at most 63 characters")
        return {"name": label, "port": self.port, "targetPort": self.network_port.port, "protocol": "TCP", "appProtocol": protocol}
