"""
Manage optional synchronous workload transports with explicit connection lifetimes.
"""

from __future__ import annotations

import importlib
import math
import socket
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping
    from types import ModuleType

    from grpc import Channel
    from pika import BlockingConnection
    from redis import Redis
    from websockets.sync.client import ClientConnection

    from polyad_sdk.connections.endpoint import WorkloadEndpoint

__all__ = ("WorkloadClient",)


def _optional(module: str, extra: str) -> ModuleType:
    """
    Import a transport only when requested and explain its optional installation.

    Args:
        module (str): Importable client module.
        extra (str): SDK packaging extra that supplies the module.

    Returns:
        ModuleType: Installed native client library.

    Raises:
        ImportError: The optional client is not installed.
    """
    try:
        return importlib.import_module(module)
    except ModuleNotFoundError as error:
        if error.name != module:
            raise
        raise ImportError(f"Install this workload transport with: pip install 'polyad-sdk[{extra}]'") from error


@dataclass(frozen=True)
class WorkloadClient:
    """
    Open application connections separately from Polyad's operator API client.

    Construction performs no I/O. Each context checks admission before and after
    connecting, and closes its resource on exit, including failed admission.
    Native clients are intentionally exposed: call check() before each new unit
    of work and exit the context on revocation. There is no background watcher,
    per-message authorization interceptor or application-level replay.

    Attributes:
        endpoint (WorkloadEndpoint): Deployment-configured identity and address.
        timeout (float): Connection and socket timeout in seconds; RPC deadlines remain application-owned.
        max_message_bytes (int): WebSocket and gRPC message bound, not a broker queue limit.
        authorize (Callable[[], None] | None): Admission callback; None uses existing static network policy only.
    """

    endpoint: WorkloadEndpoint
    timeout: float = 10
    max_message_bytes: int = 1024 * 1024
    authorize: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        """
        Require finite connection waits and bounded message sizes.

        Returns:
            None: Invalid limits raise ValueError without importing optional clients.
        """
        if type(self.timeout) not in (int, float) or not math.isfinite(self.timeout) or not 0 < self.timeout <= 300:
            raise ValueError("timeout must be finite and between 0 and 300 seconds")
        if type(self.max_message_bytes) is not int or not 1 <= self.max_message_bytes <= 64 * 1024 * 1024:
            raise ValueError("max_message_bytes must be between 1 byte and 64 MiB")

    def check(self) -> None:
        """
        Reevaluate the configured admission guard before accepting new work.

        Returns:
            None: The callback permits work; its denial exception otherwise propagates.
        """
        if self.authorize is not None:
            self.authorize()

    def _opening(self, *schemes: str) -> None:
        """
        Check protocol compatibility before importing a client or opening sockets.

        Args:
            *schemes (str): Application schemes supported by this method.

        Returns:
            None: Protocol and admission checks succeeded.
        """
        if self.endpoint.scheme not in schemes:
            raise ValueError(f"this transport requires one of: {', '.join(schemes)}")
        self.check()

    def _tls(self, context: ssl.SSLContext | None) -> ssl.SSLContext | None:
        """
        Keep TLS verification mandatory without silently enabling plaintext fallback.

        Args:
            context (ssl.SSLContext | None): Optional application trust roots and client certificate.

        Returns:
            ssl.SSLContext | None: Verified TLS context, or None for a plaintext scheme.

        Raises:
            ValueError: A plaintext URI supplies TLS options or verification is disabled.
        """
        if self.endpoint.scheme not in {"https", "wss", "amqps", "tls"}:
            if context is not None:
                raise ValueError("TLS options require a TLS application scheme")
            return None
        context = context or ssl.create_default_context()
        if not context.check_hostname or context.verify_mode != ssl.CERT_REQUIRED:
            raise ValueError("application TLS must verify certificates and hostnames")
        return context

    @contextmanager
    def http(self, *, tls: ssl.SSLContext | None = None) -> Iterator[HTTPConnection]:
        """
        Open a native HTTP connection without redirects, proxies or API credentials.

        Args:
            tls (ssl.SSLContext | None): Optional application TLS trust and client identity.

        Yields:
            HTTPConnection: Connected HTTP or HTTPS client; the caller supplies request paths and bounds response reads.
        """
        self._opening("http", "https")
        context = self._tls(tls)
        connection = (
            HTTPSConnection(self.endpoint.host, self.endpoint.port, timeout=self.timeout, context=context)
            if context
            else HTTPConnection(self.endpoint.host, self.endpoint.port, timeout=self.timeout)
        )
        try:
            connection.connect()
            self.check()
            yield connection
        finally:
            connection.close()

    @contextmanager
    def websocket(
        self, *, headers: Mapping[str, str] | None = None, subprotocols: tuple[str, ...] = (), tls: ssl.SSLContext | None = None
    ) -> Iterator[ClientConnection]:
        """
        Open a workload WebSocket, independently of the operator's events socket.

        Args:
            headers (Mapping[str, str] | None): Explicit application authentication and handshake headers.
            subprotocols (tuple[str, ...]): Application subprotocol preference order.
            tls (ssl.SSLContext | None): Optional application TLS trust and client identity.

        Yields:
            ClientConnection: Native synchronous socket; supply a timeout when receiving.
        """
        self._opening("ws", "wss")

        # The shared redirect-denying handshake cannot escape the configured
        # endpoint. Environment proxies are also disabled for workload traffic.
        from websockets.typing import Subprotocol

        from polyad_sdk.transport.websocket import _NoRedirect

        with _NoRedirect(
            self.endpoint.uri,
            ssl=self._tls(tls),
            proxy=None,
            additional_headers=headers,
            subprotocols=[Subprotocol(value) for value in subprotocols] or None,
            open_timeout=self.timeout,
            close_timeout=self.timeout,
            max_size=self.max_message_bytes,
            max_queue=1,
            compression=None,
        ) as connection:
            self.check()
            yield connection

    @contextmanager
    def grpc_channel(
        self, *, root_certificates: bytes | None = None, private_key: bytes | None = None, certificate_chain: bytes | None = None
    ) -> Iterator[Channel]:
        """
        Open a ready gRPC channel using the optional grpc installation extra.

        Args:
            root_certificates (bytes | None): PEM application trust roots; None uses the gRPC defaults.
            private_key (bytes | None): PEM client key for application mutual TLS.
            certificate_chain (bytes | None): PEM client certificate chain paired with the key.

        Yields:
            Channel: Ready native channel for generated stubs; set a deadline on every RPC.

        Raises:
            ValueError: TLS material is incomplete or supplied to a plaintext endpoint.
        """
        self._opening("grpc", "grpcs")
        if (private_key is None) != (certificate_chain is None):
            raise ValueError("gRPC client key and certificate chain must be supplied together")
        if self.endpoint.scheme == "grpc" and any(value is not None for value in (root_certificates, private_key, certificate_chain)):
            raise ValueError("gRPC TLS credentials require grpcs")
        grpc = _optional("grpc", "grpc")
        host = f"[{self.endpoint.host}]" if ":" in self.endpoint.host else self.endpoint.host
        target = f"{host}:{self.endpoint.port}"
        options = (
            ("grpc.enable_retries", 0),
            ("grpc.enable_http_proxy", 0),
            ("grpc.max_receive_message_length", self.max_message_bytes),
            ("grpc.max_send_message_length", self.max_message_bytes),
        )
        if self.endpoint.scheme == "grpcs":
            credentials = grpc.ssl_channel_credentials(root_certificates, private_key, certificate_chain)
            channel = grpc.secure_channel(target, credentials, options=options)
        else:
            channel = grpc.insecure_channel(target, options=options)
        try:
            readiness = grpc.channel_ready_future(channel)
            try:
                readiness.result(timeout=self.timeout)
            finally:
                # A timed-out readiness subscription must not outlive its caller.
                readiness.cancel()
            self.check()
            yield cast("Channel", channel)
        finally:
            channel.close()

    @contextmanager
    def amqp(
        self, *, username: str, password: str, virtual_host: str = "/", heartbeat: int = 60, tls: ssl.SSLContext | None = None
    ) -> Iterator[BlockingConnection]:
        """
        Open an AMQP 0-9-1 session using the optional amqp installation extra.

        Args:
            username (str): Explicit broker username, unrelated to operator credentials.
            password (str): Explicit broker password, normally read from a mounted Secret.
            virtual_host (str): Broker virtual host; not a URI path.
            heartbeat (int): Positive heartbeat interval in seconds.
            tls (ssl.SSLContext | None): Optional application TLS trust and client identity.

        Yields:
            BlockingConnection: Native Pika connection; the application owns channels, confirms, acknowledgments and heartbeat servicing.
        """
        self._opening("amqp", "amqps")
        if type(heartbeat) is not int or not 1 <= heartbeat <= 65535:
            raise ValueError("heartbeat must be an integer from 1 through 65535 seconds")
        context = self._tls(tls)
        pika = _optional("pika", "amqp")
        parameters = pika.ConnectionParameters(
            host=self.endpoint.host,
            port=self.endpoint.port,
            virtual_host=virtual_host,
            credentials=pika.PlainCredentials(username, password),
            heartbeat=heartbeat,
            ssl_options=pika.SSLOptions(context, self.endpoint.host) if context else None,
            connection_attempts=1,
            socket_timeout=self.timeout,
            stack_timeout=2 * self.timeout,
            blocked_connection_timeout=self.timeout,
        )
        connection = pika.BlockingConnection(parameters)
        try:
            self.check()
            yield cast("BlockingConnection", connection)
        finally:
            if connection.is_open:
                connection.close()

    @contextmanager
    def redis(
        self,
        *,
        username: str | None = None,
        password: str | None = None,
        database: int = 0,
        ca_file: str | None = None,
        cert_file: str | None = None,
        key_file: str | None = None,
    ) -> Iterator[Redis]:
        """
        Open a single-connection Redis client using the optional redis extra.

        Args:
            username (str | None): Optional application ACL username.
            password (str | None): Optional application password, not the operator cache credential.
            database (int): Nonnegative database index.
            ca_file (str | None): Application CA bundle for rediss; None uses system trust.
            cert_file (str | None): Client certificate file for application mutual TLS.
            key_file (str | None): Client private key file paired with cert_file.

        Yields:
            Redis: Connected native client returning bytes; no command retries are configured.

        Raises:
            ValueError: Database or TLS settings are invalid for the selected scheme.
        """
        self._opening("redis", "rediss")
        secure = self.endpoint.scheme == "rediss"
        if type(database) is not int or database < 0:
            raise ValueError("database must be a nonnegative integer")
        if (cert_file is None) != (key_file is None) or (not secure and any(value is not None for value in (ca_file, cert_file, key_file))):
            raise ValueError("Redis TLS requires rediss and paired client certificate/key files")
        redis = _optional("redis", "redis")
        retry = importlib.import_module("redis.retry")
        backoff = importlib.import_module("redis.backoff")

        # Avoid from_url: its query options could override verification and
        # timeout settings. Own the pool so cleanup closes every connection.
        pool = redis.ConnectionPool(
            connection_class=redis.SSLConnection if secure else redis.Connection,
            host=self.endpoint.host,
            port=self.endpoint.port,
            username=username,
            password=password,
            db=database,
            socket_timeout=self.timeout,
            socket_connect_timeout=self.timeout,
            max_connections=1,
            retry=retry.Retry(backoff.NoBackoff(), 0),
            retry_on_timeout=False,
            **(
                {
                    "ssl_ca_certs": ca_file,
                    "ssl_certfile": cert_file,
                    "ssl_keyfile": key_file,
                    "ssl_cert_reqs": "required",
                    "ssl_check_hostname": True,
                }
                if secure
                else {}
            ),
        )
        connection = redis.Redis(connection_pool=pool)
        try:
            connection.ping()
            self.check()
            yield cast("Redis", connection)
        finally:
            try:
                connection.close()
            finally:
                pool.disconnect()

    @contextmanager
    def tcp(self, *, tls: ssl.SSLContext | None = None) -> Iterator[socket.socket]:
        """
        Open a raw TCP or verified TLS socket for application-defined protocols.

        Args:
            tls (ssl.SSLContext | None): Optional trust roots and client certificate for a tls endpoint.

        Yields:
            socket.socket: Connected socket with the configured timeout; framing and read sizes belong to the application.
        """
        self._opening("tcp", "tls")
        context = self._tls(tls)
        with socket.create_connection((self.endpoint.host, self.endpoint.port), timeout=self.timeout) as connection:
            if context is None:
                self.check()
                yield connection
            else:
                with context.wrap_socket(connection, server_hostname=self.endpoint.host) as secure:
                    self.check()
                    yield secure
