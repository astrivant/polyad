"""
Provide bounded authenticated HTTP requests and explicit event subscriptions.
"""

from __future__ import annotations

import json
import math
import time
from collections import deque
from typing import TYPE_CHECKING
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, build_opener

from polyad_sdk.api.interfaces import (
    AdaptationReporter,
    CapabilityAdvertiser,
    ConnectionNegotiator,
    ServiceLevelReporter,
    ThroughputReporter,
)
from polyad_sdk.events.source import EventSource
from polyad_sdk.exceptions.api import APIError
from polyad_sdk.observability import Telemetry
from polyad_sdk.transport.http import _NoRedirect
from polyad_types import ActivationRequest, to_dict
from polyad_types.api.capabilities import CapabilityAdvertisement, CapabilityContract
from polyad_types.events.envelope import DEFAULT_MAX_EVENT_BYTES, Event, EventStreamSettings, validate_event_limit
from polyad_types.exceptions.events import EventTooLarge
from polyad_types.serialization import from_dict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from threading import Event as StopEvent
    from typing import Any, Literal

    from polyad_types import (
        AdaptationReport,
        CompositionRequest,
        ConnectionRequest,
        ConnectionResponse,
        ServiceConnectionRequest,
        ServiceEndpoint,
        ServiceLevelReport,
        ThroughputSample,
    )

__all__ = ("Client",)


