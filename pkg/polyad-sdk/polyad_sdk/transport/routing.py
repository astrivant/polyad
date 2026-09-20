"""
Connect to discovered Pod IPs while retaining the configured HTTP authority and TLS identity.
"""

from __future__ import annotations

import ipaddress
import socket
from http.client import HTTPConnection, HTTPSConnection
from typing import TYPE_CHECKING
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, build_opener

if TYPE_CHECKING:
    from typing import Any
    from urllib.request import OpenerDirector, Request


def addresses(document: dict[str, Any]) -> list[tuple[str, int]]:
    """
    Validate a bounded IP-only catalog obtained from the client's configured operator.

    Args:
        document (dict[str, Any]): Authenticated event directory response.

    Returns:
        list[tuple[str, int]]: Deduplicated direct socket targets; empty delegates routing to the Service.
    """
    if document.get("routing") == "Service":
        return []
    entries = document.get("endpoints")
    if document.get("routing") != "Direct" or not isinstance(entries, list) or not 1 <= len(entries) <= 256:
        raise ValueError("event directory requires Service routing or 1 through 256 direct endpoints")
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("address"), str):
            raise ValueError("event endpoint requires an IP address")

        # Discovery controls where authenticated connections go. Require numeric
        # IPs and reject special-use destinations before sending credentials.
        ip = ipaddress.ip_address(entry["address"])
        port = entry.get("port")
        if ip.is_loopback or ip.is_unspecified or ip.is_multicast or ip.is_link_local or ip.is_reserved:
            raise ValueError("event discovery cannot redirect credentials to a special-use address")
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("event endpoint requires a valid port")
        pair = str(ip), port
        if pair not in result:
            result.append(pair)
    return result


def opener(endpoint: tuple[str, int]) -> OpenerDirector:
    """
    Override only socket dialing, preserving Host, certificate checks and the TLS server name.

    Args:
        endpoint (tuple[str, int]): Validated IP and port returned by the authoritative operator.

    Returns:
        OpenerDirector: One-request transport without redirects or environment proxies.
    """
    from polyad_sdk.transport.http import _NoRedirect

    # Change only the TCP destination. The original URL still supplies HTTP Host
    # and TLS identity, so direct routing does not change which server is trusted.
    def connect(kind: type[HTTPConnection], host: str, **kwargs: Any) -> HTTPConnection:
        connection = kind(host, **kwargs)

        def dial(address: Any, timeout: Any = None, source_address: Any = None) -> socket.socket:
            return socket.create_connection(endpoint, timeout, source_address)

        connection._create_connection = dial  # type: ignore[attr-defined]
        return connection

    class HTTP(HTTPHandler):
        def http_open(self, req: Request) -> Any:
            return self.do_open(lambda host, **kwargs: connect(HTTPConnection, host, **kwargs), req)

    class HTTPS(HTTPSHandler):
        def https_open(self, req: Request) -> Any:
            return self.do_open(lambda host, **kwargs: connect(HTTPSConnection, host, **kwargs), req)

    return build_opener(ProxyHandler({}), _NoRedirect(), HTTP(), HTTPS())
