"""
Exercise the standalone client against real Flask routes through an HTTP adapter.
"""

from __future__ import annotations

import io
import json
from unittest.mock import MagicMock
from urllib.error import HTTPError

import pytest

from polyad.api import APIBuilder
from polyad_sdk import APIError, Client
from polyad_types import CompositionItem, CompositionRequest, Event, to_dict


def test_websocket_client_headers_checkpoints_and_cleanup(monkeypatch):
    """
    Preserve cluster selection and rotating headers while callbacks checkpoint only observations.
    """
    from polyad_sdk import StreamInterrupted
    from polyad_sdk.events.filters import event_type
    from polyad_sdk.transport import websocket

    connector = MagicMock()
    connection = connector.return_value.__enter__.return_value
    connection.recv.side_effect = [
        '{"id":"","event":"heartbeat","data":{}}',
        '{"id":"2-0","event":"topology","data":{"name":"root"}}',
        '{"id":"","event":"reset","data":{"reason":"expired"}}',
    ]
    monkeypatch.setattr(websocket, "_NoRedirect", connector)
    client = Client("https://events.example", "old", token_provider=lambda: "fresh", identity_cluster="east", timeout=7)
    calls = []
    subscription = client.subscribe(transport="websocket", cluster="west", cursor="1-0")
    subscription.on(event_type("topology"), lambda event: calls.append(event.id))
    with pytest.raises(StreamInterrupted):
        subscription.run()
    assert calls == ["2-0"] and subscription.cursor == "2-0"
    assert connector.call_args.args == ("wss://events.example/v1/events/ws?cluster=west",)
    kwargs = connector.call_args.kwargs
    assert kwargs["additional_headers"] == {
        "Authorization": "Bearer fresh",
        "X-Polyad-Cluster": "east",
        "Last-Event-ID": "1-0",
        "Accept": "text/event-stream",
    }
    assert kwargs["max_queue"] == 1 and kwargs["max_size"] == 1024 * 1024
    assert kwargs["open_timeout"] == 7 and kwargs["compression"] is None
    connector.return_value.__exit__.assert_called_once()


@pytest.mark.parametrize("frame", [b"binary", "[]", '{"event":"graph","data":{}}', '{"id":"1-0","event":"graph","data":[]}'])
def test_websocket_client_rejects_invalid_frames_and_closes(monkeypatch, frame):
    """
    Binary frames and invalid envelopes cannot reach application callbacks.
    """
    from polyad_sdk.transport import websocket

    connector = MagicMock()
    connector.return_value.__enter__.return_value.recv.return_value = frame
    monkeypatch.setattr(websocket, "_NoRedirect", connector)
    with pytest.raises(ValueError):
        next(Client("http://events", "token").events(transport="websocket"))
    connector.return_value.__exit__.assert_called_once()


def test_websocket_handshake_errors_keep_http_status(monkeypatch):
    """
    Expired replay has the same APIError contract through either transport.
    """
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from polyad_sdk.transport import websocket

    connector = MagicMock(side_effect=InvalidStatus(Response(410, "Gone", Headers(), bytearray(b'{"error":"expired"}'))))
    monkeypatch.setattr(websocket, "_NoRedirect", connector)
    with pytest.raises(APIError) as error:
        next(Client("http://events", "token").events(transport="websocket"))
    assert error.value.status == 410 and error.value.body == {"error": "expired"}


def test_websocket_redirects_cannot_move_an_authenticated_subscription(monkeypatch):
    """
    Subscription origins remain pinned even when a peer or proxy returns a redirect.
    """
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from polyad_sdk.transport import websocket

    redirect = InvalidStatus(Response(302, "Found", Headers({"Location": "wss://other.example/events"})))
    attempt = MagicMock(side_effect=redirect)
    monkeypatch.setattr(websocket._NoRedirect, "open_connection", attempt)
    with pytest.raises(APIError) as error:
        next(Client("https://events", "token").events(transport="websocket"))
    assert error.value.status == 302
    attempt.assert_called_once()


