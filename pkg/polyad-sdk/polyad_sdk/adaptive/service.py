"""
Drive application-owned behavior from authorized, checkpointed neighborhood deltas.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from polyad_sdk.adaptive.models import Change, Settings, differences
from polyad_sdk.adaptive.state import State, projection
from polyad_sdk.client import Client
from polyad_sdk.filters import Filter
from polyad_sdk.subscriptions import StreamInterrupted
from polyad_types import ConnectionResponse, ServiceConnectionRequest, ServiceEndpoint

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any, Literal, Self

    from polyad_sdk.adaptive.models import Environment
    from polyad_sdk.interfaces import ConnectionNegotiator, EventSource, ThroughputReporter
    from polyad_sdk.subscriptions import Subscription
    from polyad_types.events import Event
    from polyad_types.network import NetworkPort
    from polyad_types.throughput import ThroughputSample


class AdaptiveService(ABC):
    """
    Define application adaptation over authorized, checkpointed neighborhood changes.

    Subclass this ABC and implement adapt() to update application-owned routing,
    admission or worker policy. The base owns observation freshness, delta
    construction, ordered delivery and replay checkpoints. It starts no threads,
    workers or application servers, and does not infer permission to mutate graphs.

    adapt() runs first for every baseline or meaningful change. Additional
    on_change() hooks run afterward. A failed adaptation blocks later hooks and
    cursor advancement; retrying the same change resumes unfinished delivery.
    """

    def __init__(
        self,
        identity: ServiceEndpoint,
        events: EventSource,
        *,
        api: ThroughputReporter | None = None,
        connections: ConnectionNegotiator | None = None,
        settings: Settings | None = None,
        checkpoint: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """
        Bind explicit identity, separately authorized clients and bounded observation state.

        Args:
            identity (ServiceEndpoint): Exact graph incarnation and logical node, with an optional registered cluster.
            events (EventSource): Authorized events/topology client.
            api (ThroughputReporter | None): Separately authorized throughput reporter.
            connections (ConnectionNegotiator | None): Projected-token client for consent and connection requests.
            settings (Settings | None): Freshness, inventory and event transport settings.
            checkpoint (Callable[[str], None] | None): Persist a cursor after all matching hooks succeed.
            clock (Callable[[], float]): Application clock returning Unix seconds; injectable for deterministic tests.
        """
        self.identity, self.events, self.api, self.connections = identity, events, api, connections
        self.settings = settings or Settings()
        self._clock, self._checkpoint = clock, checkpoint
        self._state = State(identity, self.settings)
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._published = self._state.view(clock())
        self._hooks: list[tuple[Callable[[Change], None], tuple[str, ...], Filter | None]] = [(self.adapt, (), None)]
        self._pending: Change | None = None
        self._handled: set[int] = set()
        self._pending_cursor: str | None = None
        self._cursor: str | None = None
        self._subscription: Subscription | None = None
        self._running = False
        self._stopped = threading.Event()

    @abstractmethod
    def adapt(self, change: Change) -> None:
        """
        Apply one authorized baseline or meaningful delta to application-owned behavior.

        Implement this method in a concrete service subclass. Use the baseline
        to initialize application context and change.matching() to select later
        deltas. Recheck self.view when acting, especially on a retry, because
        topology or connection grants may have expired since the change arrived.

        Keep adaptation bounded: update local policy or notify the application's
        worker supervisor instead of doing business work on this observation
        thread. Preserve readiness, local admission budgets and accepted-work
        draining when handing a change to that supervisor.

        The base invokes this before optional hooks and checkpoint persistence.
        Exceptions propagate with this change pending. Make partial effects
        retry-safe; successful adaptation is skipped if a later hook or checkpoint
        fails and the same change is retried within this instance.

        Args:
            change (Change): Immutable before/after views, meaningful deltas and
                an isolated copy of the triggering event, when present.

        Returns:
            None: Application adaptation completed, allowing later hooks and
                checkpoint persistence to proceed.
        """
        ...

    @classmethod
    def from_environment(
        cls,
        *,
        cluster: str = "",
        settings: Settings | None = None,
        timeout: float = 10,
        max_event_bytes: int = 1024 * 1024,
        checkpoint: Callable[[str], None] | None = None,
        allow_unauthenticated: bool = False,
    ) -> Self:
        """
        Read injected workload identity and application-owned credentials without granting new access.

        Args:
            cluster (str): Registered target/identity cluster; empty selects the events Service's local cluster.
            settings (Settings | None): Application observation settings.
            timeout (float): Finite HTTP and stream read timeout.
            max_event_bytes (int): Maximum received event size, validated by Client.
            checkpoint (Callable[[str], None] | None): Successful-hook cursor persistence callback.
            allow_unauthenticated (bool): Explicitly permit clients without credentials for an administrator-enabled demo.

        Returns:
            Self: Configured concrete subclass; construction performs no network calls or thread startup.
        """
        identity = ServiceEndpoint(
            cluster,
            os.environ["POLYAD_GRAPH_NAMESPACE"],
            os.environ["POLYAD_GRAPH_KIND"],
            os.environ["POLYAD_GRAPH_NAME"],
            os.environ["POLYAD_GRAPH_UID"],
            os.environ["POLYAD_NODE_NAME"],
        )

        def client(name: str, *, required: bool = False) -> Client | None:
            url = os.environ.get(f"POLYAD_{name}_URL", "")
            token = os.environ.get(f"POLYAD_{name}_TOKEN")
            filename = os.environ.get(f"POLYAD_{name}_TOKEN_FILE", "")
            if name == "CONNECTIONS" and not filename and not token:
                projected = Path("/var/run/polyad-connections/token")
                filename = str(projected) if projected.is_file() else ""
            if not url or (not token and not filename and not allow_unauthenticated):
                if required:
                    raise ValueError(f"configure POLYAD_{name}_URL and an application-owned token or token file")
                return None
            provider = (lambda: Path(filename).read_text().strip()) if filename else None
            return Client(url, token, timeout=timeout, max_event_bytes=max_event_bytes, token_provider=provider, identity_cluster=cluster)

        events = client("EVENTS", required=True)
        assert events is not None
        return cls(identity, events, api=client("API"), connections=client("CONNECTIONS"), settings=settings, checkpoint=checkpoint)

    @property
    def view(self) -> Environment:
        """
        Read current context with freshness and grant expiry evaluated now.

        Returns:
            Environment: Immutable context; unavailable topology yields no routing candidates.
        """
        with self._lock:
            return self._state.view(self._clock())

    @property
    def cursor(self) -> str | None:
        """
        Return the last successfully handled stream position.

        Returns:
            str | None: Cursor scoped to this client authority and selected cluster.
        """
        return self._cursor

    def on_change(self, callback: Callable[[Change], None], *, paths: tuple[str, ...] = (), match: Filter | None = None) -> Self:
        """
        Register an additional hook after adapt(), optionally selecting paths and event filters.

        Args:
            callback (Callable[[Change], None]): Synchronous application hook; failures retain the pending change for retry.
            paths (tuple[str, ...]): Dot-separated prefixes such as topology.outgoing, decision or resources.
            match (Filter | None): Optional predicate over the triggering authorized event; snapshot baselines have no event.

        Returns:
            Self: This concrete instance for chained registration; hooks run only on meaningful changes or baselines.
        """
        if self._running or self._pending is not None:
            raise RuntimeError("register hooks before running or retrying a pending change")
        if any(not path or any(not part for part in path.split(".")) for path in paths):
            raise ValueError("delta prefixes must contain nonempty path components")
        self._hooks.append((callback, paths, match))
        return self

    def _snapshot(self, now: float, *, reset: bool = False) -> str:
        identity = self.identity
        snapshot = self.events.topology(
            kind=identity.kind,
            graph=identity.graph,
            graph_uid=identity.graphUid,
            node=identity.node,
            cluster=identity.cluster or None,
        )
        with self._lock:
            self._state.load(snapshot, now, reset=reset)
        return str(snapshot["cursor"])

    def _deliver(self, change: Change, cursor: str | None) -> None:
        self._pending, self._pending_cursor = change, cursor
        if change.baseline or change.deltas:
            for index, (callback, paths, match) in enumerate(self._hooks):
                if index in self._handled:
                    continue
                delivered = replace(change, event=copy.deepcopy(change.event))
                if match is not None and (delivered.event is None or not match(delivered.event)):
                    continue
                if paths and not change.baseline and not any(change.matching(path) for path in paths):
                    continue
                callback(delivered)
                self._handled.add(index)
        if cursor is not None and cursor != self._cursor and self._checkpoint is not None:
            self._checkpoint(cursor)
        self._published, self._cursor = change.after, cursor
        self._pending, self._pending_cursor = None, None
        self._handled.clear()

    def _change(self, event: Event | None, *, baseline: bool = False) -> Change:
        current = self.view
        event = copy.deepcopy(event)
        deltas = () if baseline else differences(projection(self._published), projection(current))
        return Change(self._published, current, deltas, baseline, event)

    def refresh(self, *, reset: bool = False) -> Environment:
        """
        Refresh topology, preserving the replay position unless explicitly recovering a gap.

        Args:
            reset (bool): Establish a new baseline and cursor, discarding incomplete event history after reset/HTTP 410.

        Returns:
            Environment: Refreshed context after hooks succeed; failed reads preserve data but mark it unavailable.
        """
        if not self._operation.acquire(blocking=False):
            raise RuntimeError("observation processing is already active")
        try:
            if self._pending is not None:
                if self._pending.event is not None or reset:
                    raise RuntimeError("retry the pending event before refreshing or resetting")
                self._deliver(self._pending, self._pending_cursor)
                return self.view
            baseline = reset or self._state.topology is None
            try:
                cursor = self._snapshot(self._clock(), reset=reset)
            except Exception:
                with self._lock:
                    self._state.reason = "topology refresh failed"
                raise
            self._deliver(self._change(None, baseline=baseline), cursor if baseline else self._cursor)
            return self.view
        finally:
            self._operation.release()

    def dispatch(self, event: Event) -> None:
        """
        Process one observation into stable deltas and checkpoint only successful hooks.

        Args:
            event (Event): Public event from this instance's configured authority and cluster stream.

        Returns:
            None: Duplicate committed cursors are ignored; failed callbacks can retry the same event.
        """
        if not self._operation.acquire(blocking=False):
            raise RuntimeError("observation processing is already active")
        try:
            if self._pending is not None:
                if self._pending.event != event:
                    raise RuntimeError("retry the pending event before advancing the stream")
                self._deliver(self._pending, self._pending_cursor)
                return
            event.typed()
            if event.event in {"reset", "unavailable", "copulse"}:
                with self._lock:
                    self._state.reason = f"event stream requires recovery: {event.event}"
                raise StreamInterrupted(event)
            if self._cursor is None:
                raise RuntimeError("refresh topology before dispatching observations")
            if event.id and tuple(map(int, event.id.split("-"))) <= tuple(map(int, self._cursor.split("-"))):
                return
            now = self._clock()
            identity = self.identity
            home_topology = event.event == "topology" and (
                event.data["kind"],
                event.data["namespace"],
                event.data["name"],
                event.data["uid"],
            ) == (identity.kind, identity.namespace, identity.graph, identity.graphUid)
            if identity.cluster and event.data.get("cluster") != identity.cluster:
                home_topology = False
            if home_topology or now - self._state.refreshed >= self.settings.refresh_seconds:
                try:
                    self._snapshot(now)
                except Exception:
                    with self._lock:
                        self._state.reason = "topology refresh failed"
                    raise
            with self._lock:
                self._state.accept(event, now)
            self._deliver(self._change(event), event.id or self._cursor)
        finally:
            self._operation.release()

    def run(self) -> None:
        """
        Consume observations on the caller's thread and refresh topology on heartbeat intervals.

        Returns:
            None: EOF or stop returns; transport, reset and callback failures propagate with the last successful cursor retained.
        """
        with self._lock:
            if self._running:
                raise RuntimeError("AdaptiveService cannot run concurrently")
            self._running = True
            self._stopped.clear()
        try:
            if self._pending is not None:
                if self._pending.event is None:
                    self.refresh()
                else:
                    self.dispatch(self._pending.event)
            self.refresh()
            subscription = self.events.subscribe(
                cluster=self.identity.cluster or None,
                cursor=self._cursor,
                transport=self.settings.transport,
                rebalance=self.settings.rebalance,
                heartbeats=True,
            )
            self._subscription = subscription
            if self._stopped.is_set():
                return
            subscription.on(Filter(lambda _: True), self.dispatch)
            subscription.run()
        finally:
            with self._lock:
                self._running = False
                self._subscription = None
                self._state.reason = "event subscription is not running"

    def stop(self) -> None:
        """
        Request stream shutdown without owning or starting the application's threads.

        Returns:
            None: The active read exits on a heartbeat or within the configured client timeout.
        """
        self._stopped.set()
        if self._subscription is not None:
            self._subscription.stop()

    def connect(
        self, target: ServiceEndpoint, *, request_id: str, ttl_seconds: int, ports: tuple[NetworkPort, ...], bidirectional: bool = False
    ) -> dict[str, Any]:
        """
        Propose an explicit connection from this node through its configured boundary owner.

        Args:
            target (ServiceEndpoint): Exact permitted discovery result.
            request_id (str): Stable application idempotency key; retries retain identical content.
            ttl_seconds (int): Requested lifetime including consent and admission time.
            ports (tuple[NetworkPort, ...]): Requested destination ports.
            bidirectional (bool): Also request a reverse edge.

        Returns:
            dict[str, Any]: Negotiation receipt; Pending does not grant communication.
        """
        if self.connections is None:
            raise RuntimeError("configure a separately authorized connections client")
        request = ServiceConnectionRequest(request_id, self.identity, target, ttl_seconds, ports, bidirectional)
        return self.connections.connect_services(request)

    def respond(self, uid: str, decision: Literal["Approve", "Reject"]) -> dict[str, Any]:
        """
        Explicitly answer one unexpired receipt visible to this endpoint.

        Args:
            uid (str): Server-assigned receipt UID from a connection change.
            decision (Literal['Approve', 'Reject']): Application policy decision; never inferred from a hook match.

        Returns:
            dict[str, Any]: Consent acknowledgement; the operator still performs admission.
        """
        if self.connections is None:
            raise RuntimeError("configure a separately authorized connections client")
        receipt = self.view.connections.get(uid)
        if receipt is None:
            raise ValueError("connection receipt is unknown or expired")
        return self.connections.respond_connection(receipt["namespace"], receipt["name"], ConnectionResponse(uid, decision))

    def report_throughput(self, sample: ThroughputSample) -> dict[str, Any]:
        """
        Report application-measured throughput for this exact graph boundary.

        Args:
            sample (ThroughputSample): Authoritative aggregate with the observed execution generation and policy unit.

        Returns:
            dict[str, Any]: Operator acknowledgement; neither measurement nor topology changes are invented locally.
        """
        if self.api is None:
            raise RuntimeError("configure a separately authorized API client")
        if (sample.graph, sample.graphUid, sample.kind) != (self.identity.graph, self.identity.graphUid, self.identity.kind):
            raise ValueError("throughput report targets a different graph boundary")
        return self.api.report_throughput(sample)
