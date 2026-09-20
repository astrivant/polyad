"""
Exercise optional workload protocols, explicit mesh declarations and consent fencing.
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import tomllib
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from attrs import asdict, evolve

from polyad_sdk import WorkloadClient, WorkloadEndpoint
from polyad_sdk.connections.authorization import authorize_connection
from polyad_sdk.symbiosis.models import Environment, freeze
from polyad_types import ServiceEndpoint

ROOT = Path(__file__).resolve().parents[2]
HOME = ServiceEndpoint("", "test", "Graph", "pipeline", "uid-pipeline", "source")
PEER = evolve(HOME, node="sink")


def endpoint(uri="grpc://worker:50051", **kwargs):
    """
    Bind each transport under test to the same logical peer.
    """
    return WorkloadEndpoint(PEER, uri, **kwargs)


@pytest.mark.parametrize(
    "scheme,port,app",
    [
        ("http", 80, "http"),
        ("https", 443, "https"),
        ("ws", 80, "http"),
        ("wss", 443, "https"),
        ("grpc", 50051, "grpc"),
        ("grpcs", 50051, "tls"),
        ("amqp", 5672, "tcp"),
        ("amqps", 5671, "tls"),
        ("redis", 6379, "tcp"),
        ("rediss", 6379, "tls"),
        ("tcp", 9000, "tcp"),
        ("tls", 9000, "tls"),
    ],
)
def test_protocol_ports_and_istio_selection(scheme, port, app):
    """
    Never mislabel broker traffic or opaque application TLS as inspectable HTTP.
    """
    target = endpoint(f"{scheme}://worker:{port}", target_port=12345)
    assert target.network_port.port == 12345 and target.network_port.protocol == "TCP"
    assert target.service_port("worker") == {
        "name": f"{app}-worker",
        "port": port,
        "targetPort": 12345,
        "protocol": "TCP",
        "appProtocol": app,
    }
    if scheme not in {"tcp", "tls"}:
        assert endpoint(f"{scheme}://worker").port == port


@pytest.mark.parametrize(
    "uri",
    [
        "udp://worker:123",
        "tcp://worker",
        "tls://worker",
        "grpc://",
        "grpc://worker:0",
        "grpc://worker:65536",
        "grpc://worker:",
        "grpc://worker:bad",
        "http://a@worker",
        "redis://user:password@worker",
        "grpcs://worker?verify=false",
        "ws://worker#part",
        "ws://worker?",
        "amqp://worker/vhost",
        "redis://worker/2",
        "grpc://worker/service",
        " http://worker",
        "http://wor\nker",
        "http://worker\\other",
        "http://worker%2fother",
        "http://worker:80/path\x00",
    ],
)
def test_ambiguous_or_unsafe_endpoints_fail_before_io(uri):
    """
    Keep credentials, protocol options and hidden authorities out of URI parsing.
    """
    with pytest.raises(ValueError):
        endpoint(uri)


@pytest.mark.parametrize("value", [True, 0, -1, 65536, "9000"])
def test_invalid_target_ports(value):
    """
    Consent uses a validated numeric Pod port, never a guessed Service port.
    """
    with pytest.raises(ValueError):
        endpoint(target_port=value)


@pytest.fixture
def receipt():
    """
    Supply the real local connection receipt shape with an explicit TCP grant.
    """
    return {
        "uid": "receipt-uid",
        "namespace": "test",
        "expiresAt": datetime.fromtimestamp(120, UTC).isoformat(),
        "status": {"phase": "Active"},
        "revokeRequested": False,
        "target": {
            "kind": "Graph",
            "graph": "pipeline",
            "graphUid": "uid-pipeline",
            "source": "source",
            "target": "sink",
            "ports": [{"port": 50051, "protocol": "TCP"}],
            "bidirectional": False,
        },
    }


def authorize(receipt, *, home=HOME, target=None, available=True, now=100):
    """
    Freeze public observations as the real adaptive service does before checking them.
    """
    view = Environment(None, {}, freeze({"receipt-uid": receipt}), available, None)
    authorize_connection(home, target or endpoint(), view, "receipt-uid", now)


def test_receipts_require_current_permission_identity_direction_and_pod_port(receipt):
    """
    A previously valid receipt must not authorize a different graph, port or direction.
    """
    authorize(receipt)
    authorize(receipt, target=endpoint("grpc://worker:80", target_port=50051))
    for kwargs in (
        {"available": False},
        {"now": 120},
        {"target": endpoint("grpc://worker:80")},
        {"target": WorkloadEndpoint(evolve(PEER, graphUid="replacement"), "grpc://worker")},
        {"home": PEER, "target": WorkloadEndpoint(HOME, "grpc://worker")},
    ):
        with pytest.raises(PermissionError):
            authorize(receipt, **kwargs)
    receipt["target"]["bidirectional"] = True
    authorize(receipt, home=PEER, target=WorkloadEndpoint(HOME, "grpc://worker"))


@pytest.mark.parametrize(
    "patch",
    [
        {"uid": "wrong"},
        {"status": {"phase": "Pending"}},
        {"status": None},
        {"revokeRequested": True},
        {"expiresAt": "bad"},
        {"expiresAt": "2099-01-01"},
        {"target": None},
        {"peers": {"source": {}}},
    ],
)
def test_unusable_receipts_fail_closed(receipt, patch):
    """
    Incomplete evidence never falls back to unrestricted transport access.
    """
    receipt.update(patch)
    with pytest.raises(PermissionError):
        authorize(receipt)


@pytest.mark.parametrize("ports", [[], [{"port": 50051, "protocol": "UDP"}], [{"port": 80}], None])
def test_receipt_requires_tcp_port(receipt, ports):
    """
    Structural and UDP edges do not authorize a TCP socket.
    """
    receipt["target"]["ports"] = ports
    with pytest.raises(PermissionError):
        authorize(receipt)


def test_atlas_receipt_uses_complete_participant_identities(receipt):
    """
    Cross-cluster peers cannot be confused with a same-named local service.
    """
    remote = evolve(PEER, cluster="west", namespace="remote", graphUid="remote-uid")
    target = WorkloadEndpoint(remote, "grpcs://remote:50051")
    receipt["peers"] = {"source": asdict(HOME), "target": asdict(remote)}
    authorize(receipt, target=target)
    with pytest.raises(PermissionError):
        authorize(receipt)
    receipt["peers"]["source"]["cluster"] = "east"
    with pytest.raises(PermissionError):
        authorize(receipt, target=target)


def test_imports_remain_lazy_and_optional_metadata_is_explicit():
    """
    Importing every SDK module does not load optional protocol libraries.
    """
    code = """
