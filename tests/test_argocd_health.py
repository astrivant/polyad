"""
Execute Polyad health checks in Argo CD's sandbox against lifecycle and hierarchy observations.
"""

from __future__ import annotations

import copy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from polyad.operator.graph_status import instance_metrics, observed
from polyad_types.resources import GROUP, RESOURCE_TYPES
from tests.test_operator import resource

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def argocd_config(tmp_path_factory):
    """
    Render the same explicit customizations that administrators install.
    """
    path = tmp_path_factory.mktemp("argocd") / "argocd-cm.yaml"
    path.write_text(subprocess.check_output([sys.executable, str(ROOT / "scripts/argocd-health.py"), "--format", "configmap"], text=True))
    return path


def assess(tmp_path, argocd_config, obj):
    """
    Run the real Argo Lua interpreter without contacting a cluster or opening Lua libraries.
    """
    if shutil.which("argocd") is None:
        pytest.skip("install argocd using scripts/install-asdf-tools.sh argocd")
    path = tmp_path / "resource.yaml"
    path.write_text(yaml.safe_dump(obj))
    output = subprocess.check_output(
        ["argocd", "admin", "settings", "resource-overrides", "health", str(path), "--argocd-cm-path", str(argocd_config)], text=True
    )
    return yaml.safe_load(output)


def graph(kind="Graph"):
    """
    Produce a real, generation-current status snapshot for an empty completed graph.
    """
    obj = resource(kind, "root", {"nodes": []})
    if kind == "ReplicaGroup":
        obj["spec"] = {"replicas": 0, "template": {"kind": "Graph", "ref": "template"}}
    obj["status"] = {"observedGeneration": 1, "phase": "Completed", "completed": True, "ready": True}
    obj["status"]["metrics"] = instance_metrics(obj, [])
    return obj


def test_configuration_formats_cover_only_polyad_kinds(argocd_config):
    """
    Keep built-in leaf checks and unrelated Argo settings outside the generated patch.
    """
    expected = {
        f"resource.customizations.health.{GROUP}_{kind}"
        for kind, descriptor in RESOURCE_TYPES.items()
        if descriptor.api_version.startswith(f"{GROUP}/")
    }
    data = yaml.safe_load(argocd_config.read_text())["data"]
    assert set(data) == expected
    for format_name, field in (("patch", "data"), ("helm", "configs")):
        output = yaml.safe_load(
            subprocess.check_output([sys.executable, str(ROOT / "scripts/argocd-health.py"), "--format", format_name], text=True)
        )
        assert set(output) == {field}
        assert (output[field]["cm"] if format_name == "helm" else output[field]) == data


@pytest.mark.parametrize(
    "phase,expected",
    [("Queued", "Progressing"), ("Ready", "Healthy"), ("Superseded", "Healthy"), ("Stopped", "Suspended"), ("Rejected", "Degraded")],
)
def test_activation_receipt_health(tmp_path, argocd_config, phase, expected):
    """
    Interpret durable pulse admission decisions without requiring graph metrics on receipts.
    """
    obj = resource("Activation", "pulse")
    obj["status"] = {"phase": phase, "observedGeneration": 1, "ready": phase == "Ready", "failed": phase == "Rejected"}
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected


@pytest.mark.parametrize("kind", ["Graph", "PolyGraph", "ReplicaGroup"])
def test_graph_kinds_and_templates(tmp_path, argocd_config, kind):
    """
    All graph boundaries report completion, while unexecuted templates are healthy definitions.
    """
    obj = graph(kind)
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == "Healthy"
    obj.pop("status")
    obj["spec"]["templateOnly"] = True
    assert "Reusable definition" in assess(tmp_path, argocd_config, obj)["MESSAGE"]


