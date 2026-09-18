"""
Verify the standalone Soul searching example against real spawned process trees.
"""

from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "soul.py"
FAST = ["--work-seconds", "0.02", "--tick", "0.02", "--cooldown", "0.1", "--idle-seconds", "0.15"]


@pytest.fixture
def soul():
    """
    Load the pure planner without executing the example's process entry point.
    """
    spec = importlib.util.spec_from_file_location("local_soul_example", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name)


def records(output):
    """
    Decode atomic JSON lifecycle records from the combined process output.
    """
    return [json.loads(line) for line in output.splitlines() if line.startswith("{")]


def gone(events):
    """
    Check that every reported child PID has been reaped before returning to the caller.
    """
    pids = {event["worker"] for event in events if event["event"] == "worker_spawned"}
    for event in events:
        if event["event"] == "load_started":
            pids.update(event["services"])
            pids.add(event["producer"])
    for pid in pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_exact_cut_values(soul):
    """
    Preserve known expansion values at the worker-routing and peer-network boundaries.
    """
    assert [soul.cheeger(count) for count in (1, 2, 3, 4)] == [1, 1, 1.5, 4 / 3]
    assert soul.cheeger(links=[(0, 1), (1, 2)]) == 1
    assert soul.cheeger(links=[(0, 1), (1, 2), (0, 2)]) == 2
    for count in (0, 7):
        with pytest.raises(ValueError):
            soul.cheeger(count)


def test_search_requires_sustained_pressure_cooldown_and_quiet_recovery(soul):
    """
    A spike cannot trigger activation, and an active service cannot downshift with pending work.
    """
    settings = soul.Settings()
    search, base, batch = soul.search_soul, soul.INTERACTIVE, soul.BATCH
    assert search(base, 20, 1, 0, 2, settings) == base
    assert search(base, 20, 3, 0, 0, settings) == base
    assert search(base, 0, 3, 0, 2, settings) == base
    assert search(base, 20, 3, 0, 2, settings) == batch
    assert search(batch, 1, 0, 10, 2, settings) == batch
    assert search(batch, 0, 0, 0.1, 2, settings) == batch
    assert search(batch, 0, 0, 1, 2, settings) == base


@pytest.mark.parametrize("jobs", [96, 128])
def test_real_services_spawn_roll_restore_and_join_every_process(jobs):
    """
    A producer loads three services; TCP topology and execution roles change with verified results.
    """
    run = subprocess.run([sys.executable, "-I", str(SCRIPT), *FAST, "--jobs", str(jobs)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    events = records(run.stdout)
    assert events[-1]["event"] == "success" and events[-1]["completed"] == jobs * 3 and events[-1]["all_joined"]
    load = next(event for event in events if event["event"] == "load_started")
    assert len(set([load["producer"], *load["services"], load["pid"]])) == 5
    for name in ("service-0", "service-1", "service-2"):
        local = [event for event in events if event.get("service") == name]
        commits = [event for event in local if event["event"] == "committed"]
        assert [event["role"] for event in commits] == ["interactive", "batch", "interactive"]
        assert [event["workers"] for event in commits] == [1, 3, 1]
        assert [event["cheeger"] for event in commits] == [1, 1.5, 1]
        assert [event["generation"] for event in commits] == [1, 2, 3]
        spawned = [event for event in local if event["event"] == "worker_spawned"]
        assert len(spawned) == 5 and max(event["live"] for event in spawned) <= 4
        assert {event["worker"] for event in spawned} == {event["worker"] for event in local if event["event"] == "worker_joined"}
        ready = set()
        for event in local:
            if event["event"] == "worker_ready":
                ready.add(event["worker"])
            if event["event"] == "committed":
                replacements = [item["worker"] for item in spawned if item["role"] == event["role"] and item["time"] <= event["time"]]
                assert all(pid in ready for pid in replacements)
        restored = next(event for event in local if event["event"] == "baseline_restored")
        assert restored["completed"] == jobs and restored["workers"] == 1
        observations = [event for event in local if event["event"] == "observed"]
        assert any(event["delta_backlog"] > 0 for event in observations)
        assert any(event["delta_backlog"] < 0 for event in observations)
    topology = [event for event in events if event["event"] == "topology_committed"]
    assert [event["topology"] for event in topology] == ["chain", "triangle", "chain"]
    admitted = [event for event in events if event["event"] == "topology_admitted"]
    assert [event["cheeger"] for event in admitted] == [1, 2, 1]
    opened = [event for event in events if event["event"] == "edge_opened"]
    assert {(event["service"], event["target"]) for event in opened} == {
        ("service-0", "service-1"),
        ("service-1", "service-2"),
        ("service-0", "service-2"),
    }
    served = [event for event in events if event["event"] == "peer_work_completed"]
    assert sorted(event["result"] for event in served) == sorted(event["result"] for event in opened)
    shortcut = next(event for event in opened if event["epoch"] == 2)
    assert shortcut["time"] <= topology[1]["time"]
    closed = [event for event in events if event["event"] == "edge_closed"]
    assert len(closed) == 1 and (closed[0]["service"], closed[0]["target"]) == ("service-0", "service-2")
    assert closed[0]["time"] <= topology[2]["time"]
    gone(events)


def test_insufficient_rolling_budget_blocks_the_change_and_cleans_up():
    """
    Worker ceilings include old and replacement generations during a rolling transition.
    """
    run = subprocess.run([sys.executable, "-I", str(SCRIPT), *FAST, "--worker-limit", "2"], capture_output=True, text=True, timeout=30)
    events = records(run.stdout)
    assert run.returncode != 0 and any(event["event"] == "blocked" for event in events)
    assert not any(event["event"] == "success" for event in events)
    assert all(event["live"] <= 2 for event in events if event["event"] == "worker_spawned")
    gone(events)


def test_interruption_cleans_the_subprocess_tree():
    """
    Root cancellation closes services and their children without reporting experiment success.
    """
    process = subprocess.Popen([sys.executable, "-I", str(SCRIPT), *FAST], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    prefix = []
    try:
        while line := process.stdout.readline():
            prefix.append(line)
            if '"event": "load_started"' in line:
                process.send_signal(signal.SIGINT)
                break
        output, errors = process.communicate(timeout=20)
        events = records("".join(prefix) + output)
        assert process.returncode != 0, errors
        assert not any(event["event"] == "success" for event in events)
        gone(events)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=15)


@pytest.mark.parametrize("arguments", [["--hard-minimum", "1.1"], ["--worker-limit", "7"], ["--tick", "nan"], ["--jobs", "0"]])
def test_invalid_limits_fail_before_process_creation(arguments):
    """
    Impossible initial bounds and unbounded timing inputs cannot start the experiment.
    """
    run = subprocess.run([sys.executable, "-I", str(SCRIPT), *arguments], capture_output=True, text=True, timeout=5)
    assert run.returncode == 2 and not records(run.stdout)