import importlib, pkgutil, sys
import polyad_sdk
for module in pkgutil.walk_packages(polyad_sdk.__path__, 'polyad_sdk.'):
    importlib.import_module(module.name)
assert not {'grpc', 'pika', 'redis'}.intersection(sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True)
    metadata = tomllib.loads((ROOT / "pkg/polyad-sdk/pyproject.toml").read_text())["project"]
    assert not any(value.startswith(("grpcio", "pika", "redis")) for value in metadata["dependencies"])
    extras = metadata["optional-dependencies"]
    assert set(extras["protocols"]) == set(extras["grpc"] + extras["amqp"] + extras["redis"])


@pytest.mark.parametrize(
    "module,extra,method,uri,kwargs",
    [
        ("grpc", "grpc", "grpc_channel", "grpc://worker", {}),
        ("pika", "amqp", "amqp", "amqp://worker", {"username": "app", "password": "secret"}),
        ("redis", "redis", "redis", "redis://worker", {}),
    ],
)
def test_missing_optional_clients_explain_installation(monkeypatch, module, extra, method, uri, kwargs):
    """
    An unavailable adapter fails only when explicitly opened and names its install extra.
    """
    original = importlib.import_module

    def missing(name):
        if name == module:
            raise ModuleNotFoundError(name=module)
        return original(name)

    monkeypatch.setattr(importlib, "import_module", missing)
    client = WorkloadClient(endpoint(uri))
    with pytest.raises(ImportError, match=rf"polyad-sdk\[{extra}\]"):
        with getattr(client, method)(**kwargs):
            pytest.fail("missing dependency opened a connection")


