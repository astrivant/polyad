"""
Send bounded, destination-pinned HTTP calls using configured outbound credential lanes.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from attrs import field, frozen

from polyad.auth.http import Access
from polyad_types.api.auth import KeyDirection

if TYPE_CHECKING:
    from typing import Any


class NoRedirect(HTTPRedirectHandler):
    """
    Return redirect responses without forwarding bearer credentials to another destination.
    """

    def redirect_request(self, req: Request, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        """
        Disable redirect following for all authenticated calls.

        Args:
            req (Request): Original request.
            fp (Any): Response stream.
            code (int): Redirect status.
            msg (str): Status message.
            headers (Any): Response headers.
            newurl (str): Untrusted redirect destination.

        Returns:
            None: urllib exposes the redirect as an HTTPError response.
        """
        return None


@frozen
class OutboundResponse:
    """
    Retain a completed response without exposing its body in object representations.

    Attributes:
        status (int): Peer HTTP status, including errors and redirects.
        headers (dict[str, str]): Returned HTTP headers.
        body (bytes): Bounded response body.
    """

    status: int
    headers: dict[str, str] = field(repr=False)
    body: bytes = field(repr=False)


class OutboundClient:
    """
    Require an explicit group and key for every service or peer-operator call.
    """

    def __init__(self, access: Access) -> None:
        """
        Share inbound and outbound budgets through their stable credential identity.

        Args:
            access (Access): Configured key registry and shared lane transport.
        """
        self.access = access

    @classmethod
    def from_environment(cls) -> OutboundClient:
        """
        Open the operator's configured destination credentials.

        Returns:
            OutboundClient: Explicit transport for operator integrations; it makes no implicit calls.
        """
        access = Access.from_environment()
        if access is None:
            raise ValueError("outbound calls require a configured credential registry")
        return cls(access)

    def request(
        self,
        group: str,
        name: str,
        method: str,
        path: str = "",
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30,
    ) -> OutboundResponse:
        """
        Dispatch once to a pinned destination after acquiring the key's shared lane.

        Args:
            group (str): Services or operators credential group.
            name (str): Stable credential name within that group.
            method (str): HTTP method; no failed operation is automatically retried.
            path (str): Relative path beneath the configured baseUrl, with an optional query.
            body (bytes | None): Optional encoded request body.
            headers (dict[str, str] | None): Extra headers; identity and destination overrides are rejected.
            timeout (float): Request budget checked between reads, at most 60 seconds with socket waits capped at five seconds.

        Returns:
            OutboundResponse: Completed response of at most 8 MiB, with its permit released.
        """
        if group not in {"services", "operators"}:
            raise ValueError("unknown credential group")
        entries = [(key, token) for category, key, token in self.access.keys.read() if category == group and key.name == name]
        if not entries or entries[0][0].direction == KeyDirection.INBOUND:
            raise ValueError("credential does not authorize outbound calls")
        policy, secret = entries[0]
        if self.access.store is not None and not self.access.store.permitted(group, policy, secret):
            raise ValueError("credential has been revoked")
        parsed = urlsplit(path)
        decoded = parsed.path
        for _ in range(len(decoded) + 1):
            expanded = unquote(decoded)
            if expanded == decoded:
                break
            decoded = expanded
        if parsed.scheme or parsed.netloc or parsed.fragment or decoded.startswith("/") or "\\" in decoded or ".." in decoded.split("/"):
            raise ValueError("outbound paths must stay beneath the configured baseUrl")
        extra = dict(headers or {})
        if any(key.lower() in {"authorization", "host", "proxy-authorization"} for key in extra):
            raise ValueError("outbound callers cannot override credential or destination headers")
        if not 0 < timeout <= 60:
            raise ValueError("outbound timeout must be between zero and 60 seconds")
        permit = self.access.lanes.acquire(group, policy)
        try:
            url = policy.baseUrl.rstrip("/") + "/" + path
            extra["Authorization"] = f"Bearer {secret}"
            request = Request(url, data=body, headers=extra, method=method)
            deadline = time.monotonic() + timeout
            try:
                response = build_opener(NoRedirect()).open(request, timeout=min(timeout, 5))
            except HTTPError as error:
                response = error
            with response:
                data = bytearray()
                while True:
                    permit.check()
                    if time.monotonic() >= deadline:
                        raise TimeoutError("outbound request deadline exceeded")
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError("outbound response exceeds 8 MiB")
                return OutboundResponse(response.status, dict(response.headers), bytes(data))
        finally:
            self.access.lanes.release(permit)

    async def arequest(self, *args: Any, **kwargs: Any) -> OutboundResponse:
        """
        Keep outbound network and lane I/O off the operator's asynchronous event loop.

        Args:
            *args (Any): Positional request arguments.
            **kwargs (Any): Named request arguments.

        Returns:
            OutboundResponse: Completed request; cancellation joins outstanding dispatch.
        """
        operation = asyncio.create_task(asyncio.to_thread(self.request, *args, **kwargs))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            await asyncio.gather(operation, return_exceptions=True)
            raise

    def close(self) -> None:
        """
        Close the transport's lane renewer after requests have completed.

        Returns:
            None: Cached credentials are no longer used by this transport.
        """
        self.access.close()
