"""
Keep categorized exceptions canonical, compatible and independent of their consumers.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from polyad.exceptions.api import Conflict, Forbidden, RequestError, Unauthorized, Unavailable
from polyad.exceptions.auth import LaneFull
from polyad.exceptions.coordination import PulseDeferred
from polyad.exceptions.graph import CheegerIncomplete
from polyad.exceptions.kubernetes import WriteConflict
from polyad.exceptions.reconciliation import Pending
from polyad.graph.cheeger import CheegerResult
from polyad_sdk.exceptions.api import APIError
from polyad_sdk.exceptions.events import StreamInterrupted
from polyad_sdk.exceptions.processes import _Aborted
from polyad_sdk.processes.models import PlanResult
from polyad_types.events.envelope import Event

ROOT = Path(__file__).resolve().parents[2]
LEGACY_IMPORTS = (
    ("polyad.api.http.errors", "polyad.exceptions.api", "RequestError"),
    ("polyad.api.http.errors", "polyad.exceptions.api", "Conflict"),
    ("polyad.api.http.errors", "polyad.exceptions.api", "Unauthorized"),
    ("polyad.api.http.errors", "polyad.exceptions.api", "Forbidden"),
    ("polyad.api.http.errors", "polyad.exceptions.api", "Unavailable"),
    ("polyad.auth.lanes", "polyad.exceptions.auth", "LaneFull"),
    ("polyad.compiler.passes.mutations", "polyad.exceptions.compiler", "PreconditionFailed"),
    ("polyad.events.store", "polyad.exceptions.events", "TopologyReplaced"),
    ("polyad.events.store", "polyad.exceptions.events", "CursorExpired"),
    ("polyad.graph.cheeger", "polyad.exceptions.graph", "CheegerIncomplete"),
    ("polyad.operator.coordination.leases", "polyad.exceptions.coordination", "NotOwner"),
    ("polyad.operator.coordination.pulses", "polyad.exceptions.coordination", "PulseDeferred"),
    ("polyad.operator.coordination.write_queue", "polyad.exceptions.kubernetes", "WriteConflict"),
    ("polyad.operator.policies.rules", "polyad.exceptions.policies", "RuleViolation"),
    ("polyad.operator.reconciliation.controller", "polyad.exceptions.reconciliation", "Pending"),
    ("polyad.api.composition.app", "polyad.exceptions.api", "Conflict"),
    ("polyad.api.composition.app", "polyad.exceptions.api", "Unavailable"),
    ("polyad_sdk.transport.http", "polyad_sdk.exceptions.api", "APIError"),
    ("polyad_sdk.events.subscriptions", "polyad_sdk.exceptions.events", "StreamInterrupted"),
    ("polyad_sdk.processes.supervisor", "polyad_sdk.exceptions.processes", "_Aborted"),
    ("polyad_sdk", "polyad_sdk.exceptions.api", "APIError"),
    ("polyad_sdk.api", "polyad_sdk.exceptions.api", "APIError"),
    ("polyad_sdk", "polyad_sdk.exceptions.events", "StreamInterrupted"),
    ("polyad_sdk.events", "polyad_sdk.exceptions.events", "StreamInterrupted"),
    ("polyad_types.events.envelope", "polyad_types.exceptions.events", "EventTooLarge"),
    ("polyad_types.events", "polyad_types.exceptions.events", "EventTooLarge"),
    ("polyad_types", "polyad_types.exceptions.events", "EventTooLarge"),
)


@pytest.mark.parametrize("legacy,category,name", LEGACY_IMPORTS)
def test_legacy_exception_imports_are_the_canonical_class(legacy, category, name):
    """
    Keep existing catch clauses valid without wrapper subclasses or duplicate definitions.
    """
    canonical = getattr(importlib.import_module(category), name)
    previous = getattr(importlib.import_module(legacy), name)
    assert previous is canonical
    assert canonical.__module__ == category

    # Category roots expose the same public types, never the private abort signal.
    namespace = importlib.import_module(category.rsplit(".", 1)[0])
    if name.startswith("_"):
        assert name not in namespace.__all__
        assert name not in importlib.import_module(category).__all__
    else:
        assert name in namespace.__all__
        assert getattr(namespace, name) is canonical


@pytest.mark.parametrize(
    "package,count", [("polyad", 15), ("polyad_sdk", 2), ("polyad_types", 1), ("polyad_schemas", 0), ("polyad_benchmarks", 0)]
)
def test_each_distribution_has_an_owned_exception_namespace(package, count):
    """
    Reserve consistent import locations without inventing failures for exception-free packages.
    """
    namespace = importlib.import_module(f"{package}.exceptions")
    assert len(namespace.__all__) == count
    for name in namespace.__all__:
        exception = getattr(namespace, name)
        assert issubclass(exception, BaseException)
        assert exception.__module__.startswith(f"{package}.exceptions.")


def test_exception_categories_do_not_load_operator_implementations():
    """
    Importing catchable signals must not initialize controllers, numerical engines or clients.
    """
    script = """
