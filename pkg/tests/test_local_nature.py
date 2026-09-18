"""
Verify typed composition selection, real capability mutation and complete process retirement.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "nature.py"


@pytest.fixture
def nature(monkeypatch):
    """
    Import the local planner and its sibling Soul implementation without starting children.
    """
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("local_nature_example", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name)


def records(output):
    """
    Parse atomic lifecycle evidence shared by the parent and its child trees.
    """
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def gone(events):
    """
    Confirm every reported producer, service and worker PID was reaped.
    """
    pids = {event["worker"] for event in events if event["event"] == "worker_spawned"}
    pids.update(event["service_pid"] for event in events if event["event"] in {"born", "mutated"})
    pids.update(event["producer"] for event in events if event["event"] == "load_started")
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_selection_derives_a_mutation_and_a_multistage_route(nature):
    """
    Compatible typed outcomes and cost choose survival independently of service name order.
    """
    settings = nature.Settings()
    original = nature.natural_selection(nature.ENVIRONMENTS[0], {}, settings)
    assert set(original.placements) == {"A", "B", "C"}
    assert original.cost == 4 and original.expansion == 1.5
    selected = nature.natural_selection(nature.ENVIRONMENTS[1], original.placements, settings)
    assert selected.describe() == [["A:square-plus-one"], ["B:square", "D:increment"]]
    assert selected.cost == 3 and selected.expansion == 1
    assert nature.natural_selection(nature.ENVIRONMENTS[2], selected.placements, settings) == selected
    # Increasing B's cost causes C to survive instead: selection is not a scripted deletion.
    nature.CATALOG = tuple(replace(place, cost=3) if place.name == "B" else place for place in nature.CATALOG)
    alternative = nature.natural_selection(nature.Requirement("enriched", 2, 4), original.placements, settings)
    assert alternative.describe() == [["A:square-plus-one"], ["C:square", "D:increment"]]


@pytest.mark.parametrize("changes", [{"overlap_limit": 4}, {"candidate_limit": 1}, {"hard_minimum": 1.1}, {"service_limit": 2}])
def test_selection_rejects_infeasible_or_incomplete_search(nature, changes):
    """
    A plan cannot bypass rolling capacity, exact bounds or an exhausted search budget.
    """
    current = nature.natural_selection(nature.ENVIRONMENTS[0], {}, nature.Settings()).placements
    with pytest.raises(RuntimeError):
        nature.natural_selection(nature.ENVIRONMENTS[1], current, replace(nature.Settings(), **changes))


def test_missing_capabilities_and_insufficient_cost_are_unsatisfied(nature):
    """
    The planner needs a real path to the requested output within its declared budget.
    """
    for requirement in (nature.Requirement("unknown", 1, 10), nature.Requirement("enriched", 2, 2)):
        with pytest.raises(RuntimeError):
            nature.natural_selection(requirement, {}, nature.Settings())


def test_service_fences_old_revisions_and_previously_completed_jobs(nature):
    """
    Delayed or duplicate admissions cannot enter the worker subtree after a plan change.
    """
    parent, child = mp.get_context("spawn").Pipe()
    service = nature.Service(nature.CATALOG[0], 2, child, nature.Settings())
    try:
        parent.send(("job", 1, (0, 1)))
        with pytest.raises(RuntimeError, match="stale"):
            service.command()
        parent.send(("job", 2, (0, 1)))
        service.command()
        parent.send(("adopt", 3, None))
        with pytest.raises(RuntimeError, match="accepted work"):
            service.command()
        service.inputs.clear()
        parent.send(("job", 2, (0, 1)))
        with pytest.raises(RuntimeError, match="duplicate"):
            service.command()
    finally:
        parent.close()
        child.close()


def test_real_population_mutates_survives_retires_and_preserves_soul_behavior():
    """
    Changing the outcome changes real routes and PIDs while useful services retain identity.
    """
    run = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=60, cwd=SCRIPT.parent)
    assert run.returncode == 0, run.stdout + run.stderr
    events = records(run.stdout)
    assert events[-1]["event"] == "nature_success" and events[-1]["completed"] == 672
    assert events[-1]["all_joined"] and events[-1]["population"] == 0
    rounds = [event for event in events if event["event"] == "round_verified"]
    assert [event["completed"] for event in rounds] == [288, 192, 192]
    original, changed, stable = [event["services"] for event in rounds]
    assert set(original) == {"A", "B", "C"} and set(changed) == {"A", "B", "D"}
    assert original["A"] != changed["A"] and original["B"] == changed["B"]
    assert changed == stable
    plans = [event for event in events if event["event"] == "plan_committed"]
    assert [event["cheeger"] for event in plans] == [1.5, 1, 1]
    assert ["B", "D"] in plans[1]["added_edges"] and ["B", "output"] in plans[1]["removed_edges"]
    assert not plans[2]["added_edges"] and not plans[2]["removed_edges"]
    spawned = [event for event in events if event["event"] in {"born", "mutated"}]
    assert max(event["live"] for event in spawned) == 5
    for name, service_pid in {**original, **changed}.items():
        local = [event for event in events if event["pid"] == service_pid and event["event"] == "committed"]
        assert local[0]["role"] == "interactive" and local[-1]["role"] == "interactive"
        assert any(event["role"] == "batch" and event["cheeger"] == 1.5 for event in local), name
    for revision in (1, 2, 3):
        for service in rounds[revision - 1]["services"]:
            local = [event for event in events if event.get("revision") == revision and event.get("service") == service]
            assert any(event["event"] == "committed" and event["role"] == "batch" for event in local)
            assert any(event["event"] == "observed" and event["delta_backlog"] > 0 for event in local)
            assert any(event["event"] == "observed" and event["delta_backlog"] < 0 for event in local)
    workers = [event for event in events if event["event"] == "worker_spawned"]
    assert max(event["live"] for event in workers) <= 4
    assert {event["worker"] for event in workers} == {event["worker"] for event in events if event["event"] == "worker_joined"}
    excluded = next(event for event in events if event["event"] == "retired" and event["service"] == "C")
    assert excluded["revision"] == 2 and excluded["exitcode"] == 0
    assert events.index(excluded) > events.index(plans[1])
    for event in spawned:
        readiness = next(item for item in events if item["pid"] == event["service_pid"] and item["event"] == "worker_ready")
        assert events.index(readiness) < events.index(plans[event["revision"] - 1])
    gone(events)


def test_insufficient_rolling_capacity_preserves_limits_and_cleans_up():
    """
    Replacement needs temporary capacity beyond the surviving population's steady size.
    """
    run = subprocess.run([sys.executable, str(SCRIPT), "--overlap-limit", "4"], capture_output=True, text=True, timeout=45)
    events = records(run.stdout)
    assert run.returncode != 0, run.stdout
    assert any(event["event"] == "nature_blocked" for event in events)
    assert not any(event["event"] == "nature_success" for event in events)
    assert all(event["live"] <= 4 for event in events if event["event"] in {"born", "mutated"})
    gone(events)


def test_interrupted_parent_reaps_its_soul_subtrees(tmp_path):
    """
    Cancellation during accepted load propagates through the entire ownership tree.
    """
    with (tmp_path / "stderr.txt").open("w+") as errors:
        process = subprocess.Popen([sys.executable, str(SCRIPT)], stdout=subprocess.PIPE, stderr=errors, text=True)
        prefix = []
        try:
            while line := process.stdout.readline():
                prefix.append(line)
                if '"event": "load_started"' in line:
                    process.send_signal(signal.SIGINT)
                    break
            output, _ = process.communicate(timeout=30)
            events = records("".join(prefix) + output)
            assert process.returncode != 0
            assert not any(event["event"] == "nature_success" for event in events)
            gone(events)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=20)


@pytest.mark.parametrize("arguments", [["--jobs", "0"], ["--tick", "nan"], ["--window", "100000"], ["--hard-minimum", "2"]])
def test_invalid_configuration_cannot_start_children(arguments):
    """
    Reject unbounded or infeasible initial runtime inputs before admission.
    """
    run = subprocess.run([sys.executable, str(SCRIPT), *arguments], capture_output=True, text=True, timeout=5)
    assert run.returncode == 2 and not records(run.stdout)
