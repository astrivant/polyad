"""
Run optional shared read replicas without starting reconciliation, leases or intake.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

from polyad.api.observations import ObservationAPI, build_app, observe
from polyad.api.server import APIServer


async def run() -> None:
    """
    Serve local observations with read-only credentials until process termination.

    Returns:
        None: The shared HTTP transport is joined before closing its Kubernetes client.
    """
    cluster = os.environ["POLYAD_CLUSTER_NAME"]
    namespace = os.environ["POLYAD_NAMESPACE"]
    token = Path(os.environ["POLYAD_OBSERVER_TOKEN_FILE"]).read_text().strip()
    if not cluster or not namespace:
        raise ValueError("observers require cluster and namespace identities")
    api = ObservationAPI()
    server = APIServer(api)
    app = build_app(lambda kind, name: server.invoke(observe(api, cluster, namespace, kind, name)), token)
    server.start(app, host="0.0.0.0", port=8094, name="observations")
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    try:
        await stopped.wait()
    finally:
        await server.close()


def main() -> None:
    """
    Start the standalone read-only process without importing operator handlers.

    Returns:
        None: Process exits after graceful HTTP shutdown.
    """
    asyncio.run(run())


if __name__ == "__main__":
    main()
