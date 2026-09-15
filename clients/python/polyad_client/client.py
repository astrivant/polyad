"""
Provide bounded authenticated HTTP requests and explicit event subscriptions.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any


class APIError(RuntimeError):
    """
    Expose HTTP status and JSON error details without embedding credentials.
    """

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        """
        Retain the server response for explicit retry decisions.

        Args:
            status (int): HTTP status code.
            body (dict[str, Any]): Parsed response body.
        """
        self.status, self.body = status, body
        super().__init__(f"Polyad API returned HTTP {status}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


@dataclass(frozen=True)
class Event:
    """
    Carry an SSE cursor, event type and decoded JSON observation.

    Attributes:
        id (str): Stream cursor to persist after processing.
        event (str): Event type, including graph, reset or unavailable.
        data (dict[str, Any]): Observation payload.
    """

    id: str
    event: str
    data: dict[str, Any]


class Client:
    """
    Call composition and activation APIs with no implicit mutation retries.
    """

    def __init__(self, url: str, token: str, *, timeout: float = 30) -> None:
        """
        Configure an API base address and bearer credential.

        Args:
            url (str): Operator API Service or gateway URL.
            token (str): Namespace-scoped bearer credential.
            timeout (float): Finite socket timeout for requests and event reads.
        """
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("use an HTTP(S) base URL without credentials, query or fragment")
        if not token or any(char in token for char in "\r\n") or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("a bearer token and positive finite timeout are required")
        self.url, self._token, self.timeout = url.rstrip("/"), token, timeout
        self._opener = build_opener(_NoRedirect())

    def _open(self, method: str, path: str, body: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None) -> Any:
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                **(headers or {}),
            },
        )
        try:
            return self._opener.open(request, timeout=self.timeout)
        except HTTPError as error:
            with error:
                raw = error.read(1024 * 1024)
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                value = {"error": "non-JSON error response"}
            raise APIError(error.code, value if isinstance(value, dict) else {"error": value}) from None

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._open(method, path, body) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("Polyad response exceeds 4 MiB")
        value: Any = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("expected a JSON object from Polyad")
        return value

    def compose(self, document: dict[str, Any]) -> dict[str, Any]:
        """
        Submit ID-addressed graph intent, retaining its requestId across retries.

        Args:
            document (dict[str, Any]): Composition request with requestId, rootId and objects.

        Returns:
            dict[str, Any]: Accepted composition receipt.
        """
        return self._request("POST", "/v1/compositions", document)

    def composition(self, request_id: str, *, resources: bool = False) -> dict[str, Any]:
        """
        Read composition status or its generated manifest identities.

        Args:
            request_id (str): Original composition ID.
            resources (bool): Include resource audit references.

        Returns:
            dict[str, Any]: Current status or audit response.
        """
        return self._request("GET", f"/v1/compositions/{quote(request_id, safe='')}" + ("/resources" if resources else ""))

    def activate(self, *, request_id: str, graph: str, graph_uid: str, node: str, kind: str = "Graph") -> dict[str, Any]:
        """
        Request a downstream execution governed by the definition's activation policy.

        Args:
            request_id (str): Stable idempotency ID; use a new ID only for a new pulse.
            graph (str): Executable graph instance name.
            graph_uid (str): Persisted graph UID from composition audit or Kubernetes.
            node (str): Logical downstream node name.
            kind (str): Graph, EphemeralGraph or PolyGraph.

        Returns:
            dict[str, Any]: Durable receipt, not a guarantee of admission.
        """
        return self._request(
            "POST",
            "/v1/activations",
            {
                "requestId": request_id,
                "graph": graph,
                "graphUid": graph_uid,
                "node": node,
                "kind": kind,
            },
        )

    def activation(self, request_id: str) -> dict[str, Any]:
        """
        Read a pulse's queue, execution or terminal status.

        Args:
            request_id (str): Activation request ID.

        Returns:
            dict[str, Any]: Latest receipt observation.
        """
        return self._request("GET", f"/v1/activations/{quote(request_id, safe='')}")

    def stop(self, request_id: str) -> dict[str, Any]:
        """
        Request cleanup while retaining the receipt's idempotency identity.

        Args:
            request_id (str): Queued or running activation to stop.

        Returns:
            dict[str, Any]: Accepted stop signal; poll activation for cleanup progress.
        """
        return self._request("POST", f"/v1/activations/{quote(request_id, safe='')}/stop")

    def openapi(self) -> dict[str, Any]:
        """
        Read the authenticated API schema.

        Returns:
            dict[str, Any]: OpenAPI document.
        """
        return self._request("GET", "/openapi.json")

    def events(self, *, last_event_id: str | None = None) -> Iterator[Event]:
        """
        Stream observations using a client configured for the separate events Service.

        Args:
            last_event_id (str | None): Last processed cursor for explicit reconnection.

        Yields:
            Event: One bounded JSON observation or stream control message.
        """
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            if any(char in last_event_id for char in "\r\n"):
                raise ValueError("event cursor must fit one HTTP header")
            headers["Last-Event-ID"] = last_event_id
        with self._open("GET", "/v1/events", headers=headers) as response:
            data: list[str] = []
            event_id, event_type, size = "", "message", 0
            while raw := response.readline(1024 * 1024 + 1):
                size += len(raw)
                if size > 1024 * 1024:
                    raise ValueError("event exceeds 1 MiB")
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        value = json.loads("\n".join(data))
                        if not isinstance(value, dict):
                            raise ValueError("expected a JSON event object")
                        yield Event(event_id, event_type, value)
                    event_type, data, size = "message", [], 0
                elif not line.startswith(":"):
                    field, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field == "id" and "\x00" not in value:
                        event_id = value
                    elif field == "event":
                        event_type = value
                    elif field == "data":
                        data.append(value)
