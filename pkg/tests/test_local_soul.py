"""
Verify the standalone Soul searching example against real spawned process trees.
"""

from __future__ import annotations

import importlib.util
import json
import multiprocessing as mp
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from polyad_sdk import AdaptiveService, PeerAvailabilityStrategy, Settings, TopologyStrategy

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


def test_sdk_deltas_drive_profile_changes_and_quiet_recovery(soul):
    """
    Exercise real SDK delivery into the demo's ABC implementation without spawning workers.
    """
    incoming, output = mp.get_context("spawn").Pipe(duplex=False)
    root, control = mp.get_context("spawn").Pipe()
    application = soul.AdaptiveService("service-0", incoming, control, soul.Settings())
    changes = []
    try:
        assert isinstance(application, AdaptiveService)
        assert isinstance(application.settings, Settings)
        assert application.runtime == soul.Settings()
        assert len(application.strategies) == 3
        assert isinstance(application.strategies[1], TopologyStrategy)
        assert isinstance(application.strategies[2], PeerAvailabilityStrategy)
        assert application.strategies[0].name == "profile-admission"
        application.on_change(changes.append)
        application.children[42] = soul.Child(Mock(), Mock(), "interactive", ready=True)
        application.candidate = None

        spike = soul.Observation(time=1, backlog=20, delta_backlog=20, completed=0, completed_delta=0, high=1, quiet=0, elapsed=2)
        application.publish_observation(spike)
        assert changes[0].baseline
        assert application.profile_available
        assert application.candidate is None
        assert application.view.candidates[0]["node"]["name"] == "worker-42"
        assert application.view.resources["backlog"] == 20

        sustained = soul.Observation(time=2, backlog=20, delta_backlog=0, completed=0, completed_delta=0, high=3, quiet=0, elapsed=3)
        application.publish_observation(sustained)
        assert application.candidate == soul.BATCH
        assert changes[-1].matching("resources.high")[0].difference == 2
        assert application.cursor == "2-0"

        count = len(changes)
        application.publish_observation(sustained)
        assert len(changes) == count
        assert application.cursor == "3-0"

        application.active = soul.BATCH
        application.quiescing = True
        application.children[42].role = "batch"
        application.candidate = None
        draining = soul.Observation(time=3, backlog=0, delta_backlog=-20, completed=20, completed_delta=20, high=0, quiet=0.1, elapsed=3)
        application.publish_observation(draining)
        assert application.candidate is None

        quiet = soul.Observation(time=4, backlog=0, delta_backlog=0, completed=20, completed_delta=0, high=0, quiet=1, elapsed=4)
        application.publish_observation(quiet)
        assert application.candidate == soul.INTERACTIVE
        assert changes[-1].matching("resources.quiet")[0].difference == pytest.approx(0.9)
    finally:
        application.children.clear()
        application.close()
        root.close()
        output.close()


def test_sdk_adaptation_rejects_stale_evidence_and_root_stop(soul, monkeypatch):
    """
    Recheck freshness before acting on a change and honor shutdown over demand.
    """
    incoming, output = mp.get_context("spawn").Pipe(duplex=False)
    root, control = mp.get_context("spawn").Pipe()
    application = soul.AdaptiveService("service-0", incoming, control, soul.Settings())
    changes = []
    try:
        application.on_change(changes.append)
        application.children[42] = soul.Child(Mock(), Mock(), "interactive", ready=True)
        application.candidate = None
        demand = soul.Observation(time=1, backlog=20, delta_backlog=20, completed=0, completed_delta=0, high=3, quiet=0, elapsed=2)
        application.publish_observation(demand)
        assert application.candidate == soul.BATCH
        application.candidate = None
        change = changes[-1]
        with monkeypatch.context() as patch:
            patch.setattr(application, "_clock", lambda: change.after.topology["observedAt"] + 301)
            application.adapt(change)
            assert application.candidate is None

        root.send("stop")
        application.receive_control()
        application.publish_observation(demand)
        assert not application.view.available
        assert not application.profile_available
        assert application.candidate is None
        with pytest.raises(RuntimeError, match="does not make HTTP"):
            application.events.event_settings()
    finally:
        application.children.clear()
        application.close()
        root.close()
        output.close()


def test_service_admission_applies_backpressure_and_respects_root_stop(soul):
    """
    Full queues defer input, and a root stop keeps admission closed when space returns.
    """
    incoming, output = mp.get_context("spawn").Pipe(duplex=False)
    root, control = mp.get_context("spawn").Pipe()
    application = soul.AdaptiveService("service-0", incoming, control, soul.Settings())
    try:
        for identity in range(65):
            output.send((identity, identity + 1, time.monotonic()))
        application.receive_producer_work()
        assert len(application.pending) == 64 and incoming.poll()
        assert application.accepted == set(range(64))
        root.send("stop")
        application.receive_control()
        application.pending.popleft()
        application.receive_producer_work()
        assert application.accepted == set(range(64)) and incoming.poll()
        assert application.stopping
        assert application._stopped.is_set()
    finally:
        application.close()
        root.close()
        output.close()


