"""
Verify public extension contracts, independent implementations and optional imports.
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from polyad.balance import FIFO, Graph, Scheduler, SchedulingPolicy, ShortestRemaining
from polyad.cache import Cache, CacheBackend
from polyad.graph import Operation, OperationQueue, Outcome, ProcessOwner, Work, Workload
from polyad.operator.adapters import ResourceAPI, StateBackend
from polyad.operator.adapters.kubernetes import API
from polyad.operator.adapters.postgresql import StateStore
from polyad_sdk import AdaptiveService, Client, ConnectionNegotiator, EventSource, ThroughputReporter


@pytest.mark.parametrize(
    "contract",
    [
        Workload,
        ProcessOwner,
        SchedulingPolicy,
        EventSource,
        ThroughputReporter,
        ConnectionNegotiator,
        ResourceAPI,
        StateBackend,
        CacheBackend,
    ],
)
def test_public_contracts_require_implementations(contract):
    """
    Reject construction before dependencies, process ownership or network calls exist.
    """
    assert inspect.isabstract(contract)
    with pytest.raises(TypeError, match="abstract"):
        contract()


@pytest.mark.parametrize(
    ("implementation", "contract"),
    [
        (Graph, Workload),
        (ShortestRemaining, SchedulingPolicy),
        (FIFO, SchedulingPolicy),
        (Client, EventSource),
        (Client, ThroughputReporter),
        (Client, ConnectionNegotiator),
        (API, ResourceAPI),
        (StateStore, StateBackend),
        (Cache, CacheBackend),
    ],
)
def test_existing_backends_fulfill_the_public_contracts(implementation, contract):
    """
    Keep shipped implementations concrete while exposing their substitutable contracts.
    """
    assert issubclass(implementation, contract)
    assert not inspect.isabstract(implementation)


def test_partial_workload_cannot_skip_its_description():
    """
    Require the scheduling description before a workload can be instantiated.
    """

    class MissingDescription(Workload):
        def run(self, control, checkpoint):
            return Outcome()

    with pytest.raises(TypeError, match="abstract.*work"):
        MissingDescription()


def test_independent_policy_orders_ready_work_without_bypassing_dependencies(tmp_path):
    """
    Accept a policy unrelated to ShortestRemaining while preserving dependency admission.
    """
    completed = []

    class ReverseOrder(SchedulingPolicy):
        def rank(self, estimate, waiting, order):
            return 0, 0, -order

        def preempt(self, running, waiting, elapsed, waited):
            return False

    class Task(Workload):
        def __init__(self, work):
            self._work = work

        @property
        def work(self):
            return self._work

        def run(self, control, checkpoint):
            completed.append(self.work.name)
            return Outcome()

    Scheduler(
        [Task(Work("root", "v1")), Task(Work("child", "v1", requires=("root",))), Task(Work("free", "v1"))],
        slots=1,
        directory=tmp_path,
        policy=ReverseOrder(),
        notify=lambda _: None,
    ).run()
    assert completed == ["free", "root", "child"]


def test_operation_queue_owns_a_concrete_process_owner_through_join(tmp_path):
    """
    Execute through the ownership ABC and release every owner after its result.
    """
    lifecycle = []

    class Owner(ProcessOwner):
        def run(self, command, *, cwd, env, stdout, stderr, timeout):
            lifecycle.append(tuple(command))
            return subprocess.CompletedProcess(command, 0)

        def stop(self):
            lifecycle.append("joined")

    result = OperationQueue(
        [Operation("one", ("one",)), Operation("two", ("two",), requires=("one",))],
        workers=1,
        directory=tmp_path,
        cwd=tmp_path,
        environment={},
        owner_factory=Owner,
        notify=lambda _: None,
    ).run()
    assert lifecycle == [("one",), "joined", ("two",), "joined"]
    assert all(record["status"] == "completed" for record in result.values())


def test_contract_imports_do_not_load_optional_infrastructure():
    """
    Import adapters and SDK contracts in a fresh process without loading backend drivers.
    """
    root = Path(__file__).resolve().parents[2]
    source = """
import sys
sys.path[:0] = sys.argv[1:]
from polyad.cache import CacheBackend
from polyad.operator.adapters import ResourceAPI, StateBackend
from polyad_sdk import EventSource, ThroughputReporter, ConnectionNegotiator
for module in ('redis', 'psycopg', 'psycopg_pool', 'kubernetes', 'kopf', 'flask'):
    assert module not in sys.modules, module
"""
    subprocess.run(
        [sys.executable, "-I", "-c", source, str(root / "pkg"), str(root / "pkg/polyad-sdk"), str(root / "pkg/polyad-types")],
        check=True,
        capture_output=True,
        text=True,
    )


def test_sdk_accepts_an_event_source_without_the_http_client():
    """
    Deliver baselines, observations and checkpoints through an independent transport.
    """
    from polyad_sdk import Event
    from polyad_types import ServiceEndpoint

    class Source(EventSource):
        url = "memory://test-events"

        def topology(self, **selection):
            return {
                "graph": {"kind": "Graph", "namespace": "test", "name": "pipeline", "uid": "graph-1"},
                "node": {"name": "worker", "desired": True, "executions": []},
                "cursor": "0-0",
                "revision": "1",
                "observedAt": 100,
                "valid": True,
                "terminating": False,
                "templateOnly": False,
                "incoming": [],
                "outgoing": [],
                "dependencies": [],
                "dependents": [],
            }

        def events(self, **selection):
            yield Event(
                "1-0",
                "graph",
                {
                    "kind": "Graph",
                    "namespace": "test",
                    "name": "pipeline",
                    "uid": "graph-1",
                    "apiVersion": "polyad.astrivant.com/v1alpha1",
                    "resourceVersion": "1",
                    "generation": 1,
                    "type": "observation",
                    "owners": [],
                    "ancestry": [],
                    "audit": {},
                    "status": {},
                    "resources": {"backlog": 12},
                },
            )

        def event_endpoints(self, **selection):
            raise AssertionError("rebalance is disabled")

    changes, checkpoints = [], []

    class Service(AdaptiveService):
        def adapt(self, change):
            changes.append(change)

    source = Source()
    service = Service(
        ServiceEndpoint("", "test", "Graph", "pipeline", "graph-1", "worker"), source, clock=lambda: 100, checkpoint=checkpoints.append
    )
    service.run()
    assert changes[0].baseline
    assert changes[-1].after.resources["backlog"] == 12
    assert changes[-1].matching("resources")
    assert service.cursor == "1-0" and checkpoints == ["0-0", "1-0"]
    assert not isinstance(source, Client)
