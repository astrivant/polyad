"""
Keep conflict repair isolated from candidate execution and release permissions.
"""

from __future__ import annotations

from tests.workflows import JOBS, PIPELINE


def test_repair_is_a_trusted_leaf_with_no_secret_or_candidate_checkout() -> None:
    """
    Permit one narrow writer without granting other stages additional authority.

    Returns:
        None: Events, permissions and trusted source boundaries remain explicit.
    """
    caller = JOBS["generated-repair"]
    job = JOBS["repair"]
    assert caller["uses"] == "./.github/workflows/stage-repair.yml"
    assert "needs" not in caller
    assert "pull_request_target" in caller["if"] and "schedule" in caller["if"]
    assert "github.event.repository.default_branch" in caller["if"]
    assert caller["permissions"] == job["permissions"] == {"contents": "write", "pull-requests": "read"}
    assert "secrets" not in caller and "environment" not in job
    assert job["concurrency"] == {"group": "generated-conflict-repair", "cancel-in-progress": "false"}
    checkout = job["steps"][0]
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    assert checkout["with"]["persist-credentials"] == "false"
    assert len(job["steps"]) == 3
    assert job["steps"][-1]["uses"].startswith("actions/github-script@")
    assert "scripts/repair/repair.mjs" in job["steps"][-1]["with"]["script"]
    assert set(PIPELINE["on"]["pull_request_target"]["types"]) == {"opened", "synchronize", "reopened", "ready_for_review"}


def test_privileged_events_cannot_enter_source_validation_or_release() -> None:
    """
    Keep PR execution out of the credential-bearing target-event path.

    Returns:
        None: Source and final checks reject the repair-only event types.
    """
    guard = "github.event_name != 'pull_request_target' && github.event_name != 'schedule'"
    assert JOBS["source"]["if"] == guard
    assert JOBS["verified"]["if"] == "always() && " + guard
    assert JOBS["test-stage"]["needs"] == JOBS["build-stage"]["needs"] == "source"
    assert "generated-repair" not in JOBS["verified"]["needs"]
    assert JOBS["deploy-stage"]["needs"] == ["source", "verified"]
    assert "needs.verified.result == 'success'" in JOBS["deploy-stage"]["if"]
    assert "needs.source.outputs.release-tag != ''" in JOBS["deploy-stage"]["if"]
