"""
Keep SDK defaults aligned with compiler-projected identity and container resource contracts.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError, fields, replace

import pytest

from polyad.compiler.passes.identity import POD_FIELDS, RESOURCE_FIELDS, workload_identity
from polyad_sdk import ContainerBudgetStrategy, ContainerResources, ObserveStrategy, PodContext, WorkloadContext, env, refresh_environment
from polyad_types.graphs.topology import Node
from tests.test_adaptation_strategies import measured
from tests.test_sdk import RecordingService


def projected():
    """
    Generate the actual operator contract and resolve representative kubelet selectors.
    """
    root = {"kind": "PolyGraph", "metadata": {"name": "atlas", "namespace": "test", "uid": "root-uid"}}
    graph = {"kind": "Graph", "metadata": {"name": "pipeline", "namespace": "test", "uid": "graph-uid"}}
    definition = {
        "kind": "Daemon",
        "metadata": {"name": "consumer", "namespace": "test", "uid": "definition-uid", "generation": 3},
        "spec": {"controller": "StatefulSet"},
    }
    values = workload_identity(
        [root, graph],
        Node("sink", "Daemon", "consumer"),
        definition,
        "pipeline-sink",
        {name: f"https://{name.lower()}.example" for name in ("API", "EVENTS", "METRICS", "CONNECTIONS")},
    )
    values.update({name: name.removeprefix("POLYAD_").lower() for name in POD_FIELDS})
    values.update(
        POLYAD_POD_NAMESPACE="test",
        POLYAD_POD_IP="10.0.0.2",
        POLYAD_POD_IPS="10.0.0.2,fd00::2",
        POLYAD_CPU_REQUEST_MILLICORES="500",
        POLYAD_CPU_LIMIT_MILLICORES="1000",
        POLYAD_MEMORY_REQUEST_BYTES="536870912",
        POLYAD_MEMORY_LIMIT_BYTES="1073741824",
        POLYAD_ACTIVATION_ID="run-42",
        POLYAD_ACTIVATION_UID="activation-uid",
        POLYAD_REQUEST_ID="request-42",
        POLYAD_COMPOSITION_UID="composition-uid",
        POLYAD_RUNTIME_NODE_NAME="sink-run-42",
        POLYAD_POD_CLUSTER="west",
    )
    return values


def test_sdk_reads_all_compiler_projected_identity_and_placement_fields():
    """
    Exercise the compiler's own projection so SDK defaults cannot silently drift from it.
    """
    values = projected()
    context = WorkloadContext.from_environment(values)
    assert context.identity.graph == "pipeline" and context.identity.node == "sink"
    assert context.root.uid == "root-uid"
    assert [item.uid for item in context.ancestry] == ["root-uid", "graph-uid"]
    assert context.definition.uid == "definition-uid" and context.definition_generation == 3
    for item in fields(WorkloadContext):
        if "env" in item.metadata:
            assert getattr(context, item.name) == values[item.metadata["env"]]
    for item in fields(PodContext):
        assert getattr(context.pod, item.name) == values[item.metadata["env"]]
    for name in RESOURCE_FIELDS:
        assert getattr(context.resources, name.removeprefix("POLYAD_").lower()) == int(values[name])

    # Placement never silently selects a remote event authority.
    assert context.pod.cluster == "west" and context.identity.cluster == ""
    assert WorkloadContext.from_environment(values, cluster="west").identity.cluster == "west"
    assert context.resources.budget("cpu") == 500 and context.resources.budget("memory") == 536870912
    with pytest.raises(FrozenInstanceError):
        context.node_id = "different"
    values["POLYAD_NODE_ID"] = "changed-after-startup"
    assert context.node_id == "sink"


def test_environment_factory_retains_context_without_retaining_credentials(monkeypatch):
    """
    Endpoint and identity defaults use the same snapshot while token handling stays separate.
    """
    for name, value in projected().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("POLYAD_EVENTS_TOKEN", "secret-not-context")
    monkeypatch.setenv("POLYAD_CACHE_URL", "private-operator-setting")
    service = RecordingService.from_environment(cluster="west", environ=os.environ, strategies=[ObserveStrategy()])
    assert service.context.identity == service.identity
    assert service.events.url == service.context.events_url
    assert service.context.activation_uid == "activation-uid"
    assert "secret-not-context" not in repr(service.context)
    assert "private-operator-setting" not in repr(service.context)
    assert service.events._authorization_headers()["Authorization"] == "Bearer secret-not-context"
    different = replace(service.context, identity=WorkloadContext.from_environment(projected()).identity)
    with pytest.raises(ValueError, match="must match"):
        RecordingService(service.identity, service.events, context=different, strategies=[ObserveStrategy()])


def test_missing_optional_context_and_explicit_defaults():
    """
    Minimum graph identity remains sufficient, with no invented resource budget or ancestry.
    """
    minimum = {
        name: value
        for name, value in projected().items()
        if name in {"POLYAD_GRAPH_KIND", "POLYAD_GRAPH_NAMESPACE", "POLYAD_GRAPH_UID", "POLYAD_GRAPH_NAME", "POLYAD_NODE_NAME"}
    }
    context = WorkloadContext.from_environment(minimum)
    assert context.root is None and context.ancestry == ()
    assert context.node_id == context.runtime_node_name == "sink"
    assert context.resources.budget("memory") is None
    assert context.api_url == "" and context.pod.ip == ""


def test_environment_factory_allocates_independent_runtime_instances():
    """
    Repeated factory calls share workload identity, not runtime actor identity or delivery state.
    """
    values = {**projected(), "POLYAD_EVENTS_TOKEN": "reader"}
    first = RecordingService.from_environment(environ=values, strategies=[ObserveStrategy()])
    second = RecordingService.from_environment(environ=values, strategies=[ObserveStrategy()])
    assert first.identity == second.identity and first.context == second.context
    assert first.instance_id != second.instance_id
    assert first.delivery_id is second.delivery_id is None
    assert first.events is not second.events


@pytest.mark.parametrize("raw", ["oops", "{}", "[]", '[{"kind": "Graph"}]', "[null]", '"' + "x" * 65537 + '"'])
def test_invalid_ancestry_fails_without_echoing_contents(raw):
    """
    Invalid ancestry cannot produce a silently incorrect service context.
    """
    values = projected()
    values["POLYAD_GRAPH_ANCESTRY"] = raw
    with pytest.raises(ValueError, match="ancestry"):
        WorkloadContext.from_environment(values)


def test_replaced_parent_and_wrong_containing_graph_fail_identity_checks():
    """
    Incarnation and namespace checks survive parsing the startup snapshot.
    """
    for field, value in (("uid", "other-uid"), ("namespace", "other-namespace")):
        values = projected()
        entries = json.loads(values["POLYAD_GRAPH_ANCESTRY"])
        entries[-1][field] = value
        values["POLYAD_GRAPH_ANCESTRY"] = json.dumps(entries)
        with pytest.raises(ValueError, match="ancestry"):
            WorkloadContext.from_environment(values)
    values = projected()
    values.pop("POLYAD_ROOT_GRAPH_UID")
    with pytest.raises(ValueError, match="ROOT_GRAPH"):
        WorkloadContext.from_environment(values)


@pytest.mark.parametrize("value", ["-1", "1Gi", "1.5", "NaN", "inf", "True", "１２", "1" * 65])
def test_invalid_resource_selectors_are_not_inferred_as_capacity(value):
    """
    Use Downward API integer units explicitly and reject malformed projections.
    """
    with pytest.raises(ValueError, match="MEMORY_REQUEST_BYTES"):
        ContainerResources.from_environment({"POLYAD_MEMORY_REQUEST_BYTES": value})


@pytest.mark.parametrize("value", [True, -1, 1.5, "1024"])
def test_explicit_resource_context_keeps_integer_units(value):
    """
    Explicit resource overrides retain the same capacity contract as projected defaults.
    """
    with pytest.raises(ValueError, match="nonnegative integer"):
        ContainerResources(memory_request_bytes=value)


def test_container_guard_uses_projected_request_and_application_usage(monkeypatch):
    """
    A rollout's overlap must fit per-container capacity, independently of graph-wide metrics.
    """
    monkeypatch.setitem(env, "POLYAD_MEMORY_REQUEST_BYTES", "1024")
    monkeypatch.setitem(env, "POLYAD_MEMORY_LIMIT_BYTES", "2048")
    usage = [800]
    guard = ContainerBudgetStrategy("workers", lambda _: None, resource="memory", used=lambda: usage[0], reserve=300)
    _, current = measured({"pods": 0})
    assert guard.evaluate(current).state == "blocked"
    usage[0] = 724
    assert guard.evaluate(current).satisfied
    usage[0] = float("nan")
    assert guard.evaluate(current).state == "unknown"


def test_explicit_container_budget_overrides_environment_and_missing_request_holds(monkeypatch):
    """
    Node allocatable fallback alone is not a default entitlement; explicit settings take precedence.
    """
    resources = ContainerResources(cpu_limit_millicores=64000)
    _, current = measured({})
    guard = ContainerBudgetStrategy("workers", lambda _: None, resource="cpu", used=lambda: 100, resources=resources)
    assert guard.evaluate(current).state == "unknown"
    monkeypatch.setitem(env, "POLYAD_CPU_REQUEST_MILLICORES", "invalid")
    explicit = ContainerBudgetStrategy("workers", lambda _: None, resource="cpu", used=lambda: 100, maximum=500, reserve=400)
    assert explicit.evaluate(current).satisfied
    assert ContainerResources(cpu_request_millicores=1000, cpu_limit_millicores=500).budget("cpu") == 500
    assert ContainerResources(cpu_request_millicores=0, cpu_limit_millicores=500).budget("cpu") is None


def test_full_environment_snapshot_is_captured_at_sdk_import():
    """
    Capture unrelated application variables too, with explicit reload and stable imported references.
    """
    script = """