@pytest.mark.parametrize(
    "method,uri,kwargs",
    [
        ("http", "http://worker", {}),
        ("websocket", "ws://worker", {}),
        ("grpc_channel", "grpc://worker", {}),
        ("amqp", "amqp://worker", {"username": "app", "password": "secret"}),
        ("redis", "redis://worker", {}),
        ("tcp", "tcp://worker:1234", {}),
    ],
)
def test_admission_precedes_optional_imports_and_io(method, uri, kwargs):
    """
    A rejected grant cannot initiate even a transport handshake.
    """
    guard = MagicMock(side_effect=PermissionError("denied"))
    client = WorkloadClient(endpoint(uri), authorize=guard)
    with pytest.raises(PermissionError, match="denied"):
        with getattr(client, method)(**kwargs):
            pytest.fail("admission was bypassed")
    guard.assert_called_once_with()


@pytest.mark.parametrize(
    "method,uri", [("http", "https://worker"), ("websocket", "wss://worker"), ("amqp", "amqps://worker"), ("tcp", "tls://worker:9000")]
)
def test_unverified_tls_is_rejected(method, uri):
    """
    Workload convenience APIs never silently disable certificate or hostname checks.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    options = {"tls": context}
    if method == "amqp":
        options.update(username="app", password="secret")
    with pytest.raises(ValueError, match="verify"):
        with getattr(WorkloadClient(endpoint(uri)), method)(**options):
            pytest.fail("insecure TLS was accepted")


def test_native_grpc_cleanup_and_bounded_options(monkeypatch):
    """
    Failed readiness and permission rechecks close channels without write retries or proxies.
    """
    grpc = MagicMock()
    monkeypatch.setattr("polyad_sdk.connections.client._optional", lambda *_: grpc)
    guard = MagicMock(side_effect=[None, PermissionError("revoked")])
    client = WorkloadClient(endpoint("grpc://[::1]:50051"), timeout=2, authorize=guard)
    with pytest.raises(PermissionError, match="revoked"):
        with client.grpc_channel():
            pytest.fail("revoked during handshake")
    grpc.insecure_channel.return_value.close.assert_called_once_with()
    args, kwargs = grpc.insecure_channel.call_args
    assert args == ("[::1]:50051",)
    assert dict(kwargs["options"])["grpc.enable_retries"] == 0
    assert dict(kwargs["options"])["grpc.enable_http_proxy"] == 0
    grpc.channel_ready_future.return_value.result.assert_called_once_with(timeout=2)
    grpc.channel_ready_future.return_value.result.side_effect = TimeoutError
    with pytest.raises(TimeoutError), WorkloadClient(endpoint()).grpc_channel():
        pytest.fail("unready channel escaped")
    assert grpc.insecure_channel.return_value.close.call_count == 2


def test_native_amqp_parameters_and_cleanup(monkeypatch):
    """
    Application credentials and virtual hosts remain separate from operator configuration.
    """
    pika = MagicMock()
    monkeypatch.setattr("polyad_sdk.connections.client._optional", lambda *_: pika)
    context = ssl.create_default_context()
    with WorkloadClient(endpoint("amqps://broker"), timeout=3).amqp(username="app", password="secret", virtual_host="jobs", tls=context):
        pass
    parameters = pika.ConnectionParameters.call_args.kwargs
    assert parameters["host"] == "broker" and parameters["port"] == 5671
    assert parameters["virtual_host"] == "jobs" and parameters["connection_attempts"] == 1
    assert parameters["socket_timeout"] == 3 and parameters["stack_timeout"] == 6
    pika.PlainCredentials.assert_called_once_with("app", "secret")
    pika.SSLOptions.assert_called_once_with(context, "broker")
    pika.BlockingConnection.return_value.close.assert_called_once_with()


def test_native_redis_parameters_and_failure_cleanup(monkeypatch):
    """
    Bound the private Redis pool and release it even if its initial ping fails.
    """
    redis = MagicMock()
    monkeypatch.setattr("polyad_sdk.connections.client._optional", lambda *_: redis)
    redis.Redis.return_value.ping.side_effect = OSError("unreachable")
    with pytest.raises(OSError), WorkloadClient(endpoint("rediss://cache")).redis(username="app", password="secret", database=2):
        pytest.fail("unreachable Redis escaped")
    options = redis.ConnectionPool.call_args.kwargs
    assert options["max_connections"] == 1 and options["db"] == 2
    assert options["ssl_check_hostname"] is True and options["ssl_cert_reqs"] == "required"
    assert options["retry"]._retries == 0
    redis.Redis.return_value.close.assert_called_once_with()
    redis.ConnectionPool.return_value.disconnect.assert_called_once_with()


def test_optional_native_broker_constructors(monkeypatch):
    """
    Validate the installed clients' real parameter classes without an external broker.
    """
    pika = pytest.importorskip("pika")
    redis = pytest.importorskip("redis")
    broker = MagicMock()
    monkeypatch.setattr(pika, "BlockingConnection", broker)
    with WorkloadClient(endpoint("amqps://worker")).amqp(username="app", password="secret"):
        parameters = broker.call_args.args[0]
        assert isinstance(parameters, pika.ConnectionParameters)
        assert parameters.ssl_options.server_hostname == "worker"
        assert parameters.ssl_options.context.verify_mode == ssl.CERT_REQUIRED
        assert parameters.connection_attempts == 1
    monkeypatch.setattr(redis.Redis, "ping", lambda _: True)
    with WorkloadClient(endpoint("rediss://worker")).redis() as client:
        connection = client.connection_pool.make_connection()
        assert isinstance(connection, redis.SSLConnection)
        assert connection.check_hostname and connection.cert_reqs == ssl.CERT_REQUIRED
        assert connection.socket_timeout == 10 and connection.retry._retries == 0


@pytest.fixture
def tls_material(tmp_path):
    """
    Create a short-lived test identity trusted only by explicitly configured clients.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False
        )
        .sign(key, hashes.SHA256())
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    certificate_path, key_path = tmp_path / "certificate.pem", tmp_path / "key.pem"
    certificate_path.write_bytes(certificate_pem)
    key_path.write_bytes(key_pem)
    server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server.load_cert_chain(certificate_path, key_path)
    client = ssl.create_default_context(cafile=str(certificate_path))
    return server, client, certificate_pem, key_pem


