"""
Drive application-owned behavior from authorized, checkpointed neighborhood deltas.
"""

from __future__ import annotations

import copy
import threading
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, cast

from polyad_sdk.api.client import Client
from polyad_sdk.api.interfaces import AdaptationReporter, ServiceLevelReporter
from polyad_sdk.events.filters import Filter
from polyad_sdk.events.subscriptions import StreamInterrupted
from polyad_sdk.observability import Telemetry
from polyad_sdk.runtime.context import WorkloadContext
from polyad_sdk.runtime.environment import env as sdk_environment
from polyad_sdk.symbiosis.models import Change, Settings, differences
from polyad_sdk.symbiosis.state import State, projection
from polyad_sdk.symbiosis.strategies import AdaptationStrategy, ConstraintStrategy
from polyad_types import AdaptationReport, ConnectionResponse, ServiceConnectionRequest

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from typing import Any, Literal, Self

    from polyad_sdk.api.interfaces import ConnectionNegotiator, ThroughputReporter
    from polyad_sdk.events.source import EventSource
    from polyad_sdk.events.subscriptions import Subscription
    from polyad_sdk.symbiosis.models import Environment
    from polyad_types import ServiceEndpoint
    from polyad_types.api.service_level import ServiceLevelReport
    from polyad_types.api.throughput import ThroughputSample
    from polyad_types.events.envelope import Event
    from polyad_types.networking.access import NetworkPort


def _strategy_components(strategies: Sequence[AdaptationStrategy], require_strategies: bool) -> tuple[AdaptationStrategy, ...]:
    """
    Validate and freeze application-chosen components before constructing runtime state.

    Args:
        strategies (Sequence[AdaptationStrategy]): Fully constructed application strategies.
        require_strategies (bool): Require a nonempty collection; false explicitly accepts undefined adaptation for omitted cases.

    Returns:
        tuple[AdaptationStrategy, ...]: Validated component order, independent of the caller's mutable list.

    Raises:
        TypeError: The switch is not a Boolean or a component does not implement the strategy ABC.
        ValueError: Required strategies are absent or constraint names repeat.
    """
    if type(require_strategies) is not bool:
        raise TypeError("require_strategies must be a boolean")
    components = tuple(strategies)
    if require_strategies and not components:
        raise ValueError("define at least one adaptation strategy before construction, or explicitly set require_strategies=False")
    if any(not isinstance(strategy, AdaptationStrategy) for strategy in components):
        raise TypeError("strategies must implement AdaptationStrategy")
    names = [strategy.name for strategy in components if isinstance(strategy, ConstraintStrategy)]
    if len(names) != len(set(names)):
        raise ValueError("constraint strategy names must be unique within a service")
    return components


