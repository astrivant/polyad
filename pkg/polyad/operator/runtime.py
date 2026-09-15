"""Own process signals on the main thread and embed Kopf on a dedicated thread."""

import argparse
import asyncio
import logging
import os
import signal
import threading
from collections.abc import Awaitable, Callable
from types import FrameType

import kopf


class OperatorThread:
    """Run an async operator on its own event loop with a thread-safe stop flag."""

    def __init__(self, operator: Callable[..., Awaitable[None]] = kopf.operator, **options: object) -> None:
        """Bind runtime options; the owning process controls start and shutdown."""
        self.operator, self.options = operator, options
        self.stop_flag = threading.Event()
        self.ready_flag = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="polyad-kopf", daemon=False)

    def _run(self) -> None:
        """Keep event-loop creation and teardown entirely on the operator thread."""

        async def run() -> None:
            await self.operator(stop_flag=self.stop_flag, ready_flag=self.ready_flag, **self.options)

        try:
            asyncio.run(run())
        except BaseException as error:
            self.error = error

    def start(self) -> None:
        """Start the owned thread without blocking the main process."""
        self.thread.start()

    def stop(self) -> None:
        """Request cooperative Kopf cleanup without deleting workload resources."""
        self.stop_flag.set()

    def join(self) -> None:
        """Wait for thread cleanup and propagate failures to the process supervisor."""
        self.thread.join()
        if self.error:
            raise RuntimeError("Kopf operator thread failed") from self.error


def main() -> None:
    """Launch the Python-owned operator process and forward termination signals."""
    # Import registers handlers before Kopf starts its event loop.
    from polyad.operator import handlers  # noqa: F401

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default=os.environ.get("POLYAD_NAMESPACE", "default"))
    parser.add_argument("--liveness", default="http://0.0.0.0:8080/healthz")
    args = parser.parse_args()
    os.environ["POLYAD_NAMESPACE"] = args.namespace
    logging.basicConfig(level=logging.INFO)
    runtime = OperatorThread(standalone=True, namespaces=[args.namespace], liveness_endpoint=args.liveness)

    def stop(signum: int, frame: FrameType | None) -> None:
        """Forward main-thread process signals through the shared stop event."""
        runtime.stop()

    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        runtime.start()
        runtime.join()
    finally:
        runtime.stop()
        if runtime.thread.is_alive():
            runtime.join()
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    main()