def test_partial_listener_startup_still_releases_owned_resources(soul, monkeypatch):
    """
    Failure before the service loop starts must close its listener and transferred pipes.
    """
    incoming, output = mp.get_context("spawn").Pipe(duplex=False)
    root, control = mp.get_context("spawn").Pipe()
    listener = Mock()
    listener.bind.side_effect = OSError("bind failed")
    monkeypatch.setattr(soul.socket, "socket", lambda: listener)
    monkeypatch.setattr(soul.signal, "signal", lambda signum, handler: None)
    try:
        with pytest.raises(OSError, match="bind failed"):
            soul.service("service-0", incoming, control, soul.Settings())
        listener.close.assert_called_once()
        assert incoming.closed and control.closed
    finally:
        incoming.close()
        control.close()
        root.close()
        output.close()


@pytest.fixture
def applications(soul):
    """
    Own lightweight service instances and pipes for protocol and strategy checks.
    """
    owned = []

    def create(name="service-0"):
        incoming, output = mp.get_context("spawn").Pipe(duplex=False)
        root, control = mp.get_context("spawn").Pipe()
        application = soul.AdaptiveService(name, incoming, control, soul.Settings())
        owned.append((application, root, output))
        return application, root

    yield create
    for application, root, output in owned:
        application.children.clear()
        application.close()
        root.close()
        output.close()


def test_work_sharing_uses_sdk_topology_and_live_peer_guard(soul, applications, monkeypatch):
    """
    SDK deltas select peers; fresh permission, readiness and capacity govern every send.
    """
    application, _ = applications()
    peer = soul.Peer(Mock(spec=socket.socket), "service-2", 2, ready=True, credit=5)
    application.peers[peer.name] = peer
    for identity in range(20):
        application.accepted.add(identity)
        application.metrics.offered[identity] = time.monotonic()
        application.pending.append((identity, identity + 1))
    application.refresh()
    assert isinstance(application.strategies[1], soul.WorkSharingStrategy)
    assert isinstance(application.strategies[1], TopologyStrategy)
    assert application.routes == ("service-2",)
    application.share_work()
    assert len(peer.jobs) == len(application.delegated) == 5
    assert len(application.pending) == 15 and not application.completed
    original = peer.jobs.copy()
    application.share_work()
    assert peer.jobs == original  # One outstanding batch even if more demand exists.
    for identity in original:
        application.complete_peer_job(peer, identity, (identity + 1) ** 2)
    peer.credit = 5
    with monkeypatch.context() as patch:
        patch.setattr(application, "_clock", lambda: time.time() + 301)
        application.share_work()
        assert not peer.jobs
    peer.draining = True
    application.share_work()
    assert not peer.jobs
    application.refresh()
    assert application.routes == ()


def test_receiver_rechecks_shared_capacity_when_advertisements_race(soul, applications):
    """
    Two senders cannot both consume the same advertised receiver slots.
    """
    receiver, _ = applications("service-2")
    first = soul.Peer(Mock(spec=socket.socket), "service-1", 1, ready=True)
    second = soul.Peer(Mock(spec=socket.socket), "service-0", 2, ready=True)
    jobs = [[receiver.runtime.jobs + index, receiver.runtime.jobs + index + 1] for index in range(8)]
    receiver.admit_peer_batch(first, jobs)
    assert receiver.peer_capacity() == 4
    receiver.admit_peer_batch(second, [[index, index + 1] for index in range(8)])
    rejection = json.loads(second.outgoing)
    assert rejection["kind"] == "rejected" and rejection["identities"] == list(range(8))
    assert len(receiver.pending) == len(receiver.replies) == 8
    assert not receiver.accepted.intersection(range(8))
    receiver.admit_peer_batch(second, [[index, index + 1] for index in range(4)])
    assert len(receiver.replies) == receiver.runtime.peer_window and receiver.peer_capacity() == 0
    with pytest.raises(RuntimeError, match="duplicate"):
        receiver.admit_peer_batch(first, jobs)
    with pytest.raises(RuntimeError, match="before receiving"):
        receiver.peer_message(first, {"kind": "drain", "epoch": 1}, outbound=False)


