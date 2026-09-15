"""
Verify Boolean routing, unresolved observations and graph rewrite plots.
"""

from pathlib import Path
from threading import Event

import pytest
from polyad.balance import Graph, Scheduler
from polyad.graph import Control, Outcome, ShutdownContract, Work
from polyad.graph.gates import AND, NOT, NXOR, OR, XOR, Signal

from tests.test_balance import Unit


@pytest.mark.parametrize("a", [False, True, None])
@pytest.mark.parametrize("b", [False, True, None])
def test_gate_truth_tables(a: bool | None, b: bool | None) -> None:
    """
    Check Boolean truth tables including missing observations.

    Args:
        a (bool | None): First signal or unknown.
        b (bool | None): Second signal or unknown.

    Returns:
        None: All supported operators preserve their documented semantics.
    """
    facts = {name: value for name, value in (("a", a), ("b", b)) if value is not None}
    left, right = Signal("a"), Signal("b")
    assert AND(left, right).evaluate(facts) is (False if a is False or b is False else None if None in (a, b) else True)
    assert OR(left, right).evaluate(facts) is (True if a is True or b is True else None if None in (a, b) else False)
    assert NOT(left).evaluate(facts) is (None if a is None else not a)
    assert XOR(left, right).evaluate(facts) is (None if None in (a, b) else a != b)
    assert NXOR(left, right).evaluate(facts) is (None if None in (a, b) else a == b)


def test_routing_skips_dependents_and_completes_graph(tmp_path: Path) -> None:
    """
    Complete a composed graph while reporting unselected branches as skipped.

    Args:
        tmp_path (Path): Isolated graph journal.

    Returns:
        None: Unselected work and its dependents never execute.
    """
    graph = Graph(
        Work("outer", "v1"),
        [
            Unit(Work("off", "v1"), lambda *_: pytest.fail("false route executed")),
            Unit(Work("child", "v1", requires=("off",)), lambda *_: pytest.fail("skipped prerequisite executed")),
        ],
        directory=tmp_path,
        routes={"off": NOT(Signal("healthy"))},
        facts=lambda: {"healthy": True},
        notify=lambda _: None,
    )
    assert graph.run(Control(Event(), Event(), lambda _: None), None).checkpoint is None
    assert graph.scheduler is not None
    assert all(state.status == "skipped" for state in graph.scheduler.states.values())


def test_unknown_route_waits_until_shutdown(tmp_path: Path) -> None:
    """
    Avoid admitting a negated missing observation.

    Args:
        tmp_path (Path): Isolated scheduler journal.

    Returns:
        None: Unknown routing remains pending until the deadline blocks admission.
    """
    scheduler = Scheduler(
        [Unit(Work("unknown", "v1"), lambda *_: pytest.fail("unknown route executed"))],
        slots=1,
        directory=tmp_path,
        routes={"unknown": NOT(Signal("missing"))},
        shutdown=ShutdownContract(after_seconds=0.05),
        notify=lambda _: None,
    )
    assert scheduler.run() == {"unknown": "blocked"}


def test_each_rewrite_exports_a_plot(tmp_path: Path) -> None:
    """
    Export initial and mutated dependency graphs while work remains active.

    Args:
        tmp_path (Path): Isolated plot and journal directory.

    Returns:
        None: Each committed structural revision has a nonempty PNG.
    """
    scheduler: Scheduler

    def rewrite(control: Control, checkpoint: dict[str, object] | None) -> Outcome:
        """
        Insert a dependent while the root retains its worker.

        Args:
            control (Control): Scheduler control.
            checkpoint (dict[str, object] | None): Prior payload.

        Returns:
            Outcome: Root completion after acknowledgement.
        """
        scheduler.submit([Unit(Work("child", "v1", requires=("root",)), lambda *_: Outcome())]).result(timeout=10)
        return Outcome()

    scheduler = Scheduler(
        [Unit(Work("root", "v1"), rewrite)],
        slots=1,
        directory=tmp_path,
        plots=True,
        notify=lambda _: None,
    )
    assert scheduler.run() == {"root": "completed", "child": "completed"}
    plots = list(tmp_path.glob("graph-*.png"))
    assert len(plots) == 2
    assert all(plot.read_bytes().startswith(b"\x89PNG") for plot in plots)