class Client(EventSource, ThroughputReporter, AdaptationReporter, ServiceLevelReporter, ConnectionNegotiator, CapabilityAdvertiser):
    """
    Call Polyad APIs with no implicit mutation retries.
    """

    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout: float = 30,
        identity_cluster: str = "",
        token_provider: Callable[[], str] | None = None,
        max_event_bytes: int = DEFAULT_MAX_EVENT_BYTES,
        telemetry: Telemetry | None = None,
    ) -> None:
        """
        Configure an API base address and bearer credential.

        Args:
            url (str): Operator API Service or gateway URL.
            token (str | None): Namespace-scoped bearer credential; explicitly None for an unauthenticated demo.
            timeout (float): Finite socket timeout for requests and event reads.
            identity_cluster (str): Registered token issuer for service negotiation; empty uses the receiving operator cluster.
            token_provider (Callable[[], str] | None): Read a rotating projected token immediately before each HTTP request.
            max_event_bytes (int): Maximum UTF-8 bytes per complete SSE record or WebSocket frame; 1024 through 16 MiB.
            telemetry (Telemetry | None): Shared request instrumentation; None uses application-installed providers.
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
        self.max_event_bytes = validate_event_limit(max_event_bytes)
        if any(char in identity_cluster for char in "\r\n") or len(identity_cluster) > 63:
            raise ValueError("invalid identity cluster")
        self.identity_cluster, self._token_provider = identity_cluster, token_provider
        self._opener = build_opener(_NoRedirect())
        self.telemetry = telemetry if telemetry is not None else Telemetry()

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

    def report_adaptation(self, report: AdaptationReport) -> dict[str, Any]:
        """
        Publish one fenced SDK strategy lifecycle transition.

        Args:
            report (AdaptationReport): Fenced definition and invocation transition.

        Returns:
            dict[str, Any]: Current definition adaptation acknowledgement.
        """
        return self._request("POST", "/v1/adaptations", to_dict(report))

    def report_service_level(self, report: ServiceLevelReport) -> dict[str, Any]:
        """
        Publish one service-level observation window for a Daemon.

        Args:
            report (ServiceLevelReport): Fenced availability, quality and capability measurements.

        Returns:
            dict[str, Any]: Current evaluated service-level status.
        """
        return self._request("POST", "/v1/service-level", to_dict(report))

    def connect_services(self, request: ServiceConnectionRequest) -> dict[str, Any]:
        """
        Propose a TTL connection between discovered services through their common boundary owner.

        Args:
            request (ServiceConnectionRequest): Exact service identities and stable idempotency key.

        Returns:
            dict[str, Any]: Durable proposal awaiting peer approval and policy admission.
        """
        return self._request("POST", "/v1/connections/atlas", to_dict(request))

    def advertise_capabilities(self, advertisement: CapabilityAdvertisement) -> CapabilityContract:
        """
        Replace this Pod's complete TTL-bound contract through the connections listener.

        Args:
            advertisement (CapabilityAdvertisement): Application-assessed capacity and sharing limits; must be nonempty.

        Returns:
            CapabilityContract: Server-timed offer, not a reservation or a network connection.
        """
        if not advertisement.capabilities:
            raise ValueError("use withdraw_capabilities to remove the contract")
        return from_dict(self._request("POST", "/v1/capabilities", to_dict(advertisement)), CapabilityContract)

    def withdraw_capabilities(self, endpoint: ServiceEndpoint) -> dict[str, Any]:
        """
        Remove only this authenticated Pod's contract through the connections listener.

        Args:
            endpoint (ServiceEndpoint): Exact graph incarnation and logical service.

        Returns:
            dict[str, Any]: Idempotent withdrawal acknowledgement.
        """
        return self._request("POST", "/v1/capabilities", to_dict(CapabilityAdvertisement(endpoint, ())))

    def offers(
        self,
        *,
        capabilities: Sequence[str] = (),
        labels: Mapping[str, str] | None = None,
        available_only: bool = True,
        max_graphs: int = 256,
    ) -> Iterator[CapabilityContract]:
        """
        Discover authorized replica offers matching all requested labels and work types.

        Args:
            capabilities (Sequence[str]): Required capability names; empty accepts any advertised type.
            labels (Mapping[str, str] | None): Exact, conjunctive application group selectors.
            available_only (bool): Require positive offered rates and nonzero slots for each requested type.
            max_graphs (int): Existing directory traversal bound, from 1 through 4096.

        Yields:
            CapabilityContract: Fresh matching contract; rates for different work types share the same provider pool.
        """
        if isinstance(capabilities, (str, bytes)) or any(not isinstance(item, str) or not item for item in capabilities):
            raise ValueError("capabilities must be a sequence of nonempty names")
        if type(available_only) is not bool:
            raise ValueError("available_only must be boolean")
        selectors = dict(labels or {})
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in selectors.items()):
            raise ValueError("label selectors must be strings")
        required = set(capabilities)
        for service in self.services(max_graphs=max_graphs):
            for value in service.get("contracts", []):
                contract = from_dict(value, CapabilityContract)
                offer = contract.advertisement

                # Recheck expiry at yield time, including after a paused iterator.
                # Labels filter already-authorized discovery; they confer no access.
                if contract.expiresAt <= time.time() or any(offer.labels.get(key) != item for key, item in selectors.items()):
                    continue
                candidates = {item.name: item for item in offer.capabilities}
                if not required <= candidates.keys():
                    continue
                selected = [candidates[name] for name in required] if required else list(candidates.values())
                usable = [item.offered_per_second > 0 and item.offered_concurrency != 0 for item in selected]
                if available_only and not (all(usable) if required else any(usable)):
                    continue
                yield contract

    def services(self, *, max_graphs: int = 256) -> Iterator[dict[str, Any]]:
        """
        Traverse granted graph branches with a bounded breadth-first discovery walk.

        Args:
            max_graphs (int): Maximum distinct graph incarnations visited before stopping with an error.

        Yields:
            dict[str, Any]: Current public service record; no workload payloads or credentials.
        """
        if type(max_graphs) is not int or not 1 <= max_graphs <= 4096:
            raise ValueError("max_graphs must be from 1 through 4096")

        # Traverse discovered children breadth-first and deduplicate by cluster
        # and UID. Names can recur, but graph incarnations identify replayed nodes.
        pending: deque[dict[str, Any]] = deque()
        seen: set[tuple[str, str]] = set()

        def enqueue(addresses: list[dict[str, Any]]) -> None:
            for address in addresses:
                identity = address.get("cluster", ""), address["uid"]
                if identity in seen:
                    continue
                if len(seen) >= max_graphs:
                    raise ValueError("discovery exceeds max_graphs; narrow the credential grants")
                seen.add(identity)
                pending.append(address)

        offset = 0
        while True:
            roots = self.discover(offset=offset)
            enqueue(roots["roots"])
            if roots.get("nextOffset") is None:
                break
            offset = roots["nextOffset"]
        while pending:
            address = pending.popleft()
            try:
                result = self.discover(
                    graph=address["name"],
                    namespace=address["namespace"],
                    kind=address["kind"],
                    cluster=address.get("cluster"),
                    graph_uid=address["uid"],
                )
            except APIError as error:
                if error.status in {403, 404}:
                    continue
                raise
            yield from result["services"]
            enqueue(result["children"])

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

    def _authorization_headers(self) -> dict[str, str]:
        # Read a projected token on each request so rotation does not require
        # rebuilding the client or restarting the application.
        token = self._token_provider() if self._token_provider else self._token
        if token is not None and (not token or any(char in token for char in "\r\n")):
            raise ValueError("invalid bearer token")
        return {
            **({"Authorization": f"Bearer {token}"} if token is not None else {}),
            **({"X-Polyad-Cluster": self.identity_cluster} if self.identity_cluster else {}),
        }

    def _open(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        endpoint: tuple[str, int] | None = None,
    ) -> Any:
        with self.telemetry.operation("api.request", attributes={"http.request.method": method}):
            data = json.dumps(body, allow_nan=False).encode() if body is not None else None
            request = Request(
                self.url + path,
                data=data,
                method=method,
                headers={
                    **{key.lower(): value for key, value in self.telemetry.propagation_environment().items()},
                    **self._authorization_headers(),
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    **(headers or {}),
                },
            )
            try:
                transport = self._opener
                if endpoint is not None:
                    from polyad_sdk.transport.routing import opener

                    transport = opener(endpoint)
                return transport.open(request, timeout=self.timeout)
            except HTTPError as error:
                with error:
                    raw = error.read(1024 * 1024)
                try:
                    value = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    value = {"error": "non-JSON error response"}
                raise APIError(error.code, value if isinstance(value, dict) else {"error": value}) from None

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        # Read one byte beyond the cap to distinguish a response exactly at the
        # limit from a truncated oversized document before attempting JSON parsing.
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

    def event_settings(self) -> EventStreamSettings:
        """
        Read advertised operator event budgets without increasing this client's receive limit.

        Returns:
            EventStreamSettings: Active Helm-configured limits on the events Service.
        """
        return EventStreamSettings(**self._request("GET", "/v1/events/config"))

    def events(
        self,
        *,
        last_event_id: str | None = None,
        cluster: str | None = None,
        transport: Literal["sse", "websocket"] = "sse",
        endpoint: tuple[str, int] | None = None,
        stop_event: StopEvent | None = None,
        heartbeats: bool = False,
    ) -> Iterator[Event]:
        """
        Stream observations using a client configured for the separate events Service.

        Args:
            last_event_id (str | None): Last processed cursor for explicit reconnection.
            cluster (str | None): Registered cluster stream; cursors belong to that selected stream.
            transport (Literal['sse', 'websocket']): Subscription framing; WebSocket requires operator enablement.
            endpoint (tuple[str, int] | None): Explicit trusted socket target; normally selected by a rebalancing subscription.
            stop_event (StopEvent | None): Optional cancellation, checked on heartbeats and observations.
            heartbeats (bool): Also emit empty SSE heartbeat observations for application refresh scheduling.

        Yields:
            Event: One bounded JSON observation or stream control message.
        """
        if transport not in {"sse", "websocket"}:
            raise ValueError("event transport must be sse or websocket")
        if type(heartbeats) is not bool:
            raise ValueError("heartbeats must be a boolean")
        headers = {"Accept": "text/event-stream"}
        if last_event_id is not None:
            if any(char in last_event_id for char in "\r\n"):
                raise ValueError("event cursor must fit one HTTP header")
            headers["Last-Event-ID"] = last_event_id
        query = "?" + urlencode({"cluster": cluster}) if cluster is not None else ""
        if transport == "websocket":
            from polyad_sdk.transport.websocket import events

            address = urlsplit(self.url + "/v1/events/ws" + query)
            uri = address._replace(scheme="wss" if address.scheme == "https" else "ws").geturl()
            yield from events(
                uri,
                {**self._authorization_headers(), **headers},
                self.timeout,
                self.max_event_bytes,
                endpoint=endpoint,
                stop_event=stop_event,
            )
            return
        path = "/v1/events" + query
        with self._open("GET", path, headers=headers, endpoint=endpoint) as response:
            # SSE can split one JSON event across multiple data lines. Enforce
            # the byte budget over the whole event, resetting it at a blank line.
            data: list[str] = []
            event_id, event_type, size = "", "message", 0
            comment = False
            while raw := response.readline(self.max_event_bytes - size + 1):
                if stop_event is not None and stop_event.is_set():
                    return
                size += len(raw)
                if size > self.max_event_bytes:
                    raise EventTooLarge(f"event exceeds {self.max_event_bytes} bytes")
                line = raw.decode("utf-8").rstrip("\r\n")
                if not line:
                    if data:
                        value = json.loads("\n".join(data))
                        if not isinstance(value, dict):
                            raise ValueError("expected a JSON event object")
                        yield Event("" if event_type in {"reset", "unavailable", "copulse"} else event_id, event_type, value)
                    elif heartbeats and comment:
                        yield Event("", "heartbeat", {})

                    # Event IDs persist across SSE records unless replaced;
                    # payload/type/heartbeat state belongs only to the current record.
                    event_type, data, size, comment = "message", [], 0, False
                elif line.startswith(":"):
                    comment = True
                else:
                    field, _, value = line.partition(":")
                    value = value.removeprefix(" ")
                    if field == "id" and "\x00" not in value:
                        event_id = value
                    elif field == "event":
                        event_type = value
                    elif field == "data":
                        data.append(value)

    def event_endpoints(self, *, cluster: str | None = None) -> dict[str, Any]:
        """
        Discover stream replicas from the configured authority, never from an event-provided URL.

        Args:
            cluster (str | None): Root-held cluster stream; authorization must match the subscription.

        Returns:
            dict[str, Any]: Operator membership, routing policy and initial replay cursor.
        """
        query = "?" + urlencode({"cluster": cluster}) if cluster is not None else ""
        return self._request("GET", "/v1/events/endpoints" + query)

    def discover(
        self,
        *,
        graph: str | None = None,
        namespace: str | None = None,
        kind: str = "Graph",
        cluster: str | None = None,
        graph_uid: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        """
        Read permitted atlas roots or one graph's current service and child identities.

        Args:
            graph (str | None): Exact graph instance; omitted enumerates credential-granted starting points.
            namespace (str | None): Required when selecting a graph.
            kind (str): Graph, PolyGraph or ReplicaGroup.
            cluster (str | None): Registered cluster; omitted selects local execution.
            graph_uid (str | None): Expected graph incarnation.
            offset (int): Pagination offset for starting points.
            limit (int): Maximum root grants examined per page, up to 100.

        Returns:
            dict[str, Any]: Current permitted service identities and cursors for event subscriptions.
        """
        parameters: dict[str, Any] = {"offset": offset, "limit": limit}
        if graph is not None:
            if not namespace:
                raise ValueError("graph discovery requires its namespace")
            parameters.update(graph=graph, namespace=namespace, kind=kind)
            parameters.update({key: value for key, value in {"cluster": cluster, "uid": graph_uid}.items() if value is not None})
        return self._request("GET", "/v1/discovery?" + urlencode(parameters))