def test_sender_requeues_only_explicit_rejections_and_validates_results(soul, applications):
    """
    Rejection retains the original identity; uncertain failures never retry accepted work.
    """
    sender, _ = applications()
    peer = soul.Peer(Mock(spec=socket.socket), "service-2", 2, ready=True, credit=5)
    sender.peers[peer.name] = peer
    for identity in range(10):
        sender.accepted.add(identity)
        sender.metrics.offered[identity] = time.monotonic()
        sender.pending.append((identity, identity + 1))
    sender.refresh()
    sender.share_work()
    sent = sorted(peer.jobs)
    sender.requeue_rejected(peer, sent)
    assert not sender.delegated and not peer.jobs and len(sender.pending) == 10
    assert sender.metrics.sent[peer.name] == 0
    assert {identity for identity, _ in sender.pending} == set(range(10))
    peer.credit = 5
    sender.share_work()
    identity = min(peer.jobs)
    with pytest.raises(RuntimeError, match="incorrect"):
        sender.complete_peer_job(peer, identity, -1)
    assert identity in sender.delegated
    sender.complete_peer_job(peer, identity, (identity + 1) ** 2)
    with pytest.raises(RuntimeError, match="duplicate"):
        sender.complete_peer_job(peer, identity, (identity + 1) ** 2)
    peer.connection.recv.return_value = b""
    peer.connection.send.side_effect = lambda data: len(data)
    with pytest.raises(RuntimeError, match="disconnected"):
        sender.poll_peers()
    assert sender.delegated  # Ownership is preserved until failure cleanup, never requeued.


def test_removed_link_drains_inflight_jobs_before_root_acknowledgement(soul, applications):
    """
    Removing an edge stops new sends and waits for both results and the drain reply.
    """
    sender, root = applications()
    peer = soul.Peer(Mock(spec=socket.socket), "service-2", 1, ready=True, credit=8)
    sender.peers[peer.name] = peer
    sender.accepted.add(0)
    sender.metrics.offered[0] = time.monotonic()
    sender.delegated[0] = peer
    peer.jobs.add(0)
    root.send((2, {}, []))
    sender.receive_control()
    sender.maintain_links()
    assert peer.draining and not peer.drain_sent and not root.poll()
    sender.complete_peer_job(peer, 0, 1)
    sender.maintain_links()
    assert peer.drain_sent and not root.poll()
    peer.outgoing.clear()  # Model a completed socket write of the drain request.
    sender.peer_message(peer, {"kind": "drained", "epoch": 1}, outbound=True)
    sender.maintain_links()
    assert root.recv() == ("linked", "service-0", 2)
    assert not sender.peers and sender.completed == {0}
    peer.connection.close.assert_called_once()


def test_peer_stream_handles_partial_frames_and_rejects_truncation(soul):
    """
    TCP read boundaries cannot be mistaken for message boundaries.
    """
    left, right = socket.socketpair()
    left.setblocking(False)
    peer = soul.Peer(left)
    try:
        right.sendall(b'{"kind": "capacity", ')
        assert peer.pump() == []
        right.sendall(b'"epoch": 1, "slots": 3}\n{"kind": "ready", "epoch": 1}\n')
        assert peer.pump() == [{"kind": "capacity", "epoch": 1, "slots": 3}, {"kind": "ready", "epoch": 1}]
        right.sendall(b'{"kind":')
        assert peer.pump() == []
        right.close()
        with pytest.raises(RuntimeError, match="incomplete"):
            peer.pump()
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("epoch", [True, -1, None, "1"])
def test_peer_messages_require_the_exact_integer_revision(soul, applications, epoch):
    """
    A stale or malformed frame cannot replace current receiver capacity.
    """
    sender, _ = applications()
    peer = soul.Peer(Mock(spec=socket.socket), "service-2", 1, ready=True)
    with pytest.raises(RuntimeError, match="stale"):
        sender.peer_message(peer, {"kind": "capacity", "epoch": epoch, "slots": 12}, outbound=True)
    assert peer.credit == 0


def test_peer_protocol_has_finite_input_and_output_buffers(soul):
    """
    Oversized frames and a stalled receiver cannot consume unbounded memory.
    """
    connection = Mock(spec=socket.socket)
    peer = soul.Peer(connection)
    with pytest.raises(RuntimeError, match="bounded buffer"):
        peer.send("hello", source="x" * 8192)
    for _ in range(8):
        peer.send("hello", source="x" * 8000)
    with pytest.raises(RuntimeError, match="bounded buffer"):
        peer.send("hello", source="x" * 2000)
    peer.outgoing.clear()
    connection.recv.side_effect = [b"x" * 8192, b"x"]
    assert peer.pump() == []
    with pytest.raises(RuntimeError, match="8 KiB"):
        peer.pump()


