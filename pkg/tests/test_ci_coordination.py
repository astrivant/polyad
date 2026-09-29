"""
Exercise the HA smoke test's EndpointSlice polling through primary failover.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[2]


def probe(monkeypatch, snapshots):
    """
    Execute the production HA probe against recorded API responses and a simulated clock.
    """
    script = (ROOT / "scripts/testing/test-coordination.sh").read_text().split("<<'PYHA'\n", 1)[1].split("\nPYHA", 1)[0]
    responses = Mock(side_effect=[json.dumps(snapshot).encode() for snapshot in snapshots])
    monkeypatch.setattr(subprocess, "check_output", responses)
    monkeypatch.setenv("POLYAD_TEST_NAMESPACE", "failover-test")
    monkeypatch.setenv("POLYAD_TEST_PREVIOUS_PRIMARY", "queue-0")
    clock = [0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def advance(seconds):
        """
        Advance polling deadlines without sleeping or contacting a cluster.
        """
        clock[0] += seconds

    monkeypatch.setattr(time, "sleep", advance)
    exec(compile(script, "test-coordination.sh:PYHA", "exec"), {})
    return responses, clock[0]


@pytest.mark.parametrize("empty_slice", [{}, {"endpoints": None}, {"endpoints": []}])
def test_failover_waits_through_empty_slices_for_one_ready_replacement(monkeypatch, capsys, empty_slice):
    """
    Empty slices, the former primary, and unready replacements cannot satisfy failover.
    """
    old = {"targetRef": {"name": "queue-0"}, "conditions": {"ready": True}}
    pending = {"targetRef": {"name": "queue-1"}, "conditions": {"ready": False}}
    promoted = {"targetRef": {"name": "queue-1"}, "conditions": {"ready": True}}
    snapshots = [
        {"items": []},
        {"items": [empty_slice]},
        {"items": [{"endpoints": [old]}]},
        {"items": [{"endpoints": [pending]}]},
        {"items": [{"endpoints": [old, promoted]}]},
        {"items": [empty_slice, {"endpoints": [promoted]}]},
    ]
    responses, elapsed = probe(monkeypatch, snapshots)
    assert responses.call_count == len(snapshots)
    assert elapsed == 10
    assert responses.call_args.args[0] == [
        "kubectl",
        "-n",
        "failover-test",
        "get",
        "endpointslices",
        "-l",
        "kubernetes.io/service-name=polyad-queue",
        "-o",
        "json",
    ]
    assert "Primary Service promoted queue-1 while queue-0 is unavailable." in capsys.readouterr().out


def test_failover_still_times_out_when_no_replacement_becomes_ready(monkeypatch, capsys):
    """
    An empty endpoint list retries only until the existing five-minute deadline.
    """
    with pytest.raises(SystemExit, match="Dragonfly primary failover did not complete"):
        probe(monkeypatch, [{"items": [{"endpoints": None}]}] * 150)
    assert "promoted" not in capsys.readouterr().out
