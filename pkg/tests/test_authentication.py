"""
Verify credential direction, scope, rotation, destination pinning and shared lane admission.
"""

from __future__ import annotations

import io
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock
from uuid import uuid4

import pytest
from flask import Flask, Response

from polyad.api import APIBuilder
from polyad.auth.http import Access, install
from polyad.auth.keys import Keyring
from polyad.auth.lanes import LaneFull, Lanes, Permit
from polyad.auth.outbound import NoRedirect, OutboundClient
from polyad_types import APIKey, KeyDirection
from polyad_types.api.auth import Authentication
from polyad_types.serialization import from_dict
from tests.test_composition_api import document


def registry(tmp_path):
    """
    Give the two groups independent identities with all three credential directions.
    """
    groups = {"services": [], "operators": []}
    for group in groups:
        (tmp_path / group).mkdir()
        for direction in KeyDirection:
            name = direction.value.lower()
            key = {
                "name": name,
                "direction": direction.value,
                "existingSecret": name,
                "requestsPerMinute": 5,
                "maxConcurrentRequests": 2,
            }
            if direction != KeyDirection.OUTBOUND:
                key["endpoints"] = ["composition", "events"]
            if direction != KeyDirection.INBOUND:
                key["baseUrl"] = "https://peer.example/api"
            groups[group].append(key)
            (tmp_path / group / name).write_text(f"{group}-{name}-token")
    path = tmp_path / "config.json"
    path.write_text(json.dumps(groups))
    return Keyring(path)


def access(keys):
    """
    Observe admission and release separately from real shared-storage integration tests.
    """
    lanes = Mock()
    lanes.acquire.side_effect = lambda *_: Permit("test")
    return Access(keys, lanes)


def header(group="services", direction="inbound"):
    """
    Select a named credential without exposing its group in an untrusted identity header.
    """
    return {"Authorization": f"Bearer {group}-{direction}-token"}


@pytest.mark.parametrize("group", ["services", "operators"])
@pytest.mark.parametrize(("direction", "code"), [("inbound", 202), ("bidirectional", 202), ("outbound", 403)])
def test_inbound_keys_are_grouped_and_direction_scoped(tmp_path, group, direction, code):
    """
    Only inbound capabilities reach mutation handlers and acquire a lane.
    """
    auth = access(registry(tmp_path))
    submit = Mock(return_value={"requestId": "accepted"})
    app = APIBuilder(access=auth).with_handlers(submit, lambda *_: None).build()
    response = app.test_client().post("/v1/compositions", json=document(), headers=header(group, direction))
    assert response.status_code == code
    assert submit.call_count == auth.lanes.acquire.call_count == (1 if code == 202 else 0)
    if code == 202:
        assert auth.lanes.acquire.call_args.args[0] == group
        assert auth.lanes.acquire.call_args.args[1].name == direction
        auth.lanes.release.assert_called_once()


def test_unknown_credentials_and_wrong_scopes_do_not_consume_quota(tmp_path):
    """
    Authentication precedes rate storage and scope denial precedes business handlers.
    """
    auth = access(registry(tmp_path))
    app = Flask(__name__)
    install(app, "composition", "old-token", auth)
    app.add_url_rule("/", view_func=lambda: "accepted")
    assert app.test_client().get("/", headers={"Authorization": "Bearer old-token"}).status_code == 401
    assert app.test_client().get("/").status_code == 401
    auth.lanes.acquire.assert_not_called()
    policy = json.loads(auth.keys.path.read_text())
    policy["services"][0]["endpoints"] = ["events"]
    auth.keys.path.write_text(json.dumps(policy))
    assert app.test_client().get("/", headers=header()).status_code == 403
    auth.lanes.acquire.assert_not_called()


def test_rotation_keeps_lane_identity_and_revocation_fails_closed(tmp_path):
    """
    Projected token changes take effect on the next request without resetting stable key identity.
    """
    auth = access(registry(tmp_path))
    app = APIBuilder(access=auth).with_handlers(lambda *_: {}, lambda *_: None).build()
    client = app.test_client()
    assert client.get("/openapi.json", headers=header()).status_code == 200
    identity = auth.lanes.acquire.call_args.args
    token = tmp_path / "services/inbound"
    token.write_text("rotated-token")
    assert client.get("/openapi.json", headers=header()).status_code == 401
    assert client.get("/openapi.json", headers={"Authorization": "Bearer rotated-token"}).status_code == 200
    assert auth.lanes.acquire.call_args.args == identity
    token.unlink()
    response = client.get("/openapi.json", headers={"Authorization": "Bearer rotated-token"})
    assert response.status_code == 503 and "rotated-token" not in response.text


def test_projected_revision_is_pinned_and_duplicate_tokens_rejected(tmp_path):
    """
    Symlink projections resolve policy and tokens from the same mounted revision.
    """
    root = tmp_path / "revision"
    root.mkdir()
    keys = registry(root)
    (tmp_path / "config.json").symlink_to(keys.path)
    assert Keyring(tmp_path / "config.json").read() == keys.read()
    (root / "operators/inbound").write_text((root / "services/inbound").read_text())
    with pytest.raises(ValueError, match="exactly one"):
        keys.read()


def test_quota_and_storage_failures_never_dispatch(tmp_path):
    """
    Exhaustion returns 429 while unavailable lane storage returns a credential-free 503.
    """
    auth = access(registry(tmp_path))
    submit = Mock(return_value={})
    app = APIBuilder(access=auth).with_handlers(submit, lambda *_: None).build()
    auth.lanes.acquire.side_effect = LaneFull(12)
    response = app.test_client().post("/v1/compositions", json=document(), headers=header())
    assert response.status_code == 429 and response.headers["Retry-After"] == "12"
    auth.lanes.acquire.side_effect = RuntimeError("private-connection-details")
    response = app.test_client().post("/v1/compositions", json=document(), headers=header())
    assert response.status_code == 503 and "private" not in response.text
    submit.assert_not_called()


