"""
Run optional shared read replicas without starting reconciliation, leases or intake.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal

from polyad.api.http.server import APIServer
from polyad.api.observations.app import ObservationAPI, build_app, observe
from polyad.operator.lifecycle.health import credential_token
from polyad.operator.observability.logging import add_logging_options, configure_log_export, configure_logging, shutdown_log_export
from polyad.operator.observability.tracing import configure_tracing, shutdown_tracing


async def run() -> None:
    """
    Serve local observations with read-only credentials until process termination.

    Returns:
        None: The shared HTTP transport is joined before closing its Kubernetes client.
    """
    cluster = os.environ["POLYAD_CLUSTER_NAME"]
    namespace = os.environ["POLYAD_NAMESPACE"]
    token = credential_token("OBSERVER").strip()
    if not cluster or not namespace:
        raise ValueError("observers require cluster and namespace identities")
    logging.getLogger(__name__).debug("Starting observer cluster=%s namespace=%s", cluster, namespace)
    api = ObservationAPI()
    server = APIServer(api)
    build_app(
        lambda kind, name: server.invoke(observe(api, cluster, namespace, kind, name)),
        token,
        access=server.access,
        application=server.app,
    )
    server.start(ports={"observations": 8094})
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
    parser = argparse.ArgumentParser(description=__doc__)
    add_logging_options(parser)
    args = parser.parse_args()
    configure_logging(parser, args.log_level)
    try:
        configure_tracing()
        configure_log_export()
        asyncio.run(run())
    finally:
        shutdown_tracing()
        shutdown_log_export()


if __name__ == "__main__":
    main()
