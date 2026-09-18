"""
Translate mock application arrivals into real, graph-scoped activation requests.
"""

from __future__ import annotations

import argparse
import hmac
import json
import math
import os
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from polyad_benchmarks.config import operator_client
from polyad_benchmarks.identity import log_event, parent_run_id
from polyad_client import APIError

if TYPE_CHECKING:
    from typing import Any


class FixtureServer(ThreadingHTTPServer):
    """
    Bound request threads and pin all pulses to one node in the injected containing graph.

    Attributes:
        daemon_threads (bool): Permit process shutdown without waiting indefinitely for clients.
    """

    daemon_threads: bool = True

    def __init__(self, address: tuple[str, int], workers: int = 16) -> None:
        """
        Bind a test-only HTTP listener with finite admission and request timeouts.

        Args:
            address (tuple[str, int]): Internal listener address and port.
            workers (int): Maximum simultaneous accepted sockets.
        """
        if not 1 <= workers <= 128:
            raise ValueError("fixture workers must be between 1 and 128")
        self.address_family = socket.AF_INET6 if ":" in address[0] else socket.AF_INET
        self.slots = threading.BoundedSemaphore(workers)
        super().__init__(address, FixtureHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """
        Reject excess sockets without creating an unbounded thread or work queue.

        Args:
            request (Any): Accepted TCP socket from HTTPServer.
            client_address (Any): Socket peer identity supplied by socketserver.

        Returns:
            None: Accepted connections run in bounded threads.
        """
        request.settimeout(10)
        if not self.slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 0\r\n\r\n")
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """
        Release admission even when a client disconnects during its request.

        Args:
            request (Any): Accepted TCP socket from HTTPServer.
            client_address (Any): Socket peer identity supplied by socketserver.

        Returns:
            None: Connection cleanup is delegated to the standard server.
        """
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class FixtureHandler(BaseHTTPRequestHandler):
    """
    Serve health and mock work without permitting callers to select another graph or target.
    """

    def reply(self, status: int, payload: dict[str, Any]) -> None:
        """
        Return bounded JSON without forwarding upstream credentials or response bodies.

        Args:
            status (int): HTTP result code.
            payload (dict[str, Any]): Public fixture response.

        Returns:
            None: Write the complete response and close the connection.
        """
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """
        Expose a Kubernetes health probe without creating work.

        Returns:
            None: Only the health path succeeds.
        """
        self.reply(200 if self.path == "/healthz" else 404, {"healthy": self.path == "/healthz"})

    def do_POST(self) -> None:
        """
        Submit a pulse to the fixed batch vertex using the fixture's graph identity.

        Returns:
            None: Echo only the request identity and receipt outcome.
        """
        if self.path != "/v1/activations":
            self.reply(404, {"error": "unknown route"})
            return
        token = os.environ.get("POLYAD_BENCHMARK_FIXTURE_TOKEN")
        if token and not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
            self.reply(401, {"error": "fixture credential required"})
            return
        run_id = None
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 4096:
                self.reply(413, {"error": "use a JSON body of at most 4096 bytes"})
                return
            document = json.loads(self.rfile.read(length))
            identity = document["requestId"]
            if not isinstance(identity, str) or not re.fullmatch(r".+-[0-9]{5}", identity) or len(identity) > 112:
                raise ValueError("request ID requires a run identity and numbered arrival")
            run_id = parent_run_id(identity)
            receipt = operator_client(5).activate(
                request_id=identity,
                graph=os.environ["POLYAD_GRAPH_NAME"],
                graph_uid=os.environ["POLYAD_GRAPH_UID"],
                node="batch",
            )
            log_event("benchmark.request.submitted", run_id, requestId=identity, receiptName=receipt.get("name"))
            self.reply(202, receipt)
        except APIError as error:
            if run_id is not None:
                log_event("benchmark.request.rejected", run_id, requestId=identity, httpStatus=error.status)
            self.reply(error.status, {"error": "operator rejected submission"})
        except (ValueError, TypeError, KeyError):
            self.reply(400, {"error": "invalid request or missing graph context"})
        except OSError:
            if run_id is not None:
                log_event("benchmark.request.uncertain", run_id, requestId=identity)
            self.reply(502, {"error": "operator unavailable; reuse requestId when retrying"})

    def log_message(self, format: str, *args: Any) -> None:
        """
        Keep study logs free of untrusted request strings and per-request noise.

        Args:
            format (str): Standard server log format.
            *args (Any): Ignored request log arguments.

        Returns:
            None: Runner results capture outcomes and error counts instead.
        """


def main() -> None:
    """
    Start the fixture service or perform one finite mock batch inside a managed Job.

    Returns:
        None: A batch exits after its configured processing delay.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--delay", type=float, default=0.1)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    if not math.isfinite(args.delay) or not 0 <= args.delay <= 3600:
        parser.error("delay must be finite and between 0 and 3600 seconds")
    if args.once:
        time.sleep(args.delay)
        activation = os.environ.get("POLYAD_ACTIVATION_ID", "")
        print(json.dumps({"completed": True, "activation": activation, "runId": parent_run_id(activation) if activation else None}))
        return
    # This is a private benchmark fixture, not an externally exposed application server.
    with FixtureServer((os.environ.get("POLYAD_POD_IP", "127.0.0.1"), args.port)) as server:
        print(json.dumps({"fixture": "ready", "port": args.port}), flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