@pytest.mark.parametrize("jobs", [192, 384])
def test_real_services_share_owned_work_roll_restore_and_join(jobs):
    """
    A real shortcut processes S0's queued jobs and drains before closing.
    """
    run = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), *FAST, "--mode", "adaptive", "--jobs", str(jobs)],
        capture_output=True,
        text=True,
        timeout=40,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    events = records(run.stdout)
    total = jobs + jobs // 3 + max(3, jobs // 12)
    assert events[-1]["event"] == "success" and events[-1]["completed"] == total and events[-1]["all_joined"]
    summary = next(event for event in events if event["event"] == "trial_verified")
    assert summary["shortcut_jobs"] > 0 and summary["peer_jobs"] >= summary["shortcut_jobs"]
    assert summary["worker_ceiling"] == 12 and summary["peer_window"] == 12
    assert summary["mean_latency_seconds"] > 0 and summary["source_backlog_seconds"] > 0
    load = next(event for event in events if event["event"] == "load_started")
    assert len(set([load["producer"], *load["services"], load["pid"]])) == 5
    adapted = 0
    for index, name in enumerate(("service-0", "service-1", "service-2")):
        local = [event for event in events if event.get("service") == name]
        commits = [event for event in local if event["event"] == "committed"]
        assert commits[0]["role"] == commits[-1]["role"] == "interactive"
        if any(event["role"] == "batch" for event in commits):
            adapted += 1
            assert [event["role"] for event in commits] == ["interactive", "batch", "interactive"]
            assert [event["workers"] for event in commits] == [1, 3, 1]
            assert [event["cheeger"] for event in commits] == [1, 1.5, 1]
        spawned = [event for event in local if event["event"] == "worker_spawned"]
        assert max(event["live"] for event in spawned) <= 4
        assert {event["worker"] for event in spawned} == {event["worker"] for event in local if event["event"] == "worker_joined"}
        ready = set()
        for event in local:
            if event["event"] == "worker_ready":
                ready.add(event["worker"])
            if event["event"] == "committed":
                replacements = [item["worker"] for item in spawned if item["role"] == event["role"] and item["time"] <= event["time"]]
                assert all(pid in ready for pid in replacements)
        restored = next(event for event in local if event["event"] == "baseline_restored")
        assert restored["completed"] == (jobs, jobs // 3, max(3, jobs // 12))[index]
        assert restored["workers"] == 1
    assert adapted >= 2
    topology = [event for event in events if event["event"] == "topology_committed"]
    assert [event["topology"] for event in topology] == ["chain", "triangle", "chain"]
    assert [event["cheeger"] for event in events if event["event"] == "topology_admitted"] == [1, 2, 1]
    opened = [event for event in events if event["event"] == "edge_opened"]
    assert {(event["service"], event["target"]) for event in opened} == {
        ("service-0", "service-1"),
        ("service-1", "service-2"),
        ("service-0", "service-2"),
    }
    served = [event for event in events if event["event"] == "peer_work_completed"]
    returned = [event for event in events if event["event"] == "delegated_work_completed"]
    assert len(served) == len(returned) == summary["peer_jobs"]
    assert len({event["job"] for event in served}) == len(served)
    assert sorted((event["job"], event["result"]) for event in served) == sorted((event["job"], event["result"]) for event in returned)
    assert all(event["result"] == (event["job"] + 1) ** 2 for event in returned)
    shortcut_results = [event for event in returned if event["service"] == "service-0" and event["target"] == "service-2"]
    assert len(shortcut_results) == summary["shortcut_jobs"] and all(0 <= event["job"] < jobs for event in shortcut_results)
    closed = next(
        event for event in events if event["event"] == "edge_closed" and event["service"] == "service-0" and event["target"] == "service-2"
    )
    assert closed["outstanding"] == 0 and max(event["time"] for event in shortcut_results) <= closed["time"] <= topology[-1]["time"]
    assert all(event["outstanding"] <= 12 for event in events if event["event"] == "peer_batch_accepted")
    gone(events)


def test_comparison_keeps_identical_work_and_resource_limits():
    """
    Both trials return actual measurements with the same load and process ceilings.
    """
    run = subprocess.run([sys.executable, "-I", str(SCRIPT)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    events = records(run.stdout)
    comparison = next(event for event in events if event["event"] == "comparison")
    base, changed = comparison["chain"], comparison["adaptive"]
    assert base["completed"] == changed["completed"] == 544
    assert base["worker_limit_per_service"] == changed["worker_limit_per_service"] == 4
    assert base["worker_ceiling"] == changed["worker_ceiling"] == 12
    assert base["peer_window"] == changed["peer_window"] == 12
    assert base["shortcut_jobs"] == 0 < changed["shortcut_jobs"]
    assert comparison["speedup"] == pytest.approx(base["elapsed_seconds"] / changed["elapsed_seconds"], abs=0.001)
    assert comparison["source_backlog_reduction_seconds"] == pytest.approx(
        base["source_backlog_seconds"] - changed["source_backlog_seconds"]
    )
    # Scheduling noise may change the measured gain; correctness never depends on a timing ratio.
    assert events[-1]["completed"] == 1088 and events[-1]["trials"] == 2
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
