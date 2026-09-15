"""
Exercise atomic rewrites, boundary-local registries and recursive shape identity.
"""

from __future__ import annotations

import json
from threading import Event
from typing import TYPE_CHECKING

import pytest

from polyad.balance import Graph, Scheduler
from polyad.graph import Control, Outcome, Rewrite, Work
from polyad.graph.gates import Signal
from tests.test_balance import Unit

if TYPE_CHECKING:
    from pathlib import Path


def unit(name: str, requires: tuple[str, ...] = ()) -> Unit:
    """
    Construct a no-op unit with explicit dependencies.

    Args:
        name (str): Stable node identity.
        requires (tuple[str, ...]): Prerequisites.

    Returns:
        Unit: Deterministic test implementation.
    """
    return Unit(Work(name, "v1", requires=requires), lambda *_: Outcome())


def test_all_rewrite_operations_are_atomic(tmp_path: Path) -> None:
    """
    Transform queued work through all six operations while its prerequisite runs.

    Args:
        tmp_path (Path): Isolated scheduler journal.

    Returns:
        None: Each named proposal commits once and the final graph completes.
    """
    scheduler: Scheduler
    hashes: list[str] = []

    def manipulate(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Apply registered proposals without releasing the root worker.

        Args:
            control (Control): Coordinator control.
            checkpoint (dict[str, object] | None): Prior payload.

        Returns:
            Outcome: Completion after every rewrite acknowledgement.
        """
        proposals = {
            "split": Rewrite.split("original", (unit("a", ("root",)), unit("b", ("root",))), (("sink", ("a", "b")),)),
            "fuse": Rewrite.fuse(("a", "b"), unit("fused", ("root",)), (("sink", ("fused",)),)),
            "splice": Rewrite.splice(unit("bridge", ("fused",)), "sink", ("bridge",)),
            "replicate": Rewrite.replicate((unit("r1", ("root",)), unit("r2", ("root",))), (("sink", ("bridge", "r1", "r2")),)),
            "replace": Rewrite.replace(("bridge",), (unit("replacement", ("fused",)),), (("sink", ("replacement", "r1", "r2")),)),
            "prune": Rewrite.prune(("r2",), (("sink", ("replacement", "r1")),)),
        }
        hashes.append(scheduler.shape_hash)
        for name, proposal in proposals.items():
            scheduler.rewrites.register(name, proposal)
            scheduler.rewrite(name).result(timeout=3)
            hashes.append(scheduler.shape_hash)
        return Outcome()

    scheduler = Scheduler(
        [Unit(Work("root", "v1"), manipulate), unit("original", ("root",)), unit("sink", ("original",))],
        slots=1,
        directory=tmp_path,
        notify=lambda _: None,
    )
    assert set(scheduler.run().values()) == {"completed"}
    assert len(set(hashes)) == 7
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    commits = [event for event in events if "rewrite" in event and event["event"] == "graph_changed"]
    assert len(commits) == 6
    assert all(event["before_hash"] != event["after_hash"] for event in commits)


@pytest.mark.parametrize(
    "proposal",
    [
        Rewrite.prune(("a",)),
        Rewrite(links=(("a", ("b",)),)),
        Rewrite(additions=(unit("new"), unit("new"))),
        Rewrite.prune(("missing",)),
    ],
)
def test_invalid_transaction_has_no_partial_effects(tmp_path: Path, proposal: Rewrite) -> None:
    """
    Preserve graph identity and membership on invalid multi-step edits.

    Args:
        tmp_path (Path): Isolated scheduler directory.
        proposal (Rewrite): Dangling edge, cycle, duplicate or unknown removal.

    Returns:
        None: Validation rejects the entire transaction.
    """
    scheduler = Scheduler([unit("a"), unit("b", ("a",))], slots=1, directory=tmp_path)
    before = scheduler.shape_hash
    states = dict(scheduler.states)
    with pytest.raises(ValueError):
        scheduler._rewrite(proposal)
    assert scheduler.states == states
    assert scheduler.shape_hash == before


def test_rewrite_cannot_remove_active_work(tmp_path: Path) -> None:
    """
    Retain ownership of an active unit when removal is requested.

    Args:
        tmp_path (Path): Isolated graph journal.

    Returns:
        None: The active unit completes normally after transaction rejection.
    """
    scheduler: Scheduler

    def action(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Attempt to remove the currently executing unit.

        Args:
            control (Control): Coordinator control.
            checkpoint (dict[str, object] | None): Prior payload.

        Returns:
            Outcome: Normal completion after rejection.
        """
        scheduler.rewrites.register("remove-self", Rewrite.prune(("active",)))
        with pytest.raises(ValueError, match="unstarted"):
            scheduler.rewrite("remove-self").result(timeout=3)
        return Outcome()

    scheduler = Scheduler([Unit(Work("active", "v1"), action)], slots=1, directory=tmp_path, notify=lambda _: None)
    assert scheduler.run() == {"active": "completed"}


def test_shape_hash_is_canonical_and_recursive(tmp_path: Path) -> None:
    """
    Ignore insertion order and progress but propagate child rewrites to ancestors.

    Args:
        tmp_path (Path): Isolated boundary paths.

    Returns:
        None: Hash changes correspond to labeled structural changes.
    """
    a = Scheduler([unit("a"), unit("b", ("a",))], slots=1, directory=tmp_path / "a")
    b = Scheduler([unit("b", ("a",)), unit("a")], slots=1, directory=tmp_path / "b")
    assert a.shape_hash == b.shape_hash
    a.states["a"].status = "completed"
    assert a.shape_hash == b.shape_hash
    child = Graph(Work("child", "v1"), [unit("a"), unit("b", ("a",))], directory=tmp_path / "child")
    parent = Graph(Work("parent", "v1"), [child], directory=tmp_path / "parent")
    original = parent.shape_hash
    child.scheduler = b
    assert parent.shape_hash == original
    b._rewrite(Rewrite(additions=(unit("c", ("b",)),)))
    assert parent.shape_hash != original
    assert parent.rewrites is not child.rewrites
    parent.rewrites.register("same", Rewrite())
    child.rewrites.register("same", Rewrite.prune(("a",)))
    assert parent.rewrites.get("same") != child.rewrites.get("same")
    with pytest.raises(ValueError):
        parent.rewrites.register("same", Rewrite())


def test_routes_change_shape_hash(tmp_path: Path) -> None:
    """
    Include routing expressions in structural identity.

    Args:
        tmp_path (Path): Isolated graph paths.

    Returns:
        None: Changing admission expressions changes the shape digest.
    """
    first = Graph(Work("graph", "v1"), [unit("a")], directory=tmp_path / "a", routes={"a": Signal("healthy")})
    second = Graph(Work("graph", "v1"), [unit("a")], directory=tmp_path / "b", routes={"a": Signal("ready")})
    assert first.shape_hash != second.shape_hash


def test_pruned_graph_restores_without_reintroducing_work(tmp_path: Path) -> None:
    """
    Preserve removal records when a rewritten composed graph pauses and resumes.

    Args:
        tmp_path (Path): Isolated graph checkpoint journal.

    Returns:
        None: Deleted work remains deleted after reconstruction.
    """
    pause = Event()
    graph: Graph

    def prune(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Remove pending work and request a graph checkpoint.

        Args:
            control (Control): Child control.
            checkpoint (dict[str, object] | None): Prior payload.

        Returns:
            Outcome: Root completion before the enclosing graph pauses.
        """
        graph.rewrite("prune").result(timeout=3)
        pause.set()
        return Outcome()

    initial = [Unit(Work("root", "v1"), prune), unit("removed", ("root",)), unit("remaining", ("root",))]
    graph = Graph(Work("graph", "v1", resumable=True), initial, directory=tmp_path, notify=lambda _: None)
    graph.rewrites.register("prune", Rewrite.prune(("removed",)))
    outcome = graph.run(Control(pause, Event(), lambda _: None), None)
    assert outcome.checkpoint is not None
    assert outcome.checkpoint["removed"] == ["removed"]
    restored = Graph(Work("graph", "v1", resumable=True), initial, directory=tmp_path, notify=lambda _: None)
    assert restored.run(Control(Event(), Event(), lambda _: None), outcome.checkpoint).checkpoint is None
    assert restored.scheduler is not None
    assert "removed" not in restored.scheduler.states


def test_nested_rewrite_updates_ancestor_journal(tmp_path: Path) -> None:
    """
    Observe a child transaction from the running parent without manually refreshing hashes.

    Args:
        tmp_path (Path): Parent and child journal directories.

    Returns:
        None: Parent records the changed descendant digest.
    """
    child: Graph

    def change(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Add a child-local unit while holding the root slot.

        Args:
            control (Control): Child coordinator signals.
            checkpoint (dict[str, object] | None): Prior state.

        Returns:
            Outcome: Completion after the graph has changed.
        """
        child.rewrite("extend").result(timeout=3)
        return Outcome()

    child = Graph(
        Work("child", "v1"),
        [Unit(Work("root", "v1"), change)],
        directory=tmp_path / "child",
        notify=lambda _: None,
    )
    child.rewrites.register("extend", Rewrite(additions=(unit("next", ("root",)),)))
    parent = Scheduler([child], slots=1, directory=tmp_path / "parent", notify=lambda _: None)
    before = parent.shape_hash
    assert parent.run() == {"child": "completed"}
    assert parent.shape_hash != before
    events = [json.loads(line) for line in (tmp_path / "parent/events.jsonl").read_text().splitlines()]
    assert any(event.get("reason") == "descendant shape changed" for event in events)
