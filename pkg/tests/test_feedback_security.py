"""
Verify endpoint separation, graph-tree visibility and secret-free workload assignments.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from threading import Lock
from unittest.mock import Mock

import pytest

from polyad.api import APIBuilder
from polyad.auth.policy import inject_credentials
from polyad.auth.store import CredentialStore
from polyad.events.visibility import observation_ancestry, permitted_observation
from polyad.operator.clusters.federation import INVENTORY, PARENT
from polyad_types import APIKey, GraphAccess
from tests.test_authentication import access, header, registry
from tests.test_operator import FakeAPI, resource


@pytest.mark.parametrize("backend", ["Builtin", "FlaskHTTPAuth"])
def test_route_capabilities_are_separate_on_shared_listeners(tmp_path, monkeypatch, backend):
    """
    Authenticating on composition does not grant activation or throughput writes.
    """
    monkeypatch.setenv("POLYAD_AUTH_BACKEND", backend)
    auth = access(registry(tmp_path))
    app = APIBuilder(access=auth, throughput=Mock()).with_handlers(Mock(), Mock()).build()
    client = app.test_client()
    assert client.post("/v1/activations", json={}, headers=header()).status_code == 403
    assert client.post("/v1/throughput", json={}, headers=header()).status_code == 403
    assert client.get("/openapi.json", headers=header()).status_code == 200
    assert client.get("/openapi.json").status_code == 401


@pytest.mark.parametrize("backend", ["Builtin", "FlaskHTTPAuth"])
def test_authentication_registry_outage_is_unavailable(tmp_path, monkeypatch, backend):
    """
    Both backends distinguish unavailable credential storage from an invalid credential.
    """
    monkeypatch.setenv("POLYAD_AUTH_BACKEND", backend)
    auth = access(registry(tmp_path))
    app = APIBuilder(access=auth).with_handlers(Mock(), Mock()).build()
    monkeypatch.setattr(auth.keys, "read", Mock(side_effect=OSError("unavailable")))
    assert app.test_client().get("/openapi.json", headers=header()).status_code == 503


@pytest.mark.parametrize(
    "change",
    [
        {"generation": True},
        {"offeredPerSecond": True},
        {"completedPerSecond": "50"},
        *({"demand": {"name": "queueDepth", "unit": "jobs", "value": value}} for value in (True, "50", -1, float("inf"))),
        {"demand": {"name": 5, "unit": "jobs", "value": 50}},
    ],
)
def test_throughput_wire_types_are_validated(monkeypatch, change):
    """
    Measurements cannot acquire numeric meaning through lossy boolean or string coercion.
    """
    monkeypatch.setenv("POLYAD_AUTH_MODE", "Disabled")
    report = Mock()
    app = APIBuilder(throughput=report).with_handlers(Mock(), Mock()).build()
    sample = {
        "graph": "pipeline",
        "graphUid": "uid-pipeline",
        "generation": 1,
        "observedAt": datetime.now(UTC).isoformat(),
        "unit": "records",
        "offeredPerSecond": 100,
        "completedPerSecond": 50,
    }
    assert app.test_client().post("/v1/throughput", json={**sample, **change}).status_code == 422
    report.assert_not_called()


def test_workload_assignments_inject_references_without_reading_secrets(tmp_path, monkeypatch):
    """
    A policy-only file suffices even when no bearer Secret is available to the compiler.
    """
    configuration = {
        "services": [
            {
                "name": "client",
                "direction": "Inbound",
                "existingSecret": "root-secret",
                "endpoints": ["events"],
                "workloads": [
                    {"kind": "Daemon", "name": "processor", "namespace": "test", "env": "POLYAD_API_KEY", "secret": "local-secret"}
                ],
            }
        ]
    }
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(configuration))
    monkeypatch.setenv("POLYAD_WORKLOAD_CREDENTIALS_FILE", str(path))
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "west")
    pod = {"spec": {"containers": [{"name": "main"}]}}
    definition = resource("Daemon", "processor")
    inject_credentials(pod, definition, "east")
    assert "env" not in pod["spec"]["containers"][0]
    inject_credentials(pod, definition, "west")
    inject_credentials(pod, definition, "west")
    assert pod["spec"]["containers"][0]["env"] == [
        {
            "name": "POLYAD_API_KEY",
            "valueFrom": {"secretKeyRef": {"name": "local-secret", "key": "token"}},
        }
    ]
    pod["spec"]["containers"][0]["env"] = [{"name": "POLYAD_API_KEY", "value": "conflict"}]
    with pytest.raises(ValueError, match="conflicts"):
        inject_credentials(pod, definition, "west")


def test_root_grants_follow_verified_cross_cluster_links_only(monkeypatch):
    """
    Remote streams include the assigned application's descendants without exposing unrelated trees.
    """
    monkeypatch.setenv("POLYAD_CLUSTER_NAME", "management")

    async def run():
        parent = resource("PolyGraph", "application", {"nodes": []})
        remote = resource("Graph", "remote-child", {"nodes": []})
        parent["metadata"]["annotations"] = {
            INVENTORY: json.dumps(
                [
                    {"cluster": "west", "namespace": "test", "kind": "Graph", "name": "remote-child", "node": "west"},
                ]
            )
        }
        remote["metadata"]["annotations"] = {
            PARENT: json.dumps(["management", "test", "PolyGraph", "application", "uid-application", "west"])
        }
        root_api, remote_api = FakeAPI(parent), FakeAPI(remote)
        grants = (GraphAccess("application", "test", kind="PolyGraph", cluster="management"),)
        ancestors = await observation_ancestry(remote_api, remote, cluster="west", resolve=lambda cluster: (root_api, "test"))
        identity = {"kind": "Graph", "cluster": "west", **remote["metadata"]}
        assert permitted_observation(identity, ancestors, grants)
        assert not permitted_observation(identity, [], grants)
        assert not permitted_observation(identity, ancestors, (GraphAccess("unrelated", "test", kind="PolyGraph"),))
        root_api.objects[("PolyGraph", "test", "application")]["metadata"]["uid"] = "replacement"
        with pytest.raises(ValueError, match="does not own"):
            await observation_ancestry(remote_api, remote, cluster="west", resolve=lambda cluster: (root_api, "test"))

    asyncio.run(run())


def test_demo_api_requires_neither_credentials_nor_lane_storage(monkeypatch):
    """
    Demonstration mode builds a public listener without touching credential or quota stores.
    """
    monkeypatch.setenv("POLYAD_AUTH_MODE", "Disabled")
    app = APIBuilder().with_handlers(Mock(), Mock()).build()
    response = app.test_client().get("/openapi.json")
    assert response.status_code == 200
    assert response.json["security"] == []


def test_auth_database_stores_verifiers_and_honors_revocation():
    """
    SQL parameters never contain the raw bearer credential; a disabled lane denies access.
    """
    store = object.__new__(CredentialStore)
    connection = Mock()
    connection.execute.return_value.fetchone.return_value = (False,)
    store.pool = Mock()
    store.pool.connection.return_value.__enter__ = Mock(return_value=connection)
    store.pool.connection.return_value.__exit__ = Mock(return_value=False)
    store.scope, store.lock, store.initialized = "test/control-plane", Lock(), False
    key = APIKey(name="client", direction="Inbound", existingSecret="key", endpoints=("events",))
    secret = "a-long-random-bearer-value"
    assert store.permitted("services", key, secret)
    insertion = connection.execute.call_args_list[-1]
    assert secret not in repr(insertion)
    assert hashlib.sha256(secret.encode()).hexdigest() in insertion.args[1]
    connection.execute.return_value.fetchone.return_value = (True,)
    assert not store.permitted("services", key, secret)