class Adapter:
    """
    Dispatch urllib requests to Flask without requiring a local listening socket.
    """

    def __init__(self, app):
        """
        Retain the test client and recorded requests.
        """
        self.client, self.calls = app.test_client(), []

    def open(self, request, timeout):
        """
        Return a readable HTTP response or urllib-compatible status failure.
        """
        self.calls.append((request, timeout))
        result = self.client.open(request.selector, method=request.method, data=request.data, headers=dict(request.header_items()))
        if result.status_code >= 400:
            raise HTTPError(request.full_url, result.status_code, "HTTP error", result.headers, io.BytesIO(result.data))
        return io.BytesIO(result.data)


def test_client_calls_authenticated_activation_and_composition_routes():
    """
    Preserve request identity and keep HTTP status available to explicit retry logic.
    """
    app = (
        APIBuilder()
        .with_handlers(lambda request: {"requestId": request.requestId}, lambda key, audit: {"requestId": key, "audit": audit})
        .with_activation_handlers(
            lambda request: {"requestId": request.requestId}, lambda key: {"requestId": key}, lambda key: {"stopped": key}
        )
        .with_bearer_token("secret")
        .build()
    )
    client = Client("http://polyad:8090", "secret", timeout=4)
    adapter = Adapter(app)
    client._opener = adapter
    assert client.activate(request_id="pulse", graph="graph", graph_uid="uid", node="work") == {"requestId": "pulse"}
    assert client.activation("pulse") == {"requestId": "pulse"}
    assert client.stop("pulse") == {"stopped": "pulse"}
    assert client.composition("composition", resources=True)["audit"]
    request = CompositionRequest("composition", "root", (CompositionItem("root", "Graph", {"nodes": []}),))
    assert client.compose(request) == {"requestId": "composition"}
    assert json.loads(adapter.calls[-1][0].data) == to_dict(request)
    assert client.compose(to_dict(request)) == {"requestId": "composition"}
    assert "/v1/activations" in client.openapi()["paths"]
    assert all(timeout == 4 for _, timeout in adapter.calls)
    assert json.loads(adapter.calls[0][0].data)["graphUid"] == "uid"
    bad = Client("http://polyad:8090", "wrong")
    bad._opener = adapter
    with pytest.raises(APIError) as error:
        bad.activation("pulse")
    assert error.value.status == 401
    assert "wrong" not in str(error.value)


def test_client_streams_cursors_and_control_messages():
    """
    Parse SSE frames and close the response when the stream finishes.
    """
    client = Client("http://events:8091", "token")
    response = io.BytesIO(b': heartbeat\n\nid: 1-0\nevent: graph\ndata: {"name":\ndata: "root"}\n\nevent: reset\ndata: {}\n\n')

    class Stream:
        def open(self, request, timeout):
            assert request.get_header("Last-event-id") == "0-0"
            return response

    client._opener = Stream()
    events = list(client.events(last_event_id="0-0"))
    assert isinstance(events[0], Event)
    assert events[0].id == "1-0" and events[0].data == {"name": "root"}
    assert events[1].event == "reset"
    assert response.closed


def test_client_reads_shared_observer_snapshots():
    """
    Use the standalone client's existing authentication transport for optional read replicas.
    """
    from polyad.api.observations.app import build_app

    client = Client("http://observer:8094", "read-token")
    client._opener = Adapter(build_app(lambda kind, name: {"kind": kind, "name": name, "cluster": "west"}, "read-token"))
    assert client.observe("workflow", kind="PolyGraph") == {"kind": "PolyGraph", "name": "workflow", "cluster": "west"}


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:password@example.org", "http://example.org?token=secret"])
def test_client_rejects_unsafe_base_urls(url):
    """
    Keep credentials out of endpoint URLs and prohibit non-HTTP transports.
    """
    with pytest.raises(ValueError):
        Client(url, "token")