class AdaptiveService(ABC):
    """
    Define application adaptation over authorized, checkpointed neighborhood changes.

    Subclass this ABC and implement adapt() to update application-owned routing,
    admission or worker policy. The base owns observation freshness, delta
    construction, ordered delivery and replay checkpoints. It starts no threads,
    workers or application servers, and does not infer permission to mutate graphs.

    Injected strategies run in their declared order, then adapt() runs, followed
    by on_change() hooks. Each component has independent retry bookkeeping.
    Failure blocks later components and cursor advancement; retrying the same
    change resumes unfinished delivery.

    Construction requires at least one application-chosen strategy. Categories
    are selected by the application. Setting require_strategies=False permits an
    empty collection, with undefined adaptation behavior for uncovered cases.
    """

    def __init__(
        self,
        identity: ServiceEndpoint,
        events: EventSource,
        *,
        api: ThroughputReporter | None = None,
        adaptations: AdaptationReporter | None = None,
        service_levels: ServiceLevelReporter | None = None,
        connections: ConnectionNegotiator | None = None,
        strategies: Sequence[AdaptationStrategy] = (),
        require_strategies: bool = True,
        context: WorkloadContext | None = None,
        telemetry: Telemetry | None = None,
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
            adaptations (AdaptationReporter | None): Authorized strategy lifecycle reporter; defaults to api when supported.
            service_levels (ServiceLevelReporter | None): Authorized SLA reporter; defaults to api when supported.
            connections (ConnectionNegotiator | None): Projected-token client for consent and connection requests.
            strategies (Sequence[AdaptationStrategy]): Ordered application components, copied at construction.
            require_strategies (bool): Require at least one strategy; false accepts undefined behavior for uncovered cases.
            context (WorkloadContext | None): Projected startup context; explicit construction defaults to identity only.
            telemetry (Telemetry | None): Shared application traces and metrics; None uses installed global providers.
            settings (Settings | None): Freshness, inventory and event transport settings.
            checkpoint (Callable[[str], None] | None): Persist a cursor after all matching hooks succeed.
            clock (Callable[[], float]): Application clock returning Unix seconds; injectable for deterministic tests.

        Raises:
            TypeError: A strategy or requirement switch has an invalid type.
            ValueError: Required strategies are absent, constraint names repeat or context identity differs.
        """
        self._strategies = _strategy_components(strategies, require_strategies)
        self.identity, self.events, self.api, self.connections = identity, events, api, connections
        self.adaptations = adaptations if adaptations is not None else (api if isinstance(api, AdaptationReporter) else None)
        self.service_levels = service_levels if service_levels is not None else (api if isinstance(api, ServiceLevelReporter) else None)
        self.context = context if context is not None else WorkloadContext(identity, node_id=identity.node, runtime_node_name=identity.node)
        if self.context.identity != identity:
            raise ValueError("workload context must match the service identity")
        self.settings = settings or Settings()
        self.telemetry = telemetry if telemetry is not None else Telemetry()
        self._clock, self._checkpoint = clock, checkpoint
        self._state = State(identity, self.settings)
        self._lock = threading.RLock()
        self._operation = threading.Lock()
        self._published = self._state.view(clock())
        self._hooks: list[tuple[Callable[[Change], None], tuple[str, ...], Filter | None]] = [
            (partial(self._adapt_strategy, strategy), (), None) for strategy in self._strategies
        ]
        self._hooks.append((self.adapt, (), None))
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

        The base invokes this after injected strategies and before optional hooks
        and checkpoint persistence. Strategies can prepare application intent;
        this hook can reconcile it through the application's own supervisor.
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
        environ: Mapping[str, str] | None = None,
        strategies: Sequence[AdaptationStrategy] = (),
        require_strategies: bool = True,
        telemetry: Telemetry | None = None,
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
            environ (Mapping[str, str] | None): Explicit environment; defaults to the exported SDK import-time snapshot.
            strategies (Sequence[AdaptationStrategy]): Ordered application components supplied to the subclass.
            require_strategies (bool): Require at least one strategy; false explicitly permits undefined adaptation for uncovered cases.
            telemetry (Telemetry | None): Instrumentation shared by the service and its constructed API clients.
            settings (Settings | None): Application observation settings.
            timeout (float): Finite HTTP and stream read timeout.
            max_event_bytes (int): Maximum received event size, validated by Client.
            checkpoint (Callable[[str], None] | None): Successful-hook cursor persistence callback.
            allow_unauthenticated (bool): Explicitly permit clients without credentials for an administrator-enabled demo.

        Returns:
            Self: Configured concrete subclass; construction performs no network calls or thread startup.

        Raises:
            TypeError: A strategy or requirement switch has an invalid type.
            ValueError: Required strategies are absent, constraint names repeat or environment configuration is invalid.
        """
        components = _strategy_components(strategies, require_strategies)
        environment = dict(sdk_environment if environ is None else environ)
        context = WorkloadContext.from_environment(environment, cluster=cluster)
        identity = context.identity
        instrumentation = telemetry if telemetry is not None else Telemetry()

        def client(name: str, *, required: bool = False) -> Client | None:
            url = getattr(context, f"{name.lower()}_url")
            token = environment.get(f"POLYAD_{name}_TOKEN")
            filename = environment.get(f"POLYAD_{name}_TOKEN_FILE", "")
            if name == "CONNECTIONS" and not filename and not token:
                projected = Path("/var/run/polyad-connections/token")
                filename = str(projected) if projected.is_file() else ""
            if not url or (not token and not filename and not allow_unauthenticated):
                if required:
                    raise ValueError(f"configure POLYAD_{name}_URL and an application-owned token or token file")
                return None
            provider = (lambda: Path(filename).read_text().strip()) if filename else None
            return Client(
                url,
                token,
                timeout=timeout,
                max_event_bytes=max_event_bytes,
                token_provider=provider,
                identity_cluster=cluster,
                telemetry=instrumentation,
            )

        events = client("EVENTS", required=True)
        assert events is not None
        api_client = client("API")
        return cls(
            identity,
            events,
            api=api_client,
            adaptations=api_client,
            connections=client("CONNECTIONS"),
            strategies=components,
            require_strategies=require_strategies,
            context=context,
            telemetry=instrumentation,
            settings=settings,
            checkpoint=checkpoint,
        )

    @property
    def strategies(self) -> tuple[AdaptationStrategy, ...]:
        """
        Return the component order fixed when this service was constructed.

        Returns:
            tuple[AdaptationStrategy, ...]: Components preceding the application adapt hook.
        """
        return self._strategies

    def _adapt_strategy(self, strategy: AdaptationStrategy, change: Change) -> None:
        name = type(strategy).__name__
        reporter = self.adaptations
        definition = self.context.definition
        position = next(index for index, component in enumerate(self._strategies) if component is strategy)
        invocation = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(
                    (
                        self.identity.graphUid,
                        definition.uid if definition is not None else "inline",
                        self.context.pod.uid or self.context.runtime_node_name or "local",
                        self.identity.node,
                        str(position),
                        name,
                        self._pending_cursor or "baseline",
                    )
                ),
            )
        )

        def report(phase: Literal["Running", "Succeeded", "Failed"]) -> None:
            if reporter is None or definition is None or self.context.definition_generation is None:
                return
            reporter.report_adaptation(
                AdaptationReport(
                    self.identity.graph,
                    self.identity.graphUid,
                    definition.name,
                    definition.uid,
                    self.context.definition_generation,
                    cast("Literal['Workload', 'Daemon']", definition.kind),
                    self.identity.node,
                    invocation,
                    name,
                    phase,
                    datetime.now(UTC).isoformat(),
                    cast("Literal['Graph', 'PolyGraph', 'ReplicaGroup']", self.identity.kind),
                )
            )

        with self.telemetry.operation("adaptation.strategy", attributes={"strategy": name}):
            report("Running")
            try:
                strategy.adapt(change, self.view)
            except BaseException:
                report("Failed")
                raise
            else:
                report("Succeeded")

    @property
    def view(self) -> Environment:
        """
        Build a read-only snapshot from the observations the SDK currently holds.

        Each read checks observation age and connection expiry at the current
        time. It makes no network request; the observation loop and refresh()
        fetch newer topology information. Saving the result preserves that view
        for comparison. Read this property again before acting on older data.

        Use the snapshot to inspect potential destinations and plan a response.
        Permission to communicate, reserve resources or change the graph still
        depends on the relevant authorization and application checks.

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
        with self.telemetry.operation("adaptation.delivery", attributes={"baseline": change.baseline}):
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
                    if index < len(self._strategies):
                        callback(delivered)
                    else:
                        stage = "adaptation.apply" if index == len(self._strategies) else "adaptation.hook"
                        with self.telemetry.operation(stage):
                            callback(delivered)
                    self._handled.add(index)
            if cursor is not None and cursor != self._cursor and self._checkpoint is not None:
                with self.telemetry.operation("adaptation.checkpoint"):
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
        Ask Polyad for permission to communicate with a discovered service.

        The returned connection receipt is a record of the request, with a unique
        ID, status and expiry time. Track its updates through service.view and
        wait for Active before sending work. Your application also checks that
        the destination is ready and uses its own HTTP, gRPC or other transport.

        Args:
            target (ServiceEndpoint): Destination returned by permitted service discovery.
            request_id (str): Application-chosen request ID; reuse it with identical content when retrying.
            ttl_seconds (int): Requested permission lifetime in seconds, including time spent waiting for approval.
            ports (tuple[NetworkPort, ...]): Requested destination ports.
            bidirectional (bool): Also request a reverse edge.

        Returns:
            dict[str, Any]: Connection request record; wait for its status to become Active before using it.
        """
        if self.connections is None:
            raise RuntimeError("configure a separately authorized connections client")
        request = ServiceConnectionRequest(request_id, self.identity, target, ttl_seconds, ports, bidirectional)
        return self.connections.connect_services(request)

    def respond(self, uid: str, decision: Literal["Approve", "Reject"]) -> dict[str, Any]:
        """
        Approve or reject a connection request this service has received.

        Args:
            uid (str): Unique ID assigned by the operator to the connection receipt, available from connection events.
            decision (Literal['Approve', 'Reject']): Your application's answer after checking whether to accept this connection.

        Returns:
            dict[str, Any]: Recorded answer. The operator checks permissions and graph rules before making the connection Active.
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

    def report_service_level(self, report: ServiceLevelReport) -> dict[str, Any]:
        """
        Report application-measured service compliance for this Daemon definition.

        Args:
            report (ServiceLevelReport): Availability, quality and capability measurements for one window.

        Returns:
            dict[str, Any]: Current operator-evaluated service-level status.
        """
        if self.service_levels is None:
            raise RuntimeError("configure a separately authorized service-level reporter")
        definition = self.context.definition
        if definition is None or self.context.definition_generation is None or definition.kind != "Daemon":
            raise RuntimeError("service-level reporting requires a projected Daemon definition")
        if (report.graph, report.graphUid, report.graphKind) != (self.identity.graph, self.identity.graphUid, self.identity.kind):
            raise ValueError("service-level report targets a different graph boundary")
        if (report.target, report.targetUid, report.targetGeneration) != (
            definition.name,
            definition.uid,
            self.context.definition_generation,
        ):
            raise ValueError("service-level report targets a different Daemon definition")
        if report.node != self.identity.node:
            raise ValueError("service-level report targets a different logical node")
        return self.service_levels.report_service_level(report)