@contextmanager
def local_server(handler):
    """
    Own one loopback listener and propagate background server failures to the test.
    """
    errors = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(5)

        def serve():
            try:
                with listener.accept()[0] as connection:
                    connection.settimeout(5)
                    handler(connection)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            yield listener.getsockname()[1]
        finally:
            thread.join(timeout=6)
            assert not thread.is_alive()
            if errors:
                raise errors[0]


def test_loopback_tcp_and_http():
    """
    Exchange real bytes and HTTP responses without a Kubernetes control plane.
    """

    def echo(connection):
        connection.sendall(connection.recv(1024))

    with local_server(echo) as port, WorkloadClient(endpoint(f"tcp://127.0.0.1:{port}")).tcp() as connection:
        connection.sendall(b"hello")
        assert connection.recv(5) == b"hello"

    def respond(connection):
        request = connection.recv(4096)
        assert b"Authorization:" not in request
        connection.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")

    with local_server(respond) as port, WorkloadClient(endpoint(f"http://127.0.0.1:{port}")).http() as connection:
        connection.request("GET", "/health")
        assert connection.getresponse().read(2) == b"ok"


def test_loopback_tls_trust_and_rejection(tls_material):
    """
    Negotiate verified TLS successfully and reject an untrusted application certificate.
    """
    server, client, _, _ = tls_material

    def echo(connection):
        with server.wrap_socket(connection, server_side=True) as secure:
            secure.sendall(secure.recv(1024))

    with local_server(echo) as port, WorkloadClient(endpoint(f"tls://127.0.0.1:{port}")).tcp(tls=client) as connection:
        connection.sendall(b"hello")
        assert connection.recv(5) == b"hello"

    def untrusted(connection):
        with pytest.raises(ssl.SSLError):
            server.wrap_socket(connection, server_side=True)

    with local_server(untrusted) as port:
        with pytest.raises(ssl.SSLCertVerificationError), WorkloadClient(endpoint(f"tls://127.0.0.1:{port}")).tcp():
            pytest.fail("untrusted TLS certificate accepted")


