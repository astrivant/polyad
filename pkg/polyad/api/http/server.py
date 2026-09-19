"""
Own one Flask application and shared HTTP/event transport per operator process.
"""

from __future__ import annotations

import asyncio
import logging
import os
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import TYPE_CHECKING, TypeVar

from flask import abort, g, jsonify, request
from waitress import wasyncore
from waitress.server import create_server
from waitress.task import ThreadedTaskDispatcher

from polyad.api.http.application import create_application
from polyad.api.http.errors import RequestError, Unavailable
from polyad.api.http.limits import RateLimitPolicy
from polyad.auth.http import Access
from polyad.operator.coordination.pulses import PulseDeferred
from polyad.operator.lifecycle.health import lifecycle
from polyad.operator.observability.pressure import pressure

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from concurrent.futures import Future
    from typing import Any

    from flask import Response

    from polyad.api.connections.store import ConnectionSettings
    from polyad.events.store import EventStore
    from polyad.metrics.store import MetricsStore
    from polyad.operator.adapters.kubernetes import API

logger = logging.getLogger(__name__)
T = TypeVar("T")


class APIServer:
    """
    Bridge bounded WSGI workers into the operator loop and retain uncertain writes.
    """

    def __init__(self, api: API) -> None:
        """
        Initialize shared transport bookkeeping before assembling a domain application.

        Args:
            api (API): Dedicated intake adapter with its own write backlog.
        """
        self.api = api
        self.app = create_application()
        self.access = Access.from_environment()
        self.ports: dict[str, int] = {}
        self.stream_slots = 0
        self.websockets = False
        self.server: Any = None
        self.started = False
        self.closed = False
        self.loop = asyncio.get_running_loop()
        self.stopping = Event()
        self.slots = BoundedSemaphore(32)
        self.read_slots = BoundedSemaphore(128)
        self.lock = Lock()
        self.pending: set[Future[Any]] = set()
        self.closers: list[Callable[[], None]] = []
        self.background: list[asyncio.Task[None]] = []

    def start(self, *, host: str = "0.0.0.0", ports: dict[str, int] | None = None) -> None:
        """
        Start one shared dispatcher after every enabled endpoint family has been registered.

        Args:
            host (str): HTTP listening address for all enabled endpoint ports.
            ports (dict[str, int] | None): Optional listener overrides, including a single ephemeral test port.

        Returns:
            None: One serving thread owns every listener and HTTP worker.
        """
        if self.started or self.closed:
            raise RuntimeError("API runtime can only be started once")
        selected = self.ports if ports is None else ports
        if not selected or set(selected) != set(self.app.blueprints) or len(set(selected.values())) != len(selected):
            raise ValueError("each enabled API family requires its own listener port")
        self.app.extensions["polyad.ports"] = {str(port): name for name, port in selected.items()}

        @self.app.before_request
        def retiring() -> tuple[Response, int] | None:
            if lifecycle.replacement.is_set() or lifecycle.draining.is_set():
                return jsonify(error="replica is retiring; reconnect to a healthy replica"), 503
            return None

        if self.websockets:
            from polyad.api.http.websocket import WebSocketServer

            self.server = WebSocketServer(self.app, host, selected, self.stream_slots, self.stopping)
            port_domains = {str(port): name for name, port in selected.items() if port}
            ephemeral = next((name for name, port in selected.items() if port == 0), "unavailable")
            self.app.extensions["polyad.ports"] = {
                str(port): port_domains.get(str(port), ephemeral) for _, port in self.server.effective_listen
            }
            self.started = True
            self.thread = Thread(target=self.run, name="polyad-http", daemon=False)
            self.thread.start()
            return

        self.sockets: wasyncore._SocketMap = {}
        dispatcher = ThreadedTaskDispatcher()
        dispatcher.set_thread_count(self.stream_slots + 8)
        address = f"[{host}]" if ":" in host and not host.startswith("[") else host
        try:
            self.server = create_server(
                pressure.wrap(self.app),
                map=self.sockets,
                _dispatcher=dispatcher,
                listen=" ".join(f"{address}:{port}" for port in selected.values()),
                threads=self.stream_slots + 8,
                max_request_body_size=1024 * 1024,
                connection_limit=self.stream_slots + 64,
                channel_timeout=30,
                outbuf_high_watermark=65536,
            )
        except Exception:
            dispatcher.shutdown()
            wasyncore.close_all(self.sockets)
            raise
        listeners = getattr(self.server, "effective_listen", None)
        if listeners is None:
            single: Any = self.server
            listeners = [(single.effective_host, single.effective_port)]
        port_domains = {str(port): name for name, port in selected.items() if port}
        ephemeral = next((name for name, port in selected.items() if port == 0), "unavailable")
        self.app.extensions["polyad.ports"] = {str(port): port_domains.get(str(port), ephemeral) for _, port in listeners}
        self.started = True
        self.thread = Thread(target=self.run, name="polyad-http", daemon=False)
        self.thread.start()

    def invoke(self, operation: Coroutine[Any, Any, T], *, timeout: int = 30, retain: bool = True) -> T:
        """
        Bound outstanding operations and retain timed-out writes until acknowledgement.

        Args:
            operation (Coroutine[Any, Any, T]): Domain authentication, receipt submission or audit read.
            timeout (int): Maximum synchronous wait for acknowledgement.
            retain (bool): Keep uncertain writes alive; read-only calls may be cancelled on timeout.

        Returns:
            T: Acknowledged result, preserving expected client-facing errors.
        """
        slots = self.slots if retain else self.read_slots
        if self.stopping.is_set() or not slots.acquire(blocking=False):
            operation.close()
            raise Unavailable("API intake is stopping or full")
        future = asyncio.run_coroutine_threadsafe(operation, self.loop)
        with self.lock:
            self.pending.add(future)
        future.add_done_callback(lambda completed: self.finished(completed, slots))
        try:
            return future.result(timeout=timeout)
        except (RequestError, PulseDeferred, ValueError, TypeError):
            raise
        except Exception as error:
            if not retain:
                future.cancel()
                raise
            logger.warning("API operation failed or timed out: %s", type(error).__name__)
            raise Unavailable("Kubernetes acknowledgement unavailable") from error

    def finished(self, future: Future[Any], slots: BoundedSemaphore) -> None:
        """
        Release an intake slot only after the operation has actually finished.

        Args:
            future (Future[Any]): Completed event-loop operation.
            slots (BoundedSemaphore): Read or write admission budget held by this operation.

        Returns:
            None: No return value.
        """
        with self.lock:
            self.pending.discard(future)
        slots.release()

    def run(self) -> None:
        """
        Serve until shutdown, then close HTTP workers and sockets.

        Returns:
            None: No return value.
        """
        if self.websockets:
            self.server.run()
            return
        try:
            while not self.stopping.is_set():
                wasyncore.loop(timeout=0.5, count=1, map=self.sockets)
        finally:
            self.server.task_dispatcher.shutdown(timeout=35)
            wasyncore.close_all(self.sockets)

    async def close(self) -> None:
        """
        Stop HTTP intake and join outstanding operations while the operator loop is alive.

        Returns:
            None: No return value.
        """
        if self.closed:
            return
        for task in self.background:
            task.cancel()
        await asyncio.gather(*self.background, return_exceptions=True)
        self.background.clear()
        self.closed = True
        self.stopping.set()
        if self.started:
            await asyncio.to_thread(self.thread.join)
        with self.lock:
            pending = list(self.pending)
        await asyncio.gather(*(asyncio.wrap_future(future) for future in pending), return_exceptions=True)
        for close in self.closers:
            close()
        self.api.client.close()
        for routes in self.app.extensions["polyad.routes"].values():
            limiter = routes.extensions.get("polyad.limiter")
            if limiter is not None and limiter.enabled:
                limiter.storage.storage.close()
        if self.access is not None:
            await asyncio.to_thread(self.access.close)

    def composition(self, namespace: str, token: str) -> None:
        """
        Register composition, activation and throughput intake without starting a server.

        Args:
            namespace (str): Fixed namespace served by this replica.
            token (str): Namespace bearer credential or named-key enrollment marker.

        Returns:
            None: Routes share the process application and Kubernetes intake adapter.
        """
        from polyad.api.composition.builder import APIBuilder
        from polyad.api.composition.store import CompositionStore
        from polyad.api.workloads.activations import ActivationStore
        from polyad.api.workloads.adaptation import report_adaptation
        from polyad.api.workloads.throughput import report_throughput

        api = self.api
        store = CompositionStore(api, namespace)
        activations = ActivationStore(api, namespace)
        (
            APIBuilder(
                access=self.access,
                throughput=lambda value: self.invoke(
                    report_throughput(api, namespace, value, g.polyad_key.graphs if getattr(g, "polyad_key", None) else None)
                ),
                adaptation=lambda value: self.invoke(
                    report_adaptation(api, namespace, value, g.polyad_key.graphs if getattr(g, "polyad_key", None) else None)
                ),
            )
            .with_handlers(lambda value: self.invoke(store.submit(value)) or {}, lambda key, audit: self.invoke(store.lookup(key, audit)))
            .with_activation_handlers(
                lambda value: self.invoke(activations.submit(value)) or {},
                lambda key: self.invoke(activations.lookup(key)),
                lambda key: self.invoke(activations.stop(key)),
            )
            .with_bearer_token(token)
            .with_rate_limits(RateLimitPolicy.from_environment(namespace))
            .build(self.app)
        )
        self.ports["composition"] = 8090

    def connections(self, settings: ConnectionSettings) -> None:
        """
        Register temporary connections with separate service-account authentication.

        Args:
            settings (ConnectionSettings): Endpoint namespace and TTL policy.

        Returns:
            None: Connection routes share the process application.
        """
        from polyad.api.connections.app import build_app as connection_app
        from polyad.api.connections.store import ConnectionStore

        store = ConnectionStore(self.api, settings)
        self.closers.append(store.federation.close)
        connection_app(
            lambda token: self.invoke(store.authenticate(token, request.headers.get("X-Polyad-Cluster", ""))),
            lambda value, caller: self.invoke(store.submit(value, caller)),
            lambda namespace, identity, caller: self.invoke(store.lookup(namespace, identity, caller)),
            lambda namespace, identity, caller: self.invoke(store.revoke(namespace, identity, caller)),
            services=lambda value, caller: self.invoke(store.connect_services(value, caller)),
            respond=lambda namespace, identity, response, caller: self.invoke(store.respond(namespace, identity, response, caller)),
            limits=RateLimitPolicy.from_environment(settings.operator_namespace + ":connections"),
            application=self.app,
        )
        self.ports["connections"] = 8093

    def events(
        self,
        store: EventStore,
        namespace: str,
        token: str,
        *,
        connections: int = 16,
        clusters: dict[str, EventStore] | None = None,
        websockets: bool = False,
    ) -> None:
        """
        Register bounded streaming readers and reserve non-streaming HTTP worker capacity.

        Args:
            store (EventStore): Local shared observation stream.
            namespace (str): Namespace served by this replica.
            token (str): Subscriber credential or named-key enrollment marker.
            connections (int): Maximum active streams; enforced even in unauthenticated demos.
            clusters (dict[str, EventStore] | None): Root-held streams for registered clusters.
            websockets (bool): Enable WebSocket subscriptions on the same events listener as SSE.

        Returns:
            None: Streaming routes use the same server and shutdown signal.
        """
        from polyad.api.events.builder import EventAPIBuilder
        from polyad.events.discovery import Directory
        from polyad.events.rebalance import Rebalancer, configuration
        from polyad.events.settings import settings_from_environment
        from polyad.operator.clusters.federation import Federation

        federation = Federation(self.api)
        self.closers.append(federation.close)
        directory = Directory(self.api, namespace, federation, {federation.name: store, **(clusters or {})})
        policy = configuration()
        event_settings = settings_from_environment()
        rebalance = Rebalancer(policy, poll_interval=event_settings.pollIntervalSeconds) if policy.enabled else None
        if rebalance is not None:
            service = os.environ.get("POLYAD_EVENTS_SERVICE", "")
            if not service:
                raise ValueError("event rebalancing requires POLYAD_EVENTS_SERVICE")
            self.background.append(asyncio.create_task(rebalance.watch(self.api, namespace, service)))

        def selected() -> EventStore:
            cluster = request.args.get("cluster")
            if cluster is None or cluster == federation.name:
                return store
            if cluster not in (clusters or {}):
                abort(404, "cluster is not registered")
            assert clusters is not None
            return clusters[cluster]

        (
            EventAPIBuilder(
                settings=event_settings,
                rebalance=rebalance,
                websockets=websockets,
                stopping=self.stopping,
                max_connections=connections,
                limits=RateLimitPolicy.from_environment(namespace + ":events"),
                access=self.access,
                authorize_stream=lambda key, cluster: self.invoke(directory.authorize_stream(key, cluster), timeout=7, retain=False),
                permits=lambda key, identity: self.invoke(directory.permits(key, identity), timeout=7, retain=False),
                discover=lambda key, target, offset, limit: self.invoke(
                    directory.discover(key, target, offset=offset, limit=limit), timeout=25, retain=False
                ),
            )
            .with_handlers(
                lambda cursor: self.invoke(selected().cursor(cursor), timeout=7, retain=False),
                lambda cursor: self.invoke(selected().read(cursor), timeout=7, retain=False),
            )
            .with_topology_handler(
                lambda kind, name, uid, node: self.invoke(selected().topology(kind, name, uid, node), timeout=7, retain=False)
            )
            .with_bearer_token(token)
            .build(self.app)
        )
        self.stream_slots = connections
        self.websockets = websockets
        self.ports["events"] = 8091

    def metrics(self, store: MetricsStore, token: str | None = None) -> None:
        """
        Register cached telemetry on the process application.

        Args:
            store (MetricsStore): Thread-safe metrics snapshot source.
            token (str | None): Optional read-only metrics credential.

        Returns:
            None: Metrics use shared HTTP workers and credential storage.
        """
        from polyad.api.metrics.builder import MetricsAPIBuilder

        builder = MetricsAPIBuilder(access=self.access).with_store(store)
        if token:
            builder = builder.with_bearer_token(token)
        builder.build(self.app)
        self.ports["metrics"] = 8092