import sys
import polyad.exceptions as exceptions
from polyad.exceptions import Pending, CheegerIncomplete, PulseDeferred
from polyad.exceptions.api import Conflict
assert Pending.__module__ == 'polyad.exceptions.reconciliation'
assert CheegerIncomplete.__module__ == 'polyad.exceptions.graph'
assert not {'kubernetes', 'networkx', 'numpy', 'lupa', 'redis', 'psycopg', 'kopf'}.intersection(sys.modules)
assert 'polyad.operator.reconciliation.controller' not in sys.modules
assert 'polyad.graph.cheeger' not in sys.modules
assert 'WriteConflict' not in vars(exceptions)

# This category intentionally preserves the Kubernetes ApiException base.
from polyad.exceptions import WriteConflict
from kubernetes.client.exceptions import ApiException
assert issubclass(WriteConflict, ApiException)
assert exceptions.WriteConflict is WriteConflict
try:
    exceptions.missing
except AttributeError:
    pass
else:
    raise AssertionError('unknown exception name was accepted')
"""
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True, timeout=30)


def test_request_exception_hierarchy_is_unchanged():
    """
    Preserve HTTP classification and existing broad ValueError or RuntimeError handlers.
    """
    assert Conflict.__bases__ == (RequestError, ValueError)
    assert Forbidden.__bases__ == (RequestError,)
    assert Unauthorized.__bases__ == (RequestError,)
    assert Unavailable.__bases__ == (RuntimeError,)


def test_retry_and_reconciliation_payloads_are_preserved():
    """
    Keep retry delays, graph phases and Kubernetes conflict metadata stable.
    """
    lane = LaneFull(4)
    assert lane.retry_after == 4
    assert str(lane) == "credential lane capacity exhausted"
    assert PulseDeferred(1.2).retry_after == 2
    assert PulseDeferred(-1).retry_after == 1
    assert Pending("wait").phase == "Reconciling"
    assert Pending("wait", phase="Draining").phase == "Draining"
    assert str(Pending("wait")) == "wait"
    conflict = WriteConflict("overlapping_pending_writes", status=429)
    assert conflict.conflict_reason == "overlapping_pending_writes"
    assert conflict.status == 429
    assert WriteConflict("overlap").status == 409


def test_diagnostic_payloads_remain_the_original_objects():
    """
    Retain caller-owned certificates and response data without copying or stringifying them.
    """
    body = {"error": "unavailable"}
    api_error = APIError(503, body)
    assert api_error.body is body
    assert api_error.status == 503
    assert str(api_error) == "Polyad API returned HTTP 503"
    event = Event("1-0", "reset", {})
    interrupted = StreamInterrupted(event)
    assert interrupted.event is event
    assert str(interrupted) == "Polyad event stream requires recovery: reset"
    result = CheegerResult(False, 0, 1, ("a",), 3, "CutBudget")
    incomplete = CheegerIncomplete(result)
    assert incomplete.result is result
    assert str(incomplete) == "Cheeger computation incomplete: CutBudget after 3 cuts"
    outcome = PlanResult("small", "Blocked", "capacity")
    assert _Aborted(outcome).result is outcome
