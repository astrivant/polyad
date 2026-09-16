"""
Serve metrics on a bounded listener separate from intake, events and health probes.
"""

from __future__ import annotations

import asyncio
from threading import Event, Thread
from typing import TYPE_CHECKING

from waitress import wasyncore
from waitress.server import create_server

from polyad.metrics.builder import MetricsAPIBuilder
from polyad.operator.pressure import pressure

if TYPE_CHECKING:
    from polyad.metrics.store import MetricsStore


class MetricsServer:
    """
    Host cached telemetry without blocking the scheduler event loop.
    """

    def __init__(self, store: MetricsStore, *, token: str | None = None, host: str = "0.0.0.0", port: int = 8092) -> None:
        """
        Bind a dedicated listener and start its worker thread.

        Args:
            store (MetricsStore): Shared snapshot source.
            token (str | None): Optional dedicated metrics bearer credential.
            host (str): Listening address.
            port (int): Listening port.
        """
        builder = MetricsAPIBuilder().with_store(store)
        if token is not None:
            builder = builder.with_bearer_token(token)
        self.stopping = Event()
        self.sockets: wasyncore._SocketMap = {}
        self.server = create_server(
            pressure.wrap(builder.build()),
            map=self.sockets,
            host=host,
            port=port,
            threads=2,
            connection_limit=32,
            channel_timeout=10,
            max_request_body_size=1024,
        )
        self.thread = Thread(target=self.run, name="polyad-metrics-api", daemon=False)
        self.thread.start()

    def run(self) -> None:
        """
        Serve until shutdown and close workers and sockets.

        Returns:
            None: No return value.
        """
        try:
            while not self.stopping.is_set():
                wasyncore.loop(timeout=0.5, count=1, map=self.sockets)
        finally:
            wasyncore.close_all(self.sockets)
            self.server.task_dispatcher.shutdown(timeout=10)

    async def close(self) -> None:
        """
        Stop and join HTTP workers before operator teardown.

        Returns:
            None: No return value.
        """
        self.stopping.set()
        await asyncio.to_thread(self.thread.join)
