"""
Verify work-graph controls from typed Helm values through effective runtime limits.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import jsonschema
import pytest
import yaml

from polyad.operator.adapters.kubernetes import API
from polyad.operator.coordination.queue import RefreshQueue
from polyad.operator.coordination.settings import WorkGraphSettings
from polyad.operator.coordination.validation import ValidationSettings
from polyad.operator.reconciliation.mutations import execute_mutations
from polyad_types.resources import Mutation, Scope
from tests.test_chart import CHART, render

ENVIRONMENT = {
    "plannerParallelism": "POLYAD_MUTATION_PLANNER_PARALLELISM",
    "maxInFlight": "POLYAD_WRITE_MAX_IN_FLIGHT",
    "maxPending": "POLYAD_WRITE_QUEUE_MAX_PENDING",
    "reconciliationWorkers": "POLYAD_RECONCILIATION_WORKERS",
    "reconciliationCooldownSeconds": "POLYAD_RECONCILIATION_COOLDOWN_SECONDS",
    "reconciliationBurst": "POLYAD_RECONCILIATION_BURST",
    "validationWorkers": "POLYAD_WRITE_VALIDATION_WORKERS",
    "validationIntervalSeconds": "POLYAD_WRITE_VALIDATION_INTERVAL_SECONDS",
    "validationWindowSeconds": "POLYAD_WRITE_VALIDATION_WINDOW_SECONDS",
    "validationBurst": "POLYAD_WRITE_VALIDATION_BURST",
}
CONFIGURATION = yaml.safe_load((Path(__file__).parent / "data" / "work-graph-values.yaml").read_text())["operator"]["writeQueue"]


@pytest.fixture(autouse=True)
def clear_configuration(monkeypatch):
    """
    Test the process defaults independently of the developer's environment.
    """
    for name in ENVIRONMENT.values():
        monkeypatch.delenv(name, raising=False)


def test_schema_defaults_match_runtime():
    """
    Require every administrator-facing work-graph setting to have a runtime default.
    """
    values = yaml.safe_load((CHART / "values.yaml").read_text())["operator"]["writeQueue"]
    schema = json.loads((CHART / "values.schema.json").read_text())["properties"]["operator"]["properties"]["writeQueue"]
    assert values == WorkGraphSettings.from_environment().document()
    assert set(values) == set(schema["properties"]) == set(schema["required"]) == set(ENVIRONMENT)
    jsonschema.validate(CONFIGURATION, schema)


@pytest.mark.parametrize("name", ENVIRONMENT)
def test_schema_and_runtime_reject_invalid_limits(monkeypatch, name):
    """
    Enforce each chart limit at startup even when Helm is bypassed.
    """
    properties = json.loads((CHART / "values.schema.json").read_text())["properties"]["operator"]["properties"]
    schema = properties["writeQueue"]["properties"][name]
    for value in (schema["minimum"] - 1, schema["maximum"] + 1, True, "invalid"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(value, schema)
        monkeypatch.setenv(ENVIRONMENT[name], str(value))
        with pytest.raises(ValueError):
            WorkGraphSettings.from_environment()


@pytest.mark.parametrize("field", ["max_in_flight", "max_pending", "planner_parallelism", "reconciliation_workers"])
def test_direct_worker_configuration_requires_integers(field):
    """
    Reject booleans and floats rather than interpreting them as worker counts.
    """
    for value in (True, 1.5, "2"):
        with pytest.raises(ValueError):
            WorkGraphSettings(**{field: value})


@pytest.mark.parametrize("field", ["interval", "window"])
def test_validation_configuration_requires_finite_numbers(field):
    """
    Reject values that cannot describe a receipt freshness interval.
    """
    for value in (True, "2", float("inf"), float("nan")):
        with pytest.raises(ValueError):
            ValidationSettings(**{field: value})


def test_invalid_planner_setting_fails_before_starting_workers(monkeypatch):
    """
    Stop an invalid release before connecting clients or launching background work.
    """
    from polyad.operator.lifecycle import handlers

    monkeypatch.setenv(ENVIRONMENT["plannerParallelism"], "0")
    monkeypatch.setattr(handlers, "initialized", False)
    monkeypatch.setattr(handlers, "http", None)
    constructor = Mock()
    monkeypatch.setattr(handlers, "API", constructor)
    with pytest.raises(ValueError, match="planner_parallelism"):
        asyncio.run(handlers.startup(Mock()))
    constructor.assert_not_called()


@pytest.mark.parametrize("ceiling,requested,expected", [(None, None, 1), (2, None, 2), (1, 8, 1), (3, 2, 2)])
def test_planner_uses_configured_ceiling_and_allows_lower_request(monkeypatch, ceiling, requested, expected):
    """
    Observe actual callback overlap while retaining dependency barriers and administrator limits.
    """
    if ceiling is not None:
        monkeypatch.setenv(ENVIRONMENT["plannerParallelism"], str(ceiling))

    async def scenario():
        active = peak = 0
        completed = set()

        async def apply(mutation):
            nonlocal active, peak
            if mutation.name == "dependent":
                assert completed == {"a", "b", "c"}
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0)
            active -= 1
            completed.add(mutation.name)

        mutations = tuple(Mutation(name, writes=(Scope((name,)),), effects_complete=True) for name in "abc")
        mutations += (Mutation("dependent", after=("a", "b", "c")),)
        plan = await execute_mutations(mutations, observe=AsyncMock(return_value={}), apply=apply, max_parallelism=requested)
        assert peak == expected
        assert max(map(len, plan.batches)) == expected
        assert plan.batches[-1] == ("dependent",)

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm")
@pytest.mark.parametrize(
    "settings,fixtures,pod_count",
    [
        ((), (), 1),
        (("ha=true",), (), 1),
        (("ha=true", "architecture.mode=Distributed", "api.enabled=true", "metrics.enabled=true"), (), 4),
        ((), ("root-values.yaml",), 1),
        (("architecture.mode=Distributed", "api.enabled=true"), ("root-values.yaml",), 4),
        ((), ("worker-values.yaml",), 1),
    ],
)
def test_helm_projects_effective_work_graph_limits(monkeypatch, settings, fixtures, pod_count):
    """
    Follow nondefault values through every execution template into real Python worker budgets.
    """
    objects = render(*settings, values_files=(*fixtures, "work-graph-values.yaml"))
    pods = [
        obj["spec"]["template"]["spec"]
        for obj in objects
        if obj["kind"] == "Daemon" or (obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    ]
    assert len(pods) == pod_count
    for pod in pods:
        container = next(item for item in pod["containers"] if item["name"] == "operator")
        env = {item["name"]: item.get("value") for item in container["env"]}
        for key, name in ENVIRONMENT.items():
            assert env[name] == str(CONFIGURATION[key])
            monkeypatch.setenv(name, env[name])
        api = API.__new__(API)
        assert api.work_graph.document() == CONFIGURATION
        assert api.write_lock.limit == CONFIGURATION["maxInFlight"]
        assert api.max_pending_writes == CONFIGURATION["maxPending"]
        assert api.validations.settings.workers == CONFIGURATION["validationWorkers"]
        assert api.validations.settings.interval == CONFIGURATION["validationIntervalSeconds"]
        assert api.validations.settings.window == CONFIGURATION["validationWindowSeconds"]
        assert api.validations.settings.burst == CONFIGURATION["validationBurst"]
        assert RefreshQueue(AsyncMock()).workers == CONFIGURATION["reconciliationWorkers"]


@pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm")
@pytest.mark.parametrize("value", ["0", "33", "true", "1.5", "invalid"])
def test_helm_rejects_invalid_planner_capacity(value):
    """
    Reject invalid planner limits before generating any workload manifest.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(f"operator.writeQueue.plannerParallelism={value}")


def test_validation_cadence_cannot_exceed_receipt_window(monkeypatch):
    """
    Retain the cross-field bound when loading process configuration directly.
    """
    monkeypatch.setenv(ENVIRONMENT["validationIntervalSeconds"], "3")
    monkeypatch.setenv(ENVIRONMENT["validationWindowSeconds"], "2")
    with pytest.raises(ValueError, match="interval <= window"):
        WorkGraphSettings.from_environment()


def test_worker_reports_work_graph_limits_to_root():
    """
    Preserve a downstream operator's own configured limits in root telemetry.
    """
    from polyad.operator.clusters.root import RootControlPlane

    async def scenario():
        plane = RootControlPlane.__new__(RootControlPlane)
        plane.coordinator = SimpleNamespace(namespace="control", identity="worker", planner=False, members=["worker"])
        client = AsyncMock()
        plane.shared = SimpleNamespace(client=client)
        snapshot = {"replica": "worker", "shards": [1], "pending": 0, "writes": {}, "workGraph": CONFIGURATION}
        report = {**snapshot, "root": False, "fresh": True}
        client.mget.return_value = [json.dumps(report)]
        reports = await plane.telemetry(snapshot)
        key, payload = client.set.call_args.args
        assert key == "polyad:control:worker-observation:worker"
        assert json.loads(payload) == report
        assert reports["worker"]["workGraph"] == CONFIGURATION

    asyncio.run(scenario())


def test_worker_heartbeat_reports_limits_without_serving_metrics(monkeypatch):
    """
    Include configuration in the executor's reporting path when its metrics server is disabled.
    """
    from polyad.operator.lifecycle import handlers

    async def scenario():
        settings = WorkGraphSettings(planner_parallelism=2, reconciliation_workers=4)
        root = SimpleNamespace(telemetry=AsyncMock())
        monkeypatch.setattr(handlers, "work_graph", settings)
        monkeypatch.setattr(handlers, "root_plane", root)
        monkeypatch.setattr(handlers, "queue", SimpleNamespace(queue=asyncio.Queue()))
        monkeypatch.setattr(handlers, "coordinator", SimpleNamespace(identity="worker", owned={1}))
        monkeypatch.setattr(handlers, "shared", Mock())
        monkeypatch.setattr(handlers, "serves", Mock(return_value=False))
        monkeypatch.setattr(handlers, "report", AsyncMock())
        monkeypatch.setattr(handlers, "write_backlog", Mock(return_value={}))
        monkeypatch.setattr(handlers.asyncio, "sleep", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await handlers.component_loop()
        assert root.telemetry.call_args.args[0]["workGraph"] == settings.document()

    asyncio.run(scenario())
