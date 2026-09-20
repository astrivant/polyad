"""
Exercise approved subprocess plans against real readiness, rollback and draining behavior.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from polyad_sdk import (
    ConstraintAssessment,
    ConstraintStrategy,
    Environment,
    ManagedProcess,
    ProcessPlan,
    ProcessSpec,
    ProcessSupervisor,
    Telemetry,
)

WORKER = Path(__file__).parent / "data/sdk_process_worker.py"
AVAILABLE = Environment(None, {}, {}, True, None)


@pytest.fixture
def workers(tmp_path):
    """
    Construct workers using a concrete readiness and draining protocol.
    """

    # File handshakes prove the child reached readiness or drained, without timing-based guesses.
    def ready(process):
        return (tmp_path / f"{process.pid}.ready").is_file()

    def drain(process):
        (tmp_path / f"{process.pid}.drain").touch()
        return (tmp_path / f"{process.pid}.drained").is_file()

    def build(name):
        return ProcessSpec(name, (sys.executable, str(WORKER), str(tmp_path)), ready, drain)

    return build


def supervisor(plans, *, view=lambda: AVAILABLE, activate=lambda _: None, **options):
    """
    Configure finite test lifecycle windows without creating any implicit threads.
    """
    return ProcessSupervisor(
        plans,
        view=view,
        activate=activate,
        max_processes=options.pop("max_processes", 4),
        startup_timeout=options.pop("startup_timeout", 5),
        drain_timeout=options.pop("drain_timeout", 2),
        stop_timeout=options.pop("stop_timeout", 1),
        poll_interval=0.005,
        **options,
    )


def test_scale_reuses_workers_and_returns_to_original_profile(workers, tmp_path):
    """
    One-to-two-to-one transitions preserve unchanged roles and acknowledge removed work.
    """
    first, second = workers("first"), workers("second")
    switches = []
    owner = supervisor([ProcessPlan("idle", (first,)), ProcessPlan("busy", (first, second))], activate=switches.append)
    try:
        assert owner.active == () and owner.profile is None
        assert owner.propose("idle") and not owner.propose("idle")
        assert owner.reconcile().state == "Applied"
        original = owner.active[0]
        assert owner.reconcile().state == "Unchanged"
        owner.propose("busy")
        assert owner.reconcile().state == "Applied"
        extra = owner.active[1]
        assert owner.active[0] is original
        assert all(worker.spec.ready(worker) for worker in owner.active)
        owner.propose("idle")
        result = owner.reconcile()
        assert result.state == "Applied", result
        assert owner.active == (original,)
        assert extra.returncode is not None and (tmp_path / f"{extra.pid}.drained").exists()
    finally:
        owner.close()
    assert switches[-1] == () and original.returncode is not None
    assert owner.profile is None and not owner.active
    owner.close()
    with pytest.raises(RuntimeError, match="closed"):
        owner.propose("idle")


@pytest.mark.parametrize("failure", ["exit", "readiness", "activation"])
def test_failed_replacement_rolls_back_without_losing_old_workers(workers, failure):
    """
    Failed launch, readiness and atomic routing changes preserve the committed worker.
    """
    old, new = workers("old"), workers("new")
    if failure == "exit":
        new = replace(new, argv=(sys.executable, "-c", "raise SystemExit(7)"))
    elif failure == "readiness":
        new = replace(new, ready=lambda _: False)

    def activate(processes):
        if failure == "activation" and processes and processes[0].spec.name == "new":
            raise ValueError("application secret should never enter telemetry")

    owner = supervisor(
        [ProcessPlan("old", (old,)), ProcessPlan("new", (new,))],
        activate=activate,
        startup_timeout=0.8 if failure == "readiness" else 5,
    )
    try:
        owner.propose("old")
        assert owner.reconcile().state == "Applied"
        original = owner.active[0]
        owner.propose("new")
        result = owner.reconcile()
        assert result.state == "Failed" and "secret" not in result.reason
        assert owner.profile == "old" and owner.active == (original,) and original.returncode is None

        # Failed candidates must be joined and forgotten while the original worker remains usable.
        assert len(owner._owned) == 1
    finally:
        owner.close()


def test_overlap_capacity_and_fresh_constraints_precede_mutation(workers):
    """
    A replacement cannot exceed peak count or proceed after its guard loses evidence.
    """

    class Guard(ConstraintStrategy):
        def evaluate(self, current):
            return ConstraintAssessment(self.name, "blocked", "No lease")

    first, second = workers("first"), workers("second")
    owner = supervisor(
        [
            ProcessPlan("first", (first,)),
            ProcessPlan("second", (second,)),
            ProcessPlan("guarded", (first,), (Guard("lease", lambda _: None),)),
        ],
        max_processes=1,
    )
    try:
        owner.propose("first")
        assert owner.reconcile().state == "Applied"
        original = owner.active[0]
        for profile, reason in (("second", "overlap"), ("guarded", "lease")):
            owner.propose(profile)
            result = owner.reconcile()
            assert result.state == "Blocked" and reason in result.reason
            assert owner.active == (original,) and len(owner._owned) == 1
    finally:
        owner.close()


def test_freshness_is_rechecked_after_readiness(workers):
    """
    Readiness cannot commit a proposal whose environmental evidence has since expired.
    """
    current = AVAILABLE

    def ready(process):
        nonlocal current
        if workers("unused").ready(process):
            current = replace(AVAILABLE, available=False, reason="expired")
            return True
        return False

    owner = supervisor([ProcessPlan("one", (replace(workers("one"), ready=ready),))], view=lambda: current)
    try:
        owner.propose("one")
        assert owner.reconcile().state == "Blocked"
        assert owner.profile is None and not owner._owned
    finally:
        owner.close()


@pytest.mark.parametrize("shutdown", [False, True])
def test_new_proposal_or_shutdown_supersedes_starting_workers(workers, shutdown):
    """
    Producers can update intent while a consumer waits for startup; rejected children are reaped.
    """
    waiting = threading.Event()

    def unready(_):
        waiting.set()
        return False

    owner = supervisor([ProcessPlan("old", (workers("old"),)), ProcessPlan("new", (replace(workers("new"), ready=unready),))])
    results = []
    thread = None
    try:
        owner.propose("old")
        assert owner.reconcile().state == "Applied"
        original = owner.active[0]
        owner.propose("new")
        thread = threading.Thread(target=lambda: results.append(owner.reconcile()))
        thread.start()
        assert waiting.wait(2)
        with pytest.raises(RuntimeError, match="already active"):
            owner.reconcile()
        if shutdown:
            owner.close()
        else:
            owner.propose("old")
        thread.join(timeout=3)
        assert not thread.is_alive() and results[0].state == "Superseded"
        if not shutdown:
            assert owner.active == (original,) and owner.reconcile().state == "Unchanged"
        else:
            assert original.returncode is not None and not owner._owned
    finally:
        owner.close()
        if thread is not None:
            thread.join(timeout=3)


def test_committed_worker_crash_is_repaired_even_during_cooldown(workers):
    """
    Reconcile a crashed role inside its committed profile while retaining profile-change cooldown.
    """
    worker = workers("worker")
    owner = supervisor([ProcessPlan("one", (worker,)), ProcessPlan("empty", ())], cooldown_seconds=60)
    try:
        owner.propose("one")
        assert owner.reconcile().state == "Applied"
        previous = owner.active[0]
        previous.stop(timeout=0.1)
        assert owner.reconcile().state == "Applied"
        assert owner.active[0].pid != previous.pid
        owner.propose("empty")
        assert owner.reconcile().state == "Blocked"
    finally:
        owner.close()


def test_empty_plan_commits_and_close_clears_its_identity():
    """
    An explicitly empty profile is useful for stopping a capability without losing lifecycle state.
    """
    owner = supervisor([ProcessPlan("empty", ())])
    owner.propose("empty")
    assert owner.reconcile().state == "Applied" and owner.profile == "empty"
    owner.close()
    assert owner.profile is None


def test_process_spec_is_immutable_and_python_constructor_uses_current_interpreter(workers):
    """
    Copy mutable environment inputs and keep command arguments separate from the executable.
    """
    worker = workers("worker")
    environment = {"APP_MODE": "idle"}
    spec = ProcessSpec.python(
        "worker", "application.worker", "--mode", "busy", ready=worker.ready, drain=worker.drain, environment=environment
    )
    environment["APP_MODE"] = "new"
    assert spec.argv == (sys.executable, "-m", "application.worker", "--mode", "busy")
    assert spec.environment["APP_MODE"] == "idle"
    with pytest.raises(TypeError):
        spec.environment["APP_MODE"] = "mutate"
    with pytest.raises(ValueError, match="unique"):
        ProcessPlan("bad", (worker, worker))
    with pytest.raises(ValueError, match="argv"):
        replace(worker, argv="echo unsafe")
    with pytest.raises(ValueError, match="module"):
        ProcessSpec.python("worker", "-c", ready=worker.ready, drain=worker.drain)


def test_forced_shutdown_reaps_uncooperative_direct_children(workers):
    """
    A missing drain acknowledgement cannot indefinitely retain a child's resources.
    """
    owner = supervisor([ProcessPlan("one", (replace(workers("one"), drain=lambda _: False),))], drain_timeout=0.4, stop_timeout=0.1)
    owner.propose("one")
    try:
        assert owner.reconcile().state == "Applied"
        child = owner.active[0]
        started = time.monotonic()
    finally:
        owner.close()
    assert time.monotonic() - started < 3 and child.returncode is not None


def test_child_receives_current_parent_trace_and_environment_snapshot(workers, tmp_path, monkeypatch):
    """
    Replace inherited carriers with the startup span and pass only the configured environment base.
    """
    from opentelemetry.sdk.trace import TracerProvider

    provider = TracerProvider(shutdown_on_exit=False)
    instrument = Telemetry(tracer_provider=provider)
    captured = []
    original = ManagedProcess.__init__

    def capture(self, spec, environment):
        captured.append(dict(environment))
        original(self, spec, environment)

    monkeypatch.setattr(ManagedProcess, "__init__", capture)
    owner = supervisor(
        [ProcessPlan("one", (workers("one"),))], telemetry=instrument, environ={"APP_MODE": "test", "TRACEPARENT": "obsolete-carrier"}
    )
    try:
        with instrument.operation("adaptation.proposal"):
            carrier = instrument.propagation_environment()
            owner.propose("one")
        assert owner.reconcile().state == "Applied"
        child = owner.active[0]
        propagated = (tmp_path / f"{child.pid}.ready").read_text()
        assert propagated.split("-")[1] == carrier["TRACEPARENT"].split("-")[1]
        assert captured[0]["APP_MODE"] == "test" and "obsolete-carrier" not in captured[0].values()
    finally:
        owner.close()
        provider.shutdown()