@pytest.mark.parametrize("kind", ["Workload", "Daemon", "Resource", "Gate", "ShutdownPolicy", "GraphRule"])
def test_definitions_do_not_claim_execution(tmp_path, argocd_config, kind):
    """
    Reusable library objects have no execution health to wait for.
    """
    result = assess(tmp_path, argocd_config, resource(kind, "definition", {}))
    assert result["STATUS"] == "Healthy"
    assert "Reusable definition" in result["MESSAGE"]


@pytest.mark.parametrize(
    "case,expected",
    [
        ("missing", "Progressing"),
        ("stale", "Progressing"),
        ("stale_metrics", "Progressing"),
        ("stale_rollup", "Progressing"),
        ("unknown_descendant", "Progressing"),
        ("failed_leaf", "Degraded"),
        ("invalid_descendant", "Degraded"),
        ("reconciling_descendant", "Progressing"),
        ("suspended_descendant", "Suspended"),
        ("invalid", "Degraded"),
        ("draining", "Progressing"),
        ("suspended", "Suspended"),
        ("stopped", "Suspended"),
        ("deleting", "Progressing"),
    ],
)
def test_graph_status_precedence(tmp_path, argocd_config, case, expected):
    """
    Never report stale success or hide failing descendants behind a ready parent.
    """
    obj = graph()
    status = obj["status"]
    rollup = status["metrics"]["rollup"]
    if case == "missing":
        obj.pop("status")
    elif case == "stale":
        obj["metadata"]["generation"] = 2
    elif case == "stale_metrics":
        status["metrics"]["observedGeneration"] = 0
    elif case == "stale_rollup":
        rollup["observedGeneration"] = 0
    elif case == "unknown_descendant":
        rollup.update(observationsComplete=False, unobservedGraphs=1)
    elif case == "failed_leaf":
        rollup["failedLeafNodes"] = 1
    elif case.endswith("_descendant"):
        rollup["graphsByPhase"][case.split("_")[0].capitalize()] = 1
    elif case == "deleting":
        obj["metadata"]["deletionTimestamp"] = "2026-09-15T00:00:00Z"
    else:
        status["phase"] = case.capitalize()
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected


@pytest.mark.parametrize(
    "phase,ready,expected", [("Running", False, "Progressing"), ("Running", True, "Healthy"), ("Waiting", False, "Progressing")]
)
def test_persistent_graph_lifecycle(tmp_path, argocd_config, phase, ready, expected):
    """
    Persistent work need not complete, but waiting without ready execution is progressing.
    """
    obj = graph("Graph")
    obj["spec"]["mode"] = "persistent"
    obj["status"].update(phase=phase, ready=ready, completed=False)
    obj["status"]["metrics"] = instance_metrics(obj, [])
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected


def test_rewrite_and_composition_receipts(tmp_path, argocd_config):
    """
    Receipts report application or root lifecycle without requiring graph metrics.
    """
    rewrite = resource("Rewrite", "change", {})
    rewrite["status"] = {"observedGeneration": 1, "applied": True}
    assert assess(tmp_path, argocd_config, rewrite)["STATUS"] == "Healthy"
    composition = resource("Composition", "request", {})
    composition["status"] = {"observedGeneration": 1, "phase": "Ready", "ready": True}
    assert assess(tmp_path, argocd_config, composition)["STATUS"] == "Healthy"
    composition["status"].update(phase="Failed", failed=True)
    assert assess(tmp_path, argocd_config, composition)["STATUS"] == "Degraded"


def test_failed_daemon_rolls_up_into_graph_health(tmp_path, argocd_config):
    """
    A Deployment rollout deadline degrades its graph even before the next lifecycle pass.
    """
    parent = graph()
    parent["spec"]["mode"] = "persistent"
    parent["spec"]["nodes"] = [{"name": "api", "kind": "Daemon", "ref": "api"}]
    child = resource("Deployment", "api", {"replicas": 1})
    child["metadata"]["labels"] = {f"{GROUP}/node": "api"}
    child["status"] = {
        "observedGeneration": 1,
        "conditions": [{"type": "Progressing", "status": "False", "reason": "ProgressDeadlineExceeded"}],
    }
    assert observed(child)["failed"]
    parent["status"]["metrics"] = instance_metrics(parent, [child])
    result = assess(tmp_path, argocd_config, parent)
    assert result["STATUS"] == "Degraded"
    assert "leaves 1" in result["MESSAGE"]
    assert "failed 1" in result["MESSAGE"]
    stale = copy.deepcopy(child)
    stale["metadata"]["generation"] = 2
    assert not observed(stale)["failed"]