def test_loopback_websocket_and_no_redirects():
    """
    Complete a real workload handshake and refuse a redirected endpoint.
    """
    from websockets.sync.server import serve

    with serve(lambda connection: connection.send(connection.recv(timeout=3)), "127.0.0.1", 0) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.socket.getsockname()[1]
            with WorkloadClient(endpoint(f"ws://127.0.0.1:{port}/work")).websocket() as connection:
                connection.send("hello")
                assert connection.recv(timeout=3) == "hello"
        finally:
            server.shutdown()
            thread.join(timeout=5)

    def redirect(connection):
        connection.recv(4096)
        connection.sendall(b"HTTP/1.1 302 Found\r\nLocation: ws://unapproved.invalid:9000\r\nContent-Length: 0\r\n\r\n")

    from websockets.exceptions import InvalidStatus

    with local_server(redirect) as port:
        with pytest.raises(InvalidStatus), WorkloadClient(endpoint(f"ws://127.0.0.1:{port}")).websocket():
            pytest.fail("redirect followed")


@pytest.mark.parametrize("secure", [False, True])
def test_loopback_optional_grpc(secure, tls_material):
    """
    Exercise the native channel against a real in-process unary gRPC service.
    """
    from concurrent.futures import ThreadPoolExecutor

    grpc = pytest.importorskip("grpc")
    with ThreadPoolExecutor(max_workers=1) as executor:
        server = grpc.server(executor)
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "test.Echo",
                    {
                        "Echo": grpc.unary_unary_rpc_method_handler(lambda request, _: request),
                    },
                ),
            )
        )
        _, _, certificate, key = tls_material
        if secure:
            port = server.add_secure_port("127.0.0.1:0", grpc.ssl_server_credentials(((key, certificate),)))
        else:
            port = server.add_insecure_port("127.0.0.1:0")
        server.start()
        try:
            scheme = "grpcs" if secure else "grpc"
            with WorkloadClient(endpoint(f"{scheme}://127.0.0.1:{port}")).grpc_channel(
                root_certificates=certificate if secure else None
            ) as channel:
                assert channel.unary_unary("/test.Echo/Echo")(b"hello", timeout=3) == b"hello"
        finally:
            server.stop(0).wait(timeout=5)


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm")
@pytest.mark.parametrize("scheme", ["http", "https", "ws", "wss", "grpc", "grpcs", "amqp", "amqps", "redis", "rediss", "tcp", "tls"])
def test_helm_workload_helper_matches_sdk(tmp_path, scheme):
    """
    Keep Python and Helm application protocol declarations in lockstep.
    """
    (tmp_path / "templates").mkdir()
    (tmp_path / "Chart.yaml").write_text("apiVersion: v2\nname: test\nversion: 0.1.0\n")
    shutil.copyfile(ROOT / "charts/polyad/templates/shared/_workload-protocols.tpl", tmp_path / "templates/_ports.tpl")
    (tmp_path / "templates/service.yaml").write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: test\nspec:\n  ports:\n    - "
        '{{ include "polyad.workloadServicePort" .Values.port | nindent 6 }}\n'
    )
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"port": {"protocol": scheme, "port": 8080, "targetPort": 9000, "name": "app"}}))
    result = subprocess.check_output(["helm", "template", "test", str(tmp_path), "-f", str(values)], text=True)
    assert yaml.safe_load(result)["spec"]["ports"] == [endpoint(f"{scheme}://worker:8080", target_port=9000).service_port("app")]
    for change in ({"protocol": "mqtt"}, {"targetPort": 0}, {"port": 65536}, {"targetPort": True}, {"name": "BAD"}):
        values.write_text(json.dumps({"port": {"protocol": scheme, "port": 8080, **change}}))
        assert subprocess.run(["helm", "template", "test", str(tmp_path), "-f", str(values)], capture_output=True).returncode != 0
