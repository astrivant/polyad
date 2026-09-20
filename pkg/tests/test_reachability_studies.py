"""
Exercise local-study provenance, reduced-state errors and real process routing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from polyad_benchmarks import reachability, refresh
from polyad_benchmarks.routing_study import trial

ROOT = Path(__file__).resolve().parents[2]


def scenario(name):
    """
    Read the same versioned recipe used by the refresh workflow.
    """
    return json.loads((ROOT / "studies" / name / "fixtures/scenario.json").read_text())


def test_interaction_study_exposes_cost_to_the_other_service():
    """
    Benefiting one participant does not imply meeting both queue contracts.
    """
    config = {**scenario("symbiosis"), "numerical": False, "guardIterations": 3}
    results = {record["relationship"]: record for record in reachability.interactions(config)}
    assert results["mutualism"]["allowed"]
    assert not results["parasitism"]["allowed"]
    assert not results["competition"]["allowed"]
    assert len(results) == 6


def test_state_selection_exposes_optimistic_aggregation_and_missing_readiness():
    """
    Reduced models disagree with a fuller model under identical physical conditions.
    """
    config = {**scenario("reachability-state"), "numerical": False, "guardIterations": 3}
    results = reachability.state_variables(config)
    assert any(item["optimisticCount"] for item in results if item["representation"] == "pooled")
    assert any(item["optimisticCount"] for item in results if item["representation"] == "queues")
    assert all(item["optimisticCount"] == 0 for item in results if item["representation"] == "queues-and-readiness")
    assert {item["dimensions"] for item in results} == {1, 2, 3}


def test_local_refresh_preserves_identity_and_refuses_incomplete_publication(tmp_path):
    """
    Local studies share the prepare/study/finish barriers without Kubernetes credentials.
    """
    root = tmp_path / "run"
    refresh.prepare(ROOT, root, ("symbiosis",))
    with pytest.raises(ValueError, match="incomplete"):
        refresh.finish(ROOT, root)
    config = json.loads((root / "inputs/symbiosis.json").read_text())

    # Record an offline test recipe in provenance instead of bypassing verification.
    config["numerical"], config["guardIterations"] = False, 2
    refresh.write_json(root / "inputs/symbiosis.json", config)
    import hashlib

    provenance = json.loads((root / "provenance.json").read_text())
    provenance["inputs"]["symbiosis"] = hashlib.sha256((root / "inputs/symbiosis.json").read_bytes()).hexdigest()
    refresh.write_json(root / "provenance.json", provenance)
    refresh.study_phase(ROOT, root, "symbiosis", "")
    refresh.finish(ROOT, root)
    result = json.loads((root / "summary.json").read_text())["studies"]["symbiosis"]
    assert result["runId"] == config["runId"]
    assert len(result["records"]) == 6
    with pytest.raises(ValueError, match="already attempted"):
        refresh.study_phase(ROOT, root, "symbiosis", "")
    result["runId"] = "wrong"
    refresh.write_json(root / "outputs/symbiosis/results.json", result)
    with pytest.raises(ValueError, match="different run"):
        refresh.finish(ROOT, root)


@pytest.mark.parametrize("guarded", [False, True])
def test_process_trial_accounts_for_all_jobs_and_joins_the_tree(guarded):
    """
    Two consumers receive real producer jobs with explicit rejection and verified completion.
    """
    config = scenario("reachability-routing")
    result = trial(reachability.model_from_config(config), count=12, rate=30, horizon=2, guarded=guarded)
    assert result["completed"] + result["rejected"] == result["offered"] == 12
    assert result["allJoined"]
    assert result["predictedAllowed"] == guarded
    assert result["routed"][1] > 0 if guarded else result["routed"][1] == 0
    assert max(result["peakOutstanding"]) <= 20
