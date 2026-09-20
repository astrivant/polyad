"""
Run Polyad's Kubernetes graph operator with the configured capabilities.

Own process signals on the main thread, embed Kopf on a dedicated thread and
manage startup and shutdown of the enabled operator services.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import threading
from typing import TYPE_CHECKING

import kopf

from polyad.operator.lifecycle.health import lifecycle
from polyad.operator.lifecycle.probes import health_endpoint
from polyad.operator.observability.logging import add_logging_options, configure_log_export, configure_logging, shutdown_log_export
from polyad.operator.observability.tracing import configure_tracing, shutdown_tracing

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from types import FrameType

__all__ = (
    "OperatorThread",
    "main",
)


class OperatorThread:
    """
    Run an async operator on its own event loop with a thread-safe stop flag.
    """

    def __init__(self, operator: Callable[..., Awaitable[None]] = kopf.operator, **options: object) -> None:
        """
        Bind runtime options; the owning process controls start and shutdown.

        Args:
            operator (Callable[..., Awaitable[None]]): Async operator entry point to run on the owned thread.
            **options (object): Keyword options forwarded to the async operator.
        """
        self.operator, self.options = operator, options
        self.stop_flag = threading.Event()
        self.ready_flag = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="polyad-kopf", daemon=False)

    def _run(self) -> None:
        """
        Keep event-loop creation and teardown entirely on the operator thread.

        Returns:
            None: No return value.
        """

        async def run() -> None:
            await self.operator(stop_flag=self.stop_flag, ready_flag=self.ready_flag, **self.options)

        try:
            asyncio.run(run())
        except BaseException as error:
            self.error = error

    def start(self) -> None:
        """
        Start the owned thread without blocking the main process.

        Returns:
            None: No return value.
        """
        self.thread.start()

    def stop(self) -> None:
        """
        Request cooperative Kopf cleanup without deleting workload resources.

        Returns:
            None: No return value.
        """
        self.stop_flag.set()

    def join(self) -> None:
        """
        Wait for thread cleanup and propagate failures to the process supervisor.

        Returns:
            None: No return value.
        """
        self.thread.join()
        if self.error:
            raise RuntimeError("Kopf operator thread failed") from self.error


def main() -> None:
    """
    Launch the Python-owned operator process and forward termination signals.

    Returns:
        None: No return value.
    """

    # Import registers handlers before Kopf starts its event loop.
    from polyad.operator.lifecycle import handlers  # noqa: F401

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=os.environ.get("POLYAD_NAMESPACE", "default"))
    parser.add_argument("--liveness", help="Health URL on POLYAD_POD_IP; defaults to port 8080 and /healthz")
    add_logging_options(parser)
    args = parser.parse_args()
    try:
        endpoint = health_endpoint(args.liveness)
    except ValueError as error:
        parser.error(str(error))
    if os.environ.get("POLYAD_ROOT_WORKER", "false").lower() == "true":
        if os.environ.get("POLYAD_ROOT_ENABLED", "false").lower() != "true" or not os.environ.get("KUBECONFIG"):
            parser.error("root workers require root mode and an explicit root kubeconfig")

        # Never accidentally coordinate against the cluster hosting this worker Pod.
        os.environ.pop("KUBERNETES_SERVICE_HOST", None)
        os.environ.pop("KUBERNETES_SERVICE_PORT", None)
    configure_logging(parser, args.log_level)
    os.environ["POLYAD_NAMESPACE"] = args.namespace
    logger = logging.getLogger(__name__)
    logger.debug("Starting operator namespace=%s log_level=%s", args.namespace, args.log_level)
    logger.info(
        "Operator health listener ready to bind pod=%s node=%s endpoint=%s",
        os.environ.get("POLYAD_POD_NAME", "local"),
        os.environ.get("POLYAD_KUBERNETES_NODE_NAME", "local"),
        endpoint,
    )
    runtime = OperatorThread(standalone=True, namespaces=[args.namespace], liveness_endpoint=endpoint)

    def stop(signum: int, frame: FrameType | None) -> None:
        """
        Forward main-thread process signals through the shared stop event.

        Args:
            signum (int): Signal received by the main process.
            frame (FrameType | None): Interrupted Python frame, if available.

        Returns:
            None: No return value.
        """
        logger.debug("Process signal received signal=%s replacement=%s", signal.Signals(signum).name, signum == signal.SIGHUP)
        if signum == signal.SIGHUP:
            lifecycle.replacement.set()
        else:
            lifecycle.draining.set()
            runtime.stop()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
    try:
        configure_tracing()
        configure_log_export()
        runtime.start()
        runtime.join()
    finally:
        try:
            runtime.stop()
            if runtime.thread.is_alive():
                runtime.join()
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            shutdown_tracing()
            shutdown_log_export()


if __name__ == "__main__":
    main()
