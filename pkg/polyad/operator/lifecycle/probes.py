"""
Resolve Pod-only health addresses and run container probes without API clients.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

if TYPE_CHECKING:
    from typing import Any


def pod_address() -> str:
    """
    Select the primary Pod IP, with loopback reserved for local processes.

    Returns:
        str: Validated IPv4 or IPv6 address without URL brackets.

    Raises:
        ValueError: Kubernetes context lacks a Pod IP or the address is unsuitable for binding.
    """
    value = os.environ.get("POLYAD_POD_IP", "")
    if not value and os.environ.get("KUBERNETES_SERVICE_HOST"):
        raise ValueError("POLYAD_POD_IP must come from status.podIP when running in Kubernetes")
    address = ipaddress.ip_address(value or "127.0.0.1")
    if address.is_unspecified or address.is_multicast:
        raise ValueError("POLYAD_POD_IP must be a concrete unicast address")
    return str(address)


def health_endpoint(endpoint: str | None = None) -> str:
    """
    Build the health URL and prevent an explicit override from broadening the bind address.

    Args:
        endpoint (str | None): Optional HTTP URL selecting a port and path on the same Pod IP.

    Returns:
        str: Health URL with correctly bracketed IPv6 literals.

    Raises:
        ValueError: An override uses another host, an invalid port or unsupported URL components.
    """
    address = pod_address()
    host = f"[{address}]" if ":" in address else address
    if endpoint is None:
        return f"http://{host}:8080/healthz"
    parsed = urlsplit(endpoint)
    if (
        parsed.scheme != "http"
        or ipaddress.ip_address(parsed.hostname or "") != ipaddress.ip_address(address)
        or not parsed.port
        or not parsed.path.startswith("/")
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("--liveness must be an HTTP URL bound only to POLYAD_POD_IP")
    return f"http://{host}:{parsed.port}{parsed.path}"


def read_health(endpoint: str | None = None) -> dict[str, Any]:
    """
    Query the same Pod-bound listener without sending local probes through a proxy.

    Args:
        endpoint (str | None): Optional validated health endpoint override.

    Returns:
        dict[str, Any]: Kopf's successful health response.

    Raises:
        ValueError: The response is not a JSON object.
    """
    with build_opener(ProxyHandler({})).open(health_endpoint(endpoint), timeout=2) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise ValueError("health response must be a JSON object")
    return payload


def check_readiness(payload: dict[str, Any]) -> None:
    """
    Retain lifecycle, worker, lease and cache readiness checks on the Pod-bound endpoint.

    Args:
        payload (dict[str, Any]): Current Kopf probe response.

    Returns:
        None: An unready process raises an error for the container probe.

    Raises:
        RuntimeError: The replica is draining or its scheduler is not ready.
    """
    scheduler = payload.get("scheduler", {})
    if (
        Path("/tmp/polyad-events-draining").exists()
        or not all(scheduler.get(key) for key in ("initialized", "worker", "apiFresh", "cacheFresh"))
        or not scheduler.get("attached", True)
    ):
        raise RuntimeError("operator is draining or scheduler dependencies are not ready")


def main() -> None:
    """
    Serve Docker health checks, Kubernetes readiness checks and administrator diagnostics.

    Returns:
        None: Print health JSON on success, or exit nonzero on a failed check.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ready", action="store_true")
    parser.add_argument("--url", help="Match a custom --liveness port/path on the Pod IP")
    args = parser.parse_args()
    try:
        payload = read_health(args.url)
        if args.ready:
            check_readiness(payload)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f"Health check failed: {error}\n")
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
