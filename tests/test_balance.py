"""
Exercise cooperative preemption, graph changes, checkpoint validation and resource ownership.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from threading import Event
from typing import TYPE_CHECKING

import pytest

from polyad.balance import Scheduler, ShortestRemaining, checkpoints
from polyad.graph import Control, Estimate, Outcome, Statistics, Work

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass
class Unit:
    """
    Bind a test workload description to deterministic cooperative behavior.

    Attributes:
        work (Work): Scheduling contract.
        action (Callable[[Control, dict[str, object] | None], Outcome]): Execution behavior.
    """

    work: Work
    action: Callable[[Control, dict[str, object] | None], Outcome]

    def run(self, control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Execute the supplied fixture behavior.

        Args:
            control (Control): Coordinator requests and telemetry sink.
            checkpoint (dict[str, object] | None): Previously saved state.

        Returns:
            Outcome: The fixture's completion or checkpoint.
        """
        return self.action(control, checkpoint)


def test_preemption_and_graph_events(tmp_path: Path) -> None:
    """
    Insert short work during execution and resume the paused parent without replaying completed units.

    Args:
        tmp_path (Path): Isolated scheduler artifacts.

    Returns:
        None: Checkpoint, dispatch and resume occur in order and graphs are retained.
    """
    order: list[str] = []

    def short(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Record the short job.

        Args:
            control (Control): Coordinator signals and progress sink.
            checkpoint (dict[str, object] | None): Restored fixture state.

        Returns:
            Outcome: Completion or a recoverable fixture checkpoint.
        """
        order.append("short")
        return Outcome()

    def long(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Submit a shorter job and pause at a known boundary.

        Args:
            control (Control): Coordinator signals and progress sink.
            checkpoint (dict[str, object] | None): Restored fixture state.

        Returns:
            Outcome: Completion or a recoverable fixture checkpoint.
        """
        if checkpoint is not None:
            assert checkpoint == {"next": 1}
            order.append("resumed")
            control.report(Statistics(2, 2))
            return Outcome()
        order.append("long")
        scheduler.submit([Unit(Work("short", "v1", statistics=Statistics(estimate=Estimate(1))), short)]).result(timeout=3)
        control.report(Statistics(1, 2, Estimate(100, checkpoint_seconds=0.01, resume_seconds=0.01)))
        assert control.pause.wait(3)
        order.append("checkpoint")
        return Outcome({"next": 1})

    scheduler = Scheduler(
        [Unit(Work("long", "v1", resumable=True), long)],
        slots=1,
        directory=tmp_path,
        policy=ShortestRemaining(minimum_run_seconds=0),
        diagrams=True,
        notify=lambda _: None,
    )
    assert scheduler.run() == {"long": "completed", "short": "completed"}
    assert order == ["long", "checkpoint", "short", "resumed"]
    events = [json.loads(line)["event"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert {"graph_changed", "schedule_changed", "pause_requested", "checkpoint_committed", "resumed", "statistics"} <= set(events)
    assert list(tmp_path.glob("graph-*.mmd"))
    assert not (tmp_path / "checkpoints/long.json").exists()
    assert not (tmp_path / "scheduler.lock").exists()
    with pytest.raises(RuntimeError):
        scheduler.submit([]).result()


def test_checkpoint_identity_and_corruption(tmp_path: Path) -> None:
    """
    Preserve JSON state and reject stale or corrupted checkpoints.

    Args:
        tmp_path (Path): Checkpoint directory.

    Returns:
        None: Only matching, intact state is restored.
    """
    work = Work("a", "original", resumable=True)
    checkpoints.save(tmp_path, work, {"cursor": 4}, Statistics(4, 10))
    assert checkpoints.load(tmp_path, work) == ({"cursor": 4}, Statistics(4, 10))
    with pytest.raises(ValueError, match="changed"):
        checkpoints.load(tmp_path, Work("a", "modified"))
    path = tmp_path / "a.json"
    envelope = json.loads(path.read_text())
    envelope["body"] += " "
    path.write_text(json.dumps(envelope))
    with pytest.raises(ValueError, match="checksum"):
        checkpoints.load(tmp_path, work)


def test_dependency_mutations_and_resource_fit(tmp_path: Path) -> None:
    """
    Reject cyclic rewiring atomically and preserve resource reservations during pauses.

    Args:
        tmp_path (Path): Scheduler artifact directory.

    Returns:
        None: Invalid graph mutations leave the original graph intact.
    """

    def action(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Complete a graph-validation fixture.

        Args:
            control (Control): Coordinator signals and progress sink.
            checkpoint (dict[str, object] | None): Restored fixture state.

        Returns:
            Outcome: Completion or a recoverable fixture checkpoint.
        """
        return Outcome()

    scheduler = Scheduler(
        [Unit(Work("a", "v1"), action), Unit(Work("b", "v1", requires=("a",)), action)], slots=1, directory=tmp_path, notify=lambda _: None
    )
    invalid = scheduler.dependencies("a", ("b",))
    scheduler.run()
    with pytest.raises(ValueError, match="cycle"):
        invalid.result()
    assert scheduler.states["a"].work.requires == ()


def test_failure_cancels_and_joins_other_workers(tmp_path: Path) -> None:
    """
    Keep cancellation cooperative and wait for another worker to release its resources.

    Args:
        tmp_path (Path): Scheduler directory.

    Returns:
        None: Peer cleanup finishes before the scheduler propagates the original failure.
    """
    started, stopped = Event(), Event()

    def peer(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Wait for cooperative cancellation and record cleanup.

        Args:
            control (Control): Coordinator signals and progress sink.
            checkpoint (dict[str, object] | None): Restored fixture state.

        Returns:
            Outcome: Completion or a recoverable fixture checkpoint.
        """
        started.set()
        assert control.cancel.wait(3)
        stopped.set()
        return Outcome()

    def fail(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Fail only after the peer starts.

        Args:
            control (Control): Coordinator signals and progress sink.
            checkpoint (dict[str, object] | None): Restored fixture state.

        Returns:
            Outcome: Completion or a recoverable fixture checkpoint.
        """
        assert started.wait(3)
        raise RuntimeError("fixture failure")

    scheduler = Scheduler(
        [Unit(Work("peer", "v1"), peer), Unit(Work("fail", "v1"), fail)], slots=2, directory=tmp_path, notify=lambda _: None
    )
    with pytest.raises(RuntimeError, match="fixture failure"):
        scheduler.run()
    assert stopped.is_set()
    assert not (tmp_path / "scheduler.lock").exists()


def test_unknown_and_expensive_checkpoints_do_not_preempt() -> None:
    """
    Require known checkpoint costs and worthwhile predicted gains.

    Returns:
        None: Unknown or expensive pauses are rejected.
    """
    policy = ShortestRemaining()
    assert not policy.preempt(Estimate(100), Estimate(1), 10, 10)
    assert not policy.preempt(Estimate(100, checkpoint_seconds=80, resume_seconds=30), Estimate(1), 10, 10)
    assert not policy.preempt(Estimate(100, uncertainty_seconds=100, checkpoint_seconds=1, resume_seconds=1), Estimate(1), 10, 10)
    assert policy.preempt(Estimate(100, checkpoint_seconds=1, resume_seconds=1), Estimate(1), 10, 10)


def test_resume_checkpoint_after_scheduler_restart(tmp_path: Path) -> None:
    """
    Restore cumulative work from disk into a new workload instance.

    Args:
        tmp_path (Path): Durable checkpoint and journal directory.

    Returns:
        None: The new scheduler receives exactly the saved resume cursor.
    """
    work = Work("restored", "same-input", resumable=True)
    checkpoints.save(tmp_path / "checkpoints", work, {"next": 4}, Statistics(4, 5))

    def action(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Complete the one remaining unit.

        Args:
            control (Control): Progress sink.
            checkpoint (dict[str, object] | None): Persisted cursor.

        Returns:
            Outcome: Successful completion.
        """
        assert checkpoint == {"next": 4}
        control.report(Statistics(5, 5))
        return Outcome()

    scheduler = Scheduler([Unit(work, action)], slots=1, directory=tmp_path, restore=True, notify=lambda _: None)
    assert scheduler.run() == {"restored": "completed"}
    assert scheduler.states["restored"].statistics.completed == 5


def test_resources_stay_reserved_while_checkpointing(tmp_path: Path) -> None:
    """
    Permit overlap only after another worker releases enough capacity.

    Args:
        tmp_path (Path): Scheduler artifacts.

    Returns:
        None: Pausing work remains charged while the next job uses genuinely spare capacity.
    """
    pausing, short_finished = Event(), Event()
    started = Event()

    def short(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Confirm execution overlaps checkpointing without exceeding capacity.

        Args:
            control (Control): Coordinator requests.
            checkpoint (dict[str, object] | None): Unused fixture state.

        Returns:
            Outcome: Completed work.
        """
        assert pausing.is_set()
        assert scheduler.states["long"].status == "pausing"
        short_finished.set()
        return Outcome()

    def long(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Hold two slots during a deliberately observable checkpoint boundary.

        Args:
            control (Control): Pause request.
            checkpoint (dict[str, object] | None): Resume marker.

        Returns:
            Outcome: A checkpoint or completion on resumption.
        """
        if checkpoint is not None:
            return Outcome()
        started.set()
        scheduler.submit([Unit(Work("short", "v1", statistics=Statistics(estimate=Estimate(1))), short)]).result(timeout=3)
        assert control.pause.wait(3)
        pausing.set()
        assert short_finished.wait(3)
        return Outcome({"resume": True})

    def peer(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Release the spare slot only after the other workload begins pausing.

        Args:
            control (Control): Coordinator requests.
            checkpoint (dict[str, object] | None): Unused state.

        Returns:
            Outcome: Completed work that releases one slot.
        """
        assert started.wait(3)
        assert pausing.wait(3)
        return Outcome()

    scheduler = Scheduler(
        [
            Unit(
                Work(
                    "long",
                    "v1",
                    slots=2,
                    resumable=True,
                    statistics=Statistics(estimate=Estimate(100, checkpoint_seconds=0.1, resume_seconds=0.1)),
                ),
                long,
            ),
            Unit(Work("peer", "v1"), peer),
        ],
        slots=3,
        directory=tmp_path,
        policy=ShortestRemaining(minimum_run_seconds=0),
        notify=lambda _: None,
    )
    assert set(scheduler.run().values()) == {"completed"}


def test_graph_boundary_orders_dependents(tmp_path: Path) -> None:
    """
    Link nested graphs as parent-level work without unlocking successors early.

    Args:
        tmp_path (Path): Nested scheduler directories.

    Returns:
        None: Child work completes before a dependent outer workload starts.
    """
    from polyad.balance import Graph

    order: list[str] = []

    def first(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Record nested completion.

        Args:
            control (Control): Child control.
            checkpoint (dict[str, object] | None): Child checkpoint.

        Returns:
            Outcome: Completed child.
        """
        order.append("child")
        return Outcome()

    def after(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Check the parent graph's completion gate.

        Args:
            control (Control): Outer control.
            checkpoint (dict[str, object] | None): Outer checkpoint.

        Returns:
            Outcome: Completed successor.
        """
        assert order == ["child"]
        order.append("after")
        return Outcome()

    inner = Graph(
        Work("inner", "v1", resumable=True), [Unit(Work("child", "v1"), first)], directory=tmp_path / "inner", notify=lambda _: None
    )
    outer = Graph(Work("outer", "v1", resumable=True), [inner], directory=tmp_path / "outer", notify=lambda _: None)
    scheduler = Scheduler(
        [outer, Unit(Work("after", "v1", requires=("outer",)), after)], slots=1, directory=tmp_path / "root", notify=lambda _: None
    )
    assert set(scheduler.run().values()) == {"completed"}
    assert order == ["child", "after"]


def test_graph_checkpoint_restores_completed_members(tmp_path: Path) -> None:
    """
    Pause an entire graph and reconstruct it without replaying completed child work.

    Args:
        tmp_path (Path): Child checkpoint directory.

    Returns:
        None: Graph resume preserves membership, progress and dependency completion.
    """
    from polyad.balance import Graph

    counts = {"first": 0, "second": 0}
    pause = Event()

    def first(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Finish one child while requesting a parent pause.

        Args:
            control (Control): Child control.
            checkpoint (dict[str, object] | None): Child state.

        Returns:
            Outcome: Completed first child.
        """
        counts["first"] += 1
        pause.set()
        return Outcome()

    def second(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Finish the child that should remain pending at the pause boundary.

        Args:
            control (Control): Child control.
            checkpoint (dict[str, object] | None): Child state.

        Returns:
            Outcome: Completed second child.
        """
        counts["second"] += 1
        return Outcome()

    units = [Unit(Work("first", "v1"), first), Unit(Work("second", "v1", requires=("first",)), second)]
    work = Work("group", "v1", resumable=True)
    graph = Graph(work, units, directory=tmp_path, notify=lambda _: None)
    result = graph.run(Control(pause, Event(), lambda _: None), None)
    assert result.checkpoint is not None
    assert counts == {"first": 1, "second": 0}
    restored = Graph(work, units, directory=tmp_path, notify=lambda _: None)
    assert restored.run(Control(Event(), Event(), lambda _: None), result.checkpoint).checkpoint is None
    assert counts == {"first": 1, "second": 1}


def test_shutdown_checkpoints_then_finalizes(tmp_path: Path) -> None:
    """
    Drain active work before finalizers run and block dependent admissions.

    Args:
        tmp_path (Path): Isolated scheduler journal.

    Returns:
        None: Checkpoint and cleanup acknowledgements precede boundary release.
    """
    from polyad.graph import Finalizer, ShutdownContract

    attempts: list[bool] = []

    def action(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Publish a stop trigger and wait for the checkpoint request.

        Args:
            control (Control): Scheduler signals and progress channel.
            checkpoint (dict[str, object] | None): Prior payload.

        Returns:
            Outcome: Saved progress after cooperative shutdown.
        """
        control.report(Statistics(1, 2))
        assert control.pause.wait(2)
        return Outcome({"completed": 1})

    def finalize(state: object) -> bool:
        """
        Verify durable checkpointing and retry cleanup once.

        Args:
            state (object): Observed shutdown progress.

        Returns:
            bool: Whether the second cleanup attempt has completed.
        """
        assert (tmp_path / "scheduler.lock").exists()
        assert (tmp_path / "checkpoints" / "active.json").exists()
        attempts.append(True)
        return len(attempts) == 2

    scheduler = Scheduler(
        [
            Unit(Work("active", "v1", resumable=True), action),
            Unit(Work("next", "v1", requires=("active",)), lambda *_: pytest.fail("admitted after shutdown")),
            Unit(Work("last", "v1", requires=("next",)), lambda *_: pytest.fail("admitted after shutdown")),
        ],
        slots=1,
        directory=tmp_path,
        shutdown=ShutdownContract(
            when=lambda state: state.progress_units >= 1,
            finalizers=(Finalizer("save-report", finalize),),
            finalizer_retry_seconds=0,
        ),
        notify=lambda _: None,
    )
    assert scheduler.run() == {"active": "paused", "next": "blocked", "last": "blocked"}
    assert len(attempts) == 2
    assert not (tmp_path / "scheduler.lock").exists()
    events = [json.loads(line)["event"] for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert events.index("checkpoint_committed") < events.index("finalizer_completed") < events.index("shutdown_completed")


def test_shutdown_deadline_joins_active_worker(tmp_path: Path) -> None:
    """
    Cancel a cooperative running unit after its grace period and join before finalizing.

    Args:
        tmp_path (Path): Isolated scheduler journal.

    Returns:
        None: Finalizer observes the child's completed cleanup.
    """
    from polyad.graph import Finalizer, ShutdownContract

    joined = Event()

    def action(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Wait for deadline cancellation and acknowledge cleanup.

        Args:
            control (Control): Scheduler termination signals.
            checkpoint (dict[str, object] | None): Prior payload.

        Returns:
            Outcome: Cooperative termination.
        """
        control.report(Statistics(1, 2))
        assert control.cancel.wait(2)
        joined.set()
        return Outcome()

    scheduler = Scheduler(
        [Unit(Work("active", "v1"), action)],
        slots=1,
        directory=tmp_path,
        shutdown=ShutdownContract(
            when=lambda state: state.progress_units >= 1,
            grace_seconds=0,
            finalizers=(Finalizer("joined", lambda _: joined.is_set()),),
        ),
        notify=lambda _: None,
    )
    assert scheduler.run() == {"active": "cancelled"}
    assert joined.is_set()


def test_shutdown_reaches_nested_worker(tmp_path: Path) -> None:
    """
    Propagate a root shutdown through nested graphs and join their worker.

    Args:
        tmp_path (Path): Isolated parent and child journals.

    Returns:
        None: Nested cleanup precedes root finalization.
    """
    from polyad.balance import Graph
    from polyad.graph import Finalizer, ShutdownContract

    started, stopped = Event(), Event()

    def action(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Wait for cancellation inside the round's child graph.

        Args:
            control (Control): Nested control channel.
            checkpoint (dict[str, object] | None): Prior state.

        Returns:
            Outcome: Terminated child result.
        """
        started.set()
        assert control.cancel.wait(2)
        stopped.set()
        return Outcome()

    nested = Graph(
        Work("outer", "v1"),
        [
            Graph(
                Work("inner", "v1"),
                [Unit(Work("leaf", "v1"), action)],
                directory=tmp_path / "inner",
                notify=lambda _: None,
            )
        ],
        directory=tmp_path / "outer",
        notify=lambda _: None,
    )
    scheduler = Scheduler(
        [nested],
        slots=1,
        directory=tmp_path / "root",
        shutdown=ShutdownContract(
            when=lambda _: started.is_set(),
            grace_seconds=0,
            finalizers=(Finalizer("children-stopped", lambda _: stopped.is_set()),),
        ),
        notify=lambda _: None,
    )
    assert scheduler.run() == {"outer": "cancelled"}
    assert stopped.is_set()
    assert not (tmp_path / "inner" / "scheduler.lock").exists()


def test_finalizer_failure_retries_and_interruption_retains_lock(tmp_path: Path) -> None:
    """
    Retry ordinary cleanup errors but retain ownership when finalization is interrupted.

    Args:
        tmp_path (Path): Isolated scheduler directories.

    Returns:
        None: Failed cleanup cannot silently release the boundary.
    """
    from polyad.graph import Finalizer, ShutdownContract

    attempts: list[bool] = []

    def cleanup(state: object) -> bool:
        """
        Fail once before acknowledging durable cleanup.

        Args:
            state (object): Shutdown observation.

        Returns:
            bool: Acknowledgement after a transient failure.
        """
        attempts.append(True)
        if len(attempts) == 1:
            raise OSError("temporarily unavailable")
        return True

    Scheduler(
        [],
        slots=1,
        directory=tmp_path / "retry",
        shutdown=ShutdownContract(finalizers=(Finalizer("cleanup", cleanup),), finalizer_retry_seconds=0),
        notify=lambda _: None,
    ).run()
    assert len(attempts) == 2

    def interrupted(state: object) -> bool:
        """
        Simulate interruption before cleanup acknowledgement.

        Args:
            state (object): Shutdown observation.

        Returns:
            bool: No acknowledgement is returned.
        """
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        Scheduler(
            [],
            slots=1,
            directory=tmp_path / "interrupt",
            shutdown=ShutdownContract(finalizers=(Finalizer("cleanup", interrupted),)),
            notify=lambda _: None,
        ).run()
    assert (tmp_path / "interrupt" / "scheduler.lock").exists()


@pytest.mark.parametrize("depth_first", [False, True])
def test_graph_traversal_dispatch(tmp_path: Path, depth_first: bool) -> None:
    """
    Prefer layers or branch continuation without violating a shared descendant's prerequisites.

    Args:
        tmp_path (Path): Isolated scheduler journal.
        depth_first (bool): Select depth-first instead of breadth-first ordering.

    Returns:
        None: Serial dispatch matches the selected traversal while respecting joins.
    """
    from polyad.balance import BreadthFirst, DepthFirst

    observed: list[str] = []
    works = [
        Work("a", "v1"),
        Work("b", "v1"),
        Work("a-child", "v1", requires=("a",)),
        Work("b-child", "v1", requires=("b",)),
        Work("join", "v1", requires=("a-child", "b-child")),
    ]
    units = [Unit(work, lambda *_, name=work.name: (observed.append(name), Outcome())[1]) for work in works]
    Scheduler(
        units,
        slots=1,
        directory=tmp_path,
        policy=DepthFirst() if depth_first else BreadthFirst(),
        notify=lambda _: None,
    ).run()
    expected = ["a", "a-child", "b", "b-child", "join"] if depth_first else ["a", "b", "a-child", "b-child", "join"]
    assert observed == expected


def test_traversal_recomputes_for_graph_changes() -> None:
    """
    Include new descendants and respect changed edges when calculating traversal priority.

    Returns:
        None: Reused policies observe graph changes without stale traversal state.
    """
    from polyad.balance import BreadthFirst, DepthFirst

    works = {"a": Work("a", "v1"), "b": Work("b", "v1")}
    depth, breadth = DepthFirst(), BreadthFirst()
    assert list(depth.priorities(works)) == ["a", "b"]
    works["child"] = Work("child", "v1", requires=("a",))
    assert list(depth.priorities(works)) == ["a", "child", "b"]
    assert list(breadth.priorities(works)) == ["a", "b", "child"]
    works["child"] = Work("child", "v1", requires=("b",))
    assert list(depth.priorities(works)) == ["a", "b", "child"]


def test_traversal_supports_deep_graphs() -> None:
    """
    Traverse a deep pipeline without relying on Python recursion.

    Returns:
        None: Every node receives one priority despite a long chain.
    """
    from polyad.balance import BreadthFirst, DepthFirst

    works = {f"node-{index}": Work(f"node-{index}", "v1", requires=(f"node-{index - 1}",) if index else ()) for index in range(2000)}
    for policy in (BreadthFirst(), DepthFirst()):
        priorities = policy.priorities(works)
        assert len(priorities) == 2000
        assert priorities["node-1999"] == 1999
