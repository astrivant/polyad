"""
Provide bounded authenticated HTTP requests and explicit event subscriptions.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from polyad_types import ActivationRequest, to_dict
from polyad_types.events import Event

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any, Literal

    from polyad_types import CompositionRequest, ConnectionRequest, ConnectionResponse, ThroughputSample


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


class Client:
    """
    Call Polyad APIs with no implicit mutation retries.
    """

    def __init__(self, url: str, token: str | None, *, timeout: float = 30) -> None:
        """
        Configure an API base address and bearer credential.

        Args:
            url (str): Operator API Service or gateway URL.
            token (str | None): Namespace-scoped bearer credential; explicitly None for an unauthenticated demo.
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
        if (token is not None and (not token or any(char in token for char in "\r\n"))) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("a bearer token and positive finite timeout are required")
        self.url, self._token, self.timeout = url.rstrip("/"), token, timeout
        self._opener = build_opener(_NoRedirect())

    def report_throughput(self, sample: ThroughputSample) -> dict[str, Any]:
        """
        Report aggregate demand and completed work for the currently observed graph revision.

        Args:
            sample (ThroughputSample): Fresh measurement using the graph policy's work unit.

        Returns:
            dict[str, Any]: Operator acknowledgement; layout changes are asynchronous.
        """
        with self._open("POST", "/v1/throughput", to_dict(sample)) as response:
            result: dict[str, Any] = json.loads(response.read())
            return result

    def respond_connection(self, namespace: str, name: str, response: ConnectionResponse) -> dict[str, Any]:
        """
        Approve or reject an event's proposal using this endpoint's projected Pod token.

        Args:
            namespace (str): Receipt namespace from the connection event.
            name (str): Server-assigned receipt name from the event.
            response (ConnectionResponse): Receipt UID and explicit approval or rejection.

        Returns:
            dict[str, Any]: Durable consent receipt; activation remains asynchronous.
        """
        return self._request("POST", f"/v1/connections/{quote(namespace, safe='')}/{quote(name, safe='')}/response", to_dict(response))

    def _open(self, method: str, path: str, body: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None) -> Any:
        data = json.dumps(body, allow_nan=False).encode() if body is not None else None
        request = Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                **({"Authorization": f"Bearer {self._token}"} if self._token is not None else {}),
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

    def compose(self, document: CompositionRequest | dict[str, Any]) -> dict[str, Any]:
        """
        Submit ID-addressed graph intent, retaining its requestId across retries.

        Args:
            document (CompositionRequest | dict[str, Any]): Composition request with requestId, rootId and objects.

        Returns:
            dict[str, Any]: Accepted composition receipt.
        """
        return self._request("POST", "/v1/compositions", to_dict(document))

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

    def activate(
        self,
        *,
        request_id: str,
        graph: str,
        graph_uid: str,
        node: str,
        kind: Literal["Graph", "PolyGraph", "ReplicaGroup"] = "Graph",
    ) -> dict[str, Any]:
        """
        Request a downstream execution governed by the definition's activation policy.

        Args:
            request_id (str): Stable idempotency ID; use a new ID only for a new pulse.
            graph (str): Executable graph instance name.
            graph_uid (str): Persisted graph UID from composition audit or Kubernetes.
            node (str): Logical downstream node name.
            kind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Target graph kind.

        Returns:
            dict[str, Any]: Durable receipt, not a guarantee of admission.
        """
        return self._request(
            "POST",
            "/v1/activations",
            to_dict(ActivationRequest(requestId=request_id, graph=graph, graphUid=graph_uid, node=node, kind=kind)),
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

    def observe(self, name: str, *, kind: Literal["Graph", "PolyGraph", "ReplicaGroup"] = "Graph") -> dict[str, Any]:
        """
        Read a timestamped cluster-local snapshot from an optional observer service.

        Args:
            name (str): Graph instance name in the observer's configured namespace.
            kind (Literal['Graph', 'PolyGraph', 'ReplicaGroup']): Boundary kind to observe.

        Returns:
            dict[str, Any]: Identity, observation time, topology and local execution metrics.
        """
        return self._request("GET", f"/v1/observations/{quote(kind, safe='')}/{quote(name, safe='')}")

    def connect(self, document: ConnectionRequest | dict[str, Any]) -> dict[str, Any]:
        """
        Request a temporary edge using the connections Service and a projected token.

        Args:
            document (ConnectionRequest | dict[str, Any]): Request with namespace, graph identity, endpoints and ttlSeconds.

        Returns:
            dict[str, Any]: Durable receipt; poll connection for admission and expiry.
        """
        return self._request("POST", "/v1/connections", to_dict(document))

    def connection(self, namespace: str, request_id: str) -> dict[str, Any]:
        """
        Read this service account's temporary connection receipt.

        Args:
            namespace (str): Target graph namespace.
            request_id (str): Original idempotency key.

        Returns:
            dict[str, Any]: Immutable deadline and observed admission status.
        """
        return self._request("GET", f"/v1/connections/{quote(namespace, safe='')}/{quote(request_id, safe='')}")

    def disconnect(self, namespace: str, request_id: str) -> dict[str, Any]:
        """
        Request early revocation while preserving retry identity until audit retention ends.

        Args:
            namespace (str): Target graph namespace.
            request_id (str): Original idempotency key.

        Returns:
            dict[str, Any]: Accepted revocation; poll connection until cleanup is observed.
        """
        return self._request("DELETE", f"/v1/connections/{quote(namespace, safe='')}/{quote(request_id, safe='')}")

    def topology(
        self, *, graph: str, kind: str = "Graph", graph_uid: str | None = None, node: str | None = None, cluster: str | None = None
    ) -> dict[str, Any]:
        """
        Read neighbors and a starting cursor using the events Service and its credential.

        Args:
            graph (str): Persisted graph instance name.
            kind (str): Graph, PolyGraph or ReplicaGroup.
            graph_uid (str | None): Expected graph incarnation; replacements return HTTP 409.
            node (str | None): Logical node to inspect; omitted returns the entire boundary.
            cluster (str | None): Registered cluster when reading through a root control plane.

        Returns:
            dict[str, Any]: Current topology or neighbors, including revision and replay cursor.
        """
        query = urlencode({key: value for key, value in {"uid": graph_uid, "node": node, "cluster": cluster}.items() if value is not None})
        path = f"/v1/graphs/{quote(kind, safe='')}/{quote(graph, safe='')}/topology"
        return self._request("GET", path + (f"?{query}" if query else ""))

    def events(self, *, last_event_id: str | None = None, cluster: str | None = None) -> Iterator[Event]:
        """
        Stream observations using a client configured for the separate events Service.

        Args:
            last_event_id (str | None): Last processed cursor for explicit reconnection.
            cluster (str | None): Registered cluster stream; cursors belong to that selected stream.

        Yields:
            Event: One bounded JSON observation or stream control message.
        """
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            if any(char in last_event_id for char in "\r\n"):
                raise ValueError("event cursor must fit one HTTP header")
            headers["Last-Event-ID"] = last_event_id
        path = "/v1/events" + ("?" + urlencode({"cluster": cluster}) if cluster is not None else "")
        with self._open("GET", path, headers=headers) as response:
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