import os
from polyad_sdk import env, refresh_environment
from polyad_sdk.runtime.environment import env as shared
assert env is shared and isinstance(env, dict)
assert env["APPLICATION_TEST_INPUT"] == "at-import"
os.environ["APPLICATION_TEST_INPUT"] = "after-import"
assert env["APPLICATION_TEST_INPUT"] == "at-import"
assert refresh_environment() is env
assert shared["APPLICATION_TEST_INPUT"] == "after-import"
refresh_environment(env)
assert shared["APPLICATION_TEST_INPUT"] == "after-import"
"""
    subprocess.run([sys.executable, "-c", script], env={**os.environ, "APPLICATION_TEST_INPUT": "at-import"}, check=True, timeout=15)


def test_shared_sdk_snapshot_supplies_defaults_and_explicit_mapping_wins():
    """
    All environment consumers share one snapshot while explicit configuration remains local.
    """
    previous = dict(env)
    try:
        refresh_environment({**projected(), "POLYAD_EVENTS_TOKEN": "sdk-token"})
        service = RecordingService.from_environment(strategies=[ObserveStrategy()])
        assert service.context.resources.memory_request_bytes == 536870912
        override = {**env, "POLYAD_NODE_NAME": "override-node", "POLYAD_EVENTS_TOKEN": "override-token"}
        other = RecordingService.from_environment(environ=override, strategies=[ObserveStrategy()])
        assert other.identity.node == "override-node" and service.identity.node == "sink"
        assert env["POLYAD_NODE_NAME"] == "sink"
        assert other.events._authorization_headers()["Authorization"] == "Bearer override-token"
        env["POLYAD_MEMORY_REQUEST_BYTES"] = "1"
        assert WorkloadContext.from_environment().resources.memory_request_bytes == 1
        assert service.context.resources.memory_request_bytes == 536870912
    finally:
        refresh_environment(previous)
