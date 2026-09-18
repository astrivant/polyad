"""
Exercise the SDK's application-facing deltas, replay and authorization boundaries.
"""

from __future__ import annotations

import copy
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from polyad_sdk import AdaptiveService, Client, Delta, Event, Settings, StreamInterrupted
from polyad_sdk.filters import event_type, field
from polyad_types import ServiceEndpoint, ThroughputSample
from polyad_types.network import NetworkPort

if TYPE_CHECKING:
    from polyad_sdk import Change

EXAMPLES = json.loads((Path(__file__).parent / "data/events.json").read_text())


def observation(index=0, cursor="1-0"):
    """
    Copy a public schema example with a distinct transport position.
    """
    value = copy.deepcopy(EXAMPLES[index])
    value["id"] = cursor
    return Event(**value)


def node(name, *, uid=None, replicas=1):
    """
    Describe a logical service and its observed workload incarnation.
    """
    return {
        "name": name,
        "kind": "Daemon",
        "ref": name,
        "desired": True,
        "requires": [],
        "executions": [
            {
                "kind": "Deployment",
                "name": name,
                "uid": uid or f"uid-{name}",
                "runtimeNode": name,
                "terminating": False,
                "replicas": replicas,
            }
        ],
    }


@pytest.fixture
def runtime():
    """
    Configure real SDK clients with deterministic topology reads and time.
    """
    clock = MagicMock(return_value=100.0)
    events = Client("https://events.example", "reader")
    topology = {
        "graph": {"kind": "Graph", "namespace": "test", "name": "pipeline", "uid": "uid-pipeline"},
        "cursor": "0-0",
        "revision": "revision-1",
        "observedAt": 100,
        "valid": True,
        "templateOnly": False,
        "terminating": False,
        "node": node("source"),
        "incoming": [],
        "dependencies": [],
        "dependents": [],
        "outgoing": [{"node": node("sink"), "ports": [{"port": 8080, "protocol": "TCP"}]}],
    }
    events.topology = MagicMock(side_effect=lambda **_: copy.deepcopy(topology))
    service = AdaptiveService(ServiceEndpoint("", "test", "Graph", "pipeline", "uid-pipeline", "source"), events, clock=clock)
    changes: list[Change] = []
    service.on_change(changes.append)
    return service, topology, clock, changes


def test_baseline_and_identity_keyed_connection_capacity_deltas(runtime):
    """
    New peers, replaced executions and replica counts describe distinct useful changes.
    """
    service, topology, _, changes = runtime
    service.refresh()
    assert changes[-1].baseline and changes[-1].deltas == ()
    assert len(service.view.candidates) == 1 and service.view.resources is None
    topology["outgoing"][0]["node"]["executions"][0]["replicas"] = 3
    topology["outgoing"].append({"node": node("second"), "ports": []})
    service.refresh()
    delta = changes[-1].matching("topology.outgoing.sink.node.executions.uid-sink.replicas")[0]
    assert (delta.before, delta.after, delta.difference) == (1, 3, 2)
    added = changes[-1].matching("topology.outgoing.second")[0]
    assert added.kind == "added" and added.difference is None
    topology["outgoing"][0]["node"]["executions"][0]["uid"] = "replacement"
    topology["outgoing"].pop()
    service.refresh()
    assert {delta.kind for delta in changes[-1].matching("topology.outgoing.sink.node.executions")} == {"added", "removed"}
    assert changes[-1].matching("topology.outgoing.second")[0].kind == "removed"
    topology["outgoing"][0]["node"]["executions"] = []
    service.refresh()
    assert changes[-1].matching("topology.outgoing.sink.node.executions.replacement")[0].kind == "removed"
    assert service.view.candidates == ()


def test_order_revisions_and_sample_heartbeats_do_not_create_changes(runtime):
    """
    Stable identities prevent list reordering or refreshed timestamps from invoking hooks.
    """
    service, topology, _, changes = runtime
    topology["outgoing"][0]["ports"].append({"port": 443, "protocol": "TCP"})
    topology["outgoing"].append({"node": node("second"), "ports": []})
    service.refresh()
    topology["outgoing"][0]["ports"].reverse()
    topology["outgoing"].reverse()
    topology.update(cursor="50-0", revision="new-heartbeat")
    service.refresh()
    assert len(changes) == 1 and service.cursor == "0-0"
    event = observation()
    event.data["status"]["throughput"] = {"phase": "Recommended", "observedAt": "first", "sample": {"observedAt": "first"}}
    service.dispatch(event)
    count = len(changes)
    event = observation(cursor="2-0")
    event.data.update(resourceVersion="999")
    event.data["status"]["throughput"] = {"phase": "Recommended", "observedAt": "second", "sample": {"observedAt": "second"}}
    service.dispatch(event)
    assert len(changes) == count and service.cursor == "2-0"


def test_metrics_decisions_and_traffic_have_numeric_deltas_with_context(runtime):
    """
    First measurements are unknown before arrival; later changes keep decision phase and units.
    """
    service, _, _, changes = runtime
    service.refresh()
    first = observation()
    first.data["resources"] = {"pods": 3}
    first.data["status"]["throughput"] = {
        "phase": "Recommended",
        "unit": "records",
        "sample": {"completedPerSecond": 100},
        "currentTraffic": [{"name": "pipeline", "destinations": [{"target": "sink", "weight": 80}]}],
    }
    service.dispatch(first)
    assert changes[-1].matching("resources")[0].kind == "added"
    assert changes[-1].matching("resources")[0].difference is None
    second = Event("2-0", "graph", copy.deepcopy(first.data))
    second.data["resources"]["pods"] = 4
    decision = second.data["status"]["throughput"]
    decision["phase"] = "Applied"
    decision["sample"]["completedPerSecond"] = 140
    decision["currentTraffic"][0]["destinations"][0]["weight"] = 60
    service.dispatch(second)
    assert changes[-1].matching("resources.pods")[0].difference == 1
    assert changes[-1].matching("decision.sample.completedPerSecond")[0].difference == 40
    assert changes[-1].matching("decision.currentTraffic.pipeline.destinations.sink.weight")[0].difference == -20
    assert changes[-1].matching("decision.phase")[0].after == "Applied"
    first.data["resources"]["pods"] = 999
    assert changes[-1].before.resources["pods"] == 3
    with pytest.raises(TypeError):
        service.view.decision["phase"] = "Approved"
    with pytest.raises(TypeError):
        service.view.topology["node"]["desired"] = False


@pytest.mark.parametrize("before,after", [(None, 5), (True, 1), (1, "2"), (10**500, 0), (float("inf"), 2)])
def test_numeric_differences_do_not_invent_measurements(before, after):
    """
    Missing, nonnumeric and nonfinite inputs never masquerade as measured changes.
    """
    assert Delta(("metric",), "changed", before, after).difference is None


def test_callback_failure_replays_only_unfinished_hooks_and_checkpoint(runtime):
    """
    A failed handler cannot advance the stream or duplicate earlier successful handlers.
    """
    service, _, _, changes = runtime
    completed = []
    attempts = []
    checkpoints = []

    def fail_once(change):
        attempts.append(change)
        if len(attempts) == 1:
            raise RuntimeError("business callback failed")

    service.on_change(lambda change: completed.append(change), match=event_type("graph"))
    service.on_change(fail_once, match=event_type("graph"))
    service._checkpoint = checkpoints.append
    service.refresh()
    event = observation()
    with pytest.raises(RuntimeError, match="business callback"):
        service.dispatch(event)
    assert service.cursor == "0-0" and checkpoints == ["0-0"]
    with pytest.raises(RuntimeError, match="pending event"):
        service.dispatch(observation(cursor="2-0"))
    service.dispatch(event)
    assert len(completed) == 1 and len(attempts) == 2 and len(changes) == 2
    assert checkpoints == ["0-0", "1-0"]
    service.dispatch(event)
    assert len(attempts) == 2


def test_checkpoint_failure_and_callback_payload_mutation_are_isolated(runtime):
    """
    Persisting a cursor can retry without replaying hooks or corrupting canonical event data.
    """
    service, _, _, changes = runtime
    service.on_change(lambda change: change.event.data.clear(), match=event_type("graph"))
    service.refresh()
    service._checkpoint = MagicMock(side_effect=[RuntimeError("storage unavailable"), None])
    event = observation()
    with pytest.raises(RuntimeError, match="storage unavailable"):
        service.dispatch(event)
    assert event.data["name"] == "pipeline"
    assert changes[-1].event.data["name"] == "pipeline"
    service.dispatch(event)
    assert len(changes) == 2 and service.cursor == "1-0"


def test_refresh_expiry_and_reset_preserve_unknown_state_and_cursor(runtime):
    """
    Refresh reads never skip replay; reset explicitly starts a new baseline with unknown metrics.
    """
    service, topology, clock, changes = runtime
    service.refresh()
    service.dispatch(observation())
    topology["cursor"] = "100-0"
    service.refresh()
    assert service.cursor == "1-0"
    clock.return_value = 131
    assert not service.view.available and service.view.candidates == ()
    assert service.view.resources is None
    with pytest.raises(StreamInterrupted):
        service.dispatch(Event("", "reset", {"reason": "expired"}))
    topology["observedAt"] = 131
    service.refresh(reset=True)
    assert service.cursor == "100-0" and service.view.available
    assert changes[-1].baseline and not changes[-1].deltas and service.view.resources is None
    service.events.topology.side_effect = OSError("endpoint unavailable")
    with pytest.raises(OSError):
        service.refresh()
    assert not service.view.available and service.view.topology["graph"]["uid"] == "uid-pipeline"


@pytest.mark.parametrize("field,value", [("uid", "new-uid"), ("namespace", "other"), ("kind", "PolyGraph"), ("name", "other")])
def test_observations_cannot_cross_identity_boundaries(runtime, field, value):
    """
    Even an authorized wider stream cannot overwrite a different node's context.
    """
    service, _, _, changes = runtime
    service.refresh()
    event = observation()
    event.data[field] = value
    service.dispatch(event)
    assert len(changes) == 1 and not service.view.observations


def test_cluster_fence_inventory_and_generation_regression(runtime):
    """
    Explicit cluster identity, snapshot bounds and newer resource generations remain authoritative.
    """
    service, topology, _, _ = runtime
    bounded = AdaptiveService(service.identity, service.events, settings=Settings(max_observations=1))
    with pytest.raises(ValueError, match="max_observations"):
        bounded.refresh()
    service.refresh()
    first = observation()
    service.dispatch(first)
    stale = observation(cursor="2-0")
    stale.data.update(generation=1, resources={"pods": 999})
    service.dispatch(stale)
    assert service.view.resources == {}
    identity = ServiceEndpoint("west", "test", "Graph", "pipeline", "uid-pipeline", "source")
    foreign = AdaptiveService(identity, service.events, clock=lambda: 100)
    topology["graph"]["cluster"] = "east"
    with pytest.raises(ValueError, match="identity"):
        foreign.refresh()
    topology["graph"]["cluster"] = "west"
    foreign.refresh()
    first.data["cluster"] = "east"
    foreign.dispatch(first)
    assert foreign.view.resources is None


def test_connection_hooks_consent_and_expiry_are_explicit(runtime):
    """
    Receipt deltas inform application policy; only an explicit call requests approval.
    """
    service, topology, clock, changes = runtime
    service.connections = MagicMock(spec=Client)
    service.refresh()
    event = observation(2)
    event.data["connection"]["expiresAt"] = datetime.fromtimestamp(120, UTC).isoformat()
    service.dispatch(event)
    assert changes[-1].matching("connections.uid-connection")[0].kind == "added"
    service.connections.respond_connection.assert_not_called()
    service.respond("uid-connection", "Reject")
    assert service.connections.respond_connection.call_args.args[:2] == ("test", "connection-1")
    target = ServiceEndpoint("", "test", "Graph", "pipeline", "uid-pipeline", "sink")
    service.connect(target, request_id="join", ttl_seconds=60, ports=(NetworkPort(8080),))
    assert service.connections.connect_services.call_args.args[0].source == service.identity
    clock.return_value = 121
    topology["observedAt"] = 121
    service.dispatch(Event("", "heartbeat", {}))
    assert changes[-1].matching("connections.uid-connection")[0].kind == "removed"
    with pytest.raises(ValueError, match="expired"):
        service.respond("uid-connection", "Approve")


def test_heartbeat_refresh_and_run_restart_retain_successful_cursor(runtime):
    """
    Idle streams refresh topology, and a resumed runtime re-establishes freshness.
    """
    service, topology, clock, changes = runtime

    def events(**kwargs):
        assert kwargs["heartbeats"] and kwargs["last_event_id"] in {"0-0", "1-0"}
        yield observation()
        clock.return_value = 115
        topology["observedAt"] = 115
        topology["outgoing"] = []
        yield Event("", "heartbeat", {})

    service.events.events = events
    service.run()
    assert changes[-1].matching("topology.outgoing.sink")[0].kind == "removed"
    assert service.cursor == "1-0" and not service.view.available
    service.run()
    assert any(change.after.available for change in changes)
    assert service.cursor == "1-0"


def test_sse_heartbeats_are_opt_in_and_do_not_carry_a_replay_cursor():
    """
    SSE comments drive refresh scheduling while ordinary Client consumers retain their framing.
    """
    client = Client("https://events.example", "token")
    client._open = MagicMock(side_effect=lambda *a, **kw: io.BytesIO(b": heartbeat\n\n"))
    assert list(client.events()) == []
    assert list(client.events(heartbeats=True)) == [Event("", "heartbeat", {})]


def test_environment_uses_rotating_application_credentials(monkeypatch, tmp_path):
    """
    Load projected identity without operator dependencies and reread mounted token files per request.
    """
    token = tmp_path / "token"
    token.write_text("first")
    values = {
        "GRAPH_KIND": "Graph",
        "GRAPH_NAMESPACE": "test",
        "GRAPH_NAME": "pipeline",
        "GRAPH_UID": "uid-pipeline",
        "NODE_NAME": "source",
        "EVENTS_URL": "https://events.example",
        "EVENTS_TOKEN_FILE": str(token),
    }
    for key, value in values.items():
        monkeypatch.setenv("POLYAD_" + key, value)
    for key in ("API_URL", "CONNECTIONS_URL", "EVENTS_TOKEN"):
        monkeypatch.delenv("POLYAD_" + key, raising=False)
    service = AdaptiveService.from_environment()
    assert service.events._authorization_headers()["Authorization"] == "Bearer first"
    token.write_text("rotated")
    assert service.events._authorization_headers()["Authorization"] == "Bearer rotated"
    assert service.api is None and service.connections is None
    monkeypatch.delenv("POLYAD_EVENTS_TOKEN_FILE")
    with pytest.raises(ValueError, match="application-owned"):
        AdaptiveService.from_environment()
    assert AdaptiveService.from_environment(allow_unauthenticated=True).events._authorization_headers() == {}


def test_reporting_and_filtered_hooks_use_existing_permissions(runtime):
    """
    Measurements target the configured boundary and hooks can combine path and event predicates.
    """
    service, _, _, _ = runtime
    filtered = []
    service.on_change(filtered.append, paths=("resources.pods",), match=event_type("graph") & field("name", regex="^pipeline$"))
    service.refresh()
    service.dispatch(observation())
    assert len(filtered) == 1
    service.api = MagicMock(spec=Client)
    sample = ThroughputSample("pipeline", "uid-pipeline", 2, "2030-01-01T00:00:00Z", "records", 120, 100)
    service.report_throughput(sample)
    service.api.report_throughput.assert_called_once_with(sample)
    foreign = ThroughputSample("other", "uid-other", 2, "2030-01-01T00:00:00Z", "records", 120, 100)
    with pytest.raises(ValueError, match="different graph"):
        service.report_throughput(foreign)


@pytest.mark.parametrize(
    "settings",
    [
        {"refresh_seconds": 0},
        {"refresh_seconds": 31},
        {"max_age_seconds": float("nan")},
        {"max_observations": True},
        {"max_connections": 0},
        {"transport": "http"},
        {"rebalance": 1},
    ],
)
def test_sdk_settings_reject_unbounded_or_mistyped_budgets(settings):
    """
    Observation budgets remain explicit, finite and internally consistent.
    """
    with pytest.raises(ValueError):
        Settings(**settings)


def test_atlas_receipts_match_the_whole_participant_identity(runtime):
    """
    Cross-boundary receipts must identify this graph-node participant before policy hooks see them.
    """
    service, _, _, changes = runtime
    service.refresh()
    event = observation(3, "1-0")
    event.data["graph"].pop("cluster")
    own = {"cluster": "", "namespace": "test", "kind": "Graph", "graph": "pipeline", "graphUid": "uid-pipeline", "node": "source"}
    event.data["connection"]["peers"] = {"target": {**own, "namespace": "other"}}
    service.dispatch(event)
    assert not service.view.connections and len(changes) == 1
    valid = Event("2-0", "connection", copy.deepcopy(event.data))
    valid.data["connection"]["peers"]["target"] = own
    service.dispatch(valid)
    assert "uid-connection" in service.view.connections
    revoked = Event("3-0", "connection", copy.deepcopy(valid.data))
    revoked.data["connection"]["revokeRequested"] = True
    service.dispatch(revoked)
    assert not service.view.connections
    assert changes[-1].matching("connections.uid-connection")[0].kind == "removed"


def test_delayed_hook_retains_delta_context_but_live_view_expires(runtime):
    """
    Retrying a historical observation never refreshes the live context's age or expired grants.
    """
    service, _, clock, _ = runtime
    attempts = []

    def action(change):
        attempts.append((change, service.view))
        if len(attempts) == 1:
            raise RuntimeError("try again")

    service.on_change(action, match=event_type("graph"))
    service.refresh()
    event = observation()
    with pytest.raises(RuntimeError, match="try again"):
        service.dispatch(event)
    clock.return_value = 140
    service.dispatch(event)
    assert attempts[0][0] == attempts[1][0]
    assert attempts[0][1].available and not attempts[1][1].available
    assert attempts[1][1].candidates == ()