def test_stream_concurrency_is_held_until_close_and_stops_on_lost_lease(tmp_path):
    """
    Streaming responses retain their permit beyond the HTTP handler and reject expired ownership.
    """
    auth = access(registry(tmp_path))
    permit = Permit("test")
    auth.lanes.acquire.side_effect = None
    auth.lanes.acquire.return_value = permit
    app = Flask(__name__)
    install(app, "events", "", auth)
    app.add_url_rule("/", view_func=lambda: Response(iter(["first", "second"])))
    response = app.test_client().get("/", headers=header(), buffered=False)
    assert next(response.response) == b"first"
    auth.lanes.release.assert_not_called()
    permit.lost.set()
    with pytest.raises(RuntimeError, match="lease"):
        next(response.response)
    response.close()
    assert auth.lanes.release.call_count >= 1


@pytest.mark.parametrize("direction", ["outbound", "bidirectional"])
def test_outbound_calls_pin_destinations_and_share_the_key_lane(tmp_path, monkeypatch, direction):
    """
    Outbound requests select the named credential, send its bearer header once and release on completion.
    """
    auth = access(registry(tmp_path))
    body = io.BytesIO(b"ok")
    body.status, body.headers = 200, {"Content-Type": "text/plain"}
    opener = Mock()
    opener.open.return_value = body
    monkeypatch.setattr("polyad.auth.outbound.build_opener", lambda *_: opener)
    response = OutboundClient(auth).request("operators", direction, "POST", "work", body=b"{}")
    assert response.status == 200 and response.body == b"ok"
    request = opener.open.call_args.args[0]
    assert request.full_url == "https://peer.example/api/work"
    assert request.get_header("Authorization") == f"Bearer operators-{direction}-token"
    assert auth.lanes.acquire.call_args.args[0] == "operators"
    assert auth.lanes.acquire.call_args.args[1].name == direction
    auth.lanes.release.assert_called_once()
    assert NoRedirect().redirect_request(request, None, 302, "redirect", {}, "https://untrusted.example") is None


@pytest.mark.parametrize(
    "path", ["https://evil.example", "//evil.example", "../admin", "%2e%2e/admin", "%252e%252e/admin", "/admin", "a\\b", "a#fragment"]
)
def test_outbound_paths_cannot_override_destination(tmp_path, path):
    """
    Reject origin and path-prefix escapes before reserving or dispatching a request.
    """
    auth = access(registry(tmp_path))
    with pytest.raises(ValueError, match="beneath"):
        OutboundClient(auth).request("services", "outbound", "GET", path)
    auth.lanes.acquire.assert_not_called()


def test_outbound_inbound_only_keys_and_identity_headers_are_rejected(tmp_path):
    """
    Caller-supplied headers cannot override the selected key's bearer identity or target host.
    """
    auth = access(registry(tmp_path))
    client = OutboundClient(auth)
    with pytest.raises(ValueError, match="outbound"):
        client.request("services", "inbound", "GET")
    for name in ("Authorization", "HOST", "proxy-authorization"):
        with pytest.raises(ValueError, match="override"):
            client.request("services", "outbound", "GET", headers={name: "injected"})
    auth.lanes.acquire.assert_not_called()


@pytest.mark.skipif(not os.environ.get("POLYAD_TEST_DRAGONFLY_URL"), reason="requires isolated Dragonfly for atomic Lua admission")
def test_shared_ha_rate_and_concurrency_lanes():
    """
    Real Lua admission preserves per-key, per-group isolation and aggregate limits across replicas.
    """
    namespace = "keys-" + uuid4().hex
    replicas = [Lanes(os.environ["POLYAD_TEST_DRAGONFLY_URL"], namespace) for _ in range(2)]
    key = APIKey(
        "peer",
        KeyDirection.BIDIRECTIONAL,
        "key",
        requestsPerMinute=5,
        maxConcurrentRequests=2,
        endpoints=("composition",),
        baseUrl="https://peer.example",
    )
    permits = []
    try:
        second = replicas[0].client.time()[0] % 60
        if second > 45:
            time.sleep(60 - second)  # Keep assertions inside one actual Redis rate window.

        def attempt(index):
            try:
                replica = replicas[index % 2]
                permit = replica.acquire("operators", key)
                permits.append((replica, permit))
                return True
            except LaneFull:
                return False

        with ThreadPoolExecutor(max_workers=8) as threads:
            assert sum(threads.map(attempt, range(20))) == 2
        for replica, permit in permits:
            replica.release(permit)
        assert sum(attempt(index) for index in range(2)) == 2
        for replica, permit in permits:
            replica.release(permit)
        assert attempt(0)
        replicas[0].release(permits[-1][1])
        assert not attempt(1)  # All five rate tokens were charged across both replicas.
        other = replicas[1].acquire("services", key)
        replicas[1].release(other)
    finally:
        for replica in replicas:
            replica.close()


def test_public_models_round_trip_all_directions(tmp_path):
    """
    Public types validate both groups without importing operator-specific configuration classes.
    """
    keys = registry(tmp_path)
    model = from_dict(json.loads(keys.path.read_text()), Authentication)
    assert {key.direction for key in model.services} == set(KeyDirection)
    assert len(model.operators) == 3