def test_lost_persistent_volume_is_a_failed_leaf():
    """
    A lost claim must not leave its containing graph progressing forever.
    """
    claim = resource("PersistentVolumeClaim", "data", {})
    claim["status"] = {"phase": "Lost"}
    assert observed(claim)["failed"]
    assert not observed(claim)["ready"]


def test_nested_failed_job_reaches_polygraph_root(tmp_path, argocd_config):
    """
    Fold actual leaf observations through a nested graph into root Argo health.
    """
    job = resource("Job", "task", {})
    job["metadata"]["labels"] = {f"{GROUP}/node": "task"}
    job["status"] = {"conditions": [{"type": "Failed", "status": "True"}]}
    child = graph()
    child["metadata"]["labels"] = {f"{GROUP}/node": "child"}
    child["spec"]["nodes"] = [{"name": "task", "kind": "Workload", "ref": "task"}]
    child["status"]["metrics"] = instance_metrics(child, [job])
    parent = graph("PolyGraph")
    parent["spec"]["nodes"] = [{"name": "child", "kind": "Graph", "ref": "child"}]
    parent["status"]["metrics"] = instance_metrics(parent, [child])
    result = assess(tmp_path, argocd_config, parent)
    assert result["STATUS"] == "Degraded"
    assert "graphs 2" in result["MESSAGE"]
    assert "failed 1" in result["MESSAGE"]
    child["metadata"]["generation"] = 2
    parent["status"]["metrics"] = instance_metrics(parent, [child])
    assert assess(tmp_path, argocd_config, parent)["STATUS"] == "Progressing"


@pytest.mark.parametrize(
    "kind,status,expected",
    [
        ("Job", {"conditions": [{"type": "Failed", "status": "True"}]}, "Degraded"),
        ("Job", {"conditions": [{"type": "Complete", "status": "True"}]}, "Healthy"),
        ("PersistentVolumeClaim", {"phase": "Lost"}, "Degraded"),
    ],
)
def test_builtin_leaf_health_is_retained(tmp_path, argocd_config, kind, status, expected):
    """
    Native resources retain Argo CD health instead of being treated as definitions.
    """
    obj = resource(kind, "leaf", {})
    obj["apiVersion"] = "batch/v1" if kind == "Job" else "v1"
    obj["status"] = status
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected


@pytest.mark.parametrize(
    "phase,expected",
    [("Pending", "Progressing"), ("Active", "Healthy"), ("Expired", "Healthy"), ("Revoked", "Healthy"), ("Rejected", "Degraded")],
)
def test_temporary_connection_health(tmp_path, argocd_config, phase, expected):
    """
    Connection receipts report admission and observed cleanup without requiring graph metrics.
    """
    obj = resource("TemporaryConnection", "edge")
    obj["status"] = {"phase": phase, "observedGeneration": 1}
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected


@pytest.mark.parametrize("kind", ["OperatorPool", "RemoteScale"])
@pytest.mark.parametrize("phase,expected", [("Ready", "Healthy"), ("Pending", "Progressing"), ("Blocked", "Degraded")])
def test_root_control_health(tmp_path, argocd_config, kind, phase, expected):
    """
    Central scale resources report progress without pretending to own graph metrics.
    """
    obj = resource(kind, "remote")
    obj["status"] = {"phase": phase, "observedGeneration": 1}
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected
