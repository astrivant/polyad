"""
Verify local study guard reactions, planner isolation and complete process ownership.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from demo import nature
from polyad_benchmarks.studies import artifacts
from polyad_benchmarks.studies.nature.selection import select
from polyad_benchmarks.studies.runner import validate
from polyad_benchmarks.studies.soul.runtime.measurements import ResourceLoop, service_level
from polyad_benchmarks.studies.soul.runtime.policy import Policy

ROOT = Path(__file__).resolve().parents[2]


def recipe(study):
    """
    Load the same versioned recipe used in the refresh matrix.
    """
    return json.loads((ROOT / "studies" / study / "fixtures/scenario.json").read_text())


@pytest.mark.parametrize("key,value", [("workSeconds", float("nan")), ("timeoutSeconds", True), ("workSeconds", 100)])
def test_recipe_rejects_invalid_budgets(key, value):
    """
    Refuse invalid resource budgets before starting processes.
    """
    config = recipe("soul")
    config[key] = value
    with pytest.raises(ValueError):
        validate(config)


def test_planner_restores_demo_catalog_and_selects_replacements(monkeypatch):
    """
    Preserve planner globals while selecting compatible births, replacements and survivors.
    """
    original = nature.CATALOG
    limits = nature.Settings(service_limit=6, overlap_limit=10, candidate_limit=10000)
    first = select("squared", {}, limits)
    changed = select("enriched", first.placements, limits)
    recovered = select("squared", changed.placements, limits)
    assert nature.CATALOG is original
    assert len(first.placements) == len(changed.placements) == 6
    assert all(route[-1].capability.output == "enriched" for route in changed.routes)
    assert any(len(route) == 2 for route in changed.routes)
    assert any(len(route) == 1 for route in changed.routes)
    assert first.placements == recovered.placements
    assert changed.placements["B"] == first.placements["B"]
    assert changed.placements["A"] != first.placements["A"]

    def fail(*args):
        raise RuntimeError("search interrupted")

    monkeypatch.setattr(nature, "natural_selection", fail)
    with pytest.raises(RuntimeError, match="interrupted"):
        select("enriched", first.placements, limits)
    assert nature.CATALOG is original


def test_observation_loss_and_constraints_pause_then_recover():
    """
    Exercise each guard against unavailable inputs and restore normal admission.
    """
    data = {
        "pids": [123],
        "profile": "interactive",
        "backlog": 0,
        "projectedWorkers": 1,
        "memoryReserved": 0,
        "healthy": True,
        "stale": False,
        "decision": "Applied",
        "permission": "Active",
    }
    policy = Policy("A", lambda: data)
    policy.update()
    assert policy.permits(*policy.guards)
    for key, value, guard in (
        ("stale", True, "fresh"),
        ("healthy", False, "peer"),
        ("decision", "Stabilizing", "decision"),
        ("permission", "Pending", "permission"),
        ("permission", "Expired", "permission"),
        ("projectedWorkers", 5, "workers"),
        ("memoryReserved", 4096, "memory"),
        ("backlog", 24, "envelope"),
    ):
        before = data[key]
        data[key] = value
        policy.update()
        assert not policy.permits(guard)
        data[key] = before
        policy.update()
        assert policy.permits(guard)
    assert policy.receipt_generation == 2
    assert policy.route_names == ("worker-123",)
    data["backlog"] = 12
    policy.update()
    assert policy.proposal == "batch"
    data["memoryReserved"] = 3072
    policy.update()
    assert policy.proposal == "compact"


def test_service_level_and_resource_loop_preserve_measurement_boundaries():
    """
    Keep SLA classification and modeled VPA actions deterministic and explicit.
    """
    policy = recipe("soul")["serviceLevel"]
    compliant = service_level(
        serving=True,
        eligible=100,
        successful=100,
        latencies=[0.1, 0.2],
        completed_per_second=20,
        adapting_seconds=None,
        policy=policy,
    )
    unavailable = service_level(
        serving=False,
        eligible=100,
        successful=98,
        latencies=[0.6],
        completed_per_second=0,
        adapting_seconds=2,
        policy=policy,
    )
    assert compliant["state"] == "Compliant"
    assert unavailable["state"] == "Unavailable"
    assert set(unavailable["violations"]) == {
        "serving",
        "availability",
        "latencyP99Seconds",
        "adaptation.maximumDurationSeconds",
    }
    loop = ResourceLoop.from_config(recipe("soul")["resourceLoop"])
    initial = loop.assigned
    assigned, used, changed = loop.observe(1, workers=4, backlog=24)
    assert changed and assigned > initial and used > initial
    assigned, _, changed = loop.observe(1.01, workers=1, backlog=0)
    assert not changed and assigned > initial


def test_artifacts_reject_tampering_and_incomplete_accounting(tmp_path):
    """
    Refuse altered figures and evidence of incomplete process cleanup.
    """
    config = {"phases": [{"seconds": 1, "rate": 1}]}
    for name in artifacts.ARTIFACTS:
        (tmp_path / name).write_bytes(b"measured evidence")
    record = {"allJoined": True, "offered": 1, "completed": 1, "rejected": 0, "jobs": {"1": {"finished": 1}}, "samples": [1], "frames": [1]}
    result = {
        "recipe": config,
        "figures": list(artifacts.FIGURES),
        "artifacts": artifacts.fingerprint(tmp_path),
        "records": [{**record, "mode": mode} for mode in ("fixed", "adaptive")],
    }
    artifacts.verify(result, config, tmp_path)
    incomplete = copy.deepcopy(result)
    incomplete["records"][0]["allJoined"] = False
    with pytest.raises(ValueError, match="incomplete"):
        artifacts.verify(incomplete, config, tmp_path)
    result["figures"][0] = "../outside.png"
    with pytest.raises(ValueError, match="inventory"):
        artifacts.verify(result, config, tmp_path)
    result["figures"] = list(artifacts.FIGURES)
    (tmp_path / "topology.png").write_bytes(b"changed")
    with pytest.raises(ValueError, match="artifacts"):
        artifacts.verify(result, config, tmp_path)


@pytest.mark.parametrize("study", ["soul", "nature"])
def test_real_process_population_accounts_for_jobs_and_joins_every_child(study):
    """
    Verify actual jobs, composition changes, resource ceilings and complete shutdown.
    """
    from polyad_benchmarks.studies.nature.monitor import Monitor as NatureMonitor
    from polyad_benchmarks.studies.soul.monitor import Monitor as SoulMonitor

    config = recipe(study)
    config["workSeconds"] = 0.01
    for phase in config["phases"]:
        phase.update(seconds=0.2, rate=20)
    monitor = (SoulMonitor if study == "soul" else NatureMonitor)(config, True)
    try:
        result = monitor.run()
    finally:
        monitor.close()
    assert result["offered"] == result["completed"] == 16
    assert result["rejected"] == 0
    assert result["allJoined"]
    assert all(member.process.exitcode == 0 and not member.outstanding for member in monitor.owned)
    assert all(producer.exitcode == 0 for producer in monitor.producers)
    assert all(sample["workers"] <= 4 and sample["backlog"] <= 24 for sample in result["samples"])
    assert all(
        sample["serviceLevel"]["state"] in {"Compliant", "Degraded", "Unavailable"}
        and sample["resourceAssignedBytes"] > 0
        and set(sample["cgroup"])
        == {
            "cpuUsageUsec",
            "cpuLimitMillicores",
            "memoryUsageBytes",
            "memoryLimitBytes",
            "memoryAvailableBytes",
        }
        for sample in result["samples"]
    )
    assert {event["event"] for event in result["events"]} >= {"resource_allocation_changed"}
    if study == "nature":
        events = {event["event"] for event in result["events"]}
        assert {"service_started", "service_replaced", "service_survived", "service_joined"} <= events
