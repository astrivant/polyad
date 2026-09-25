"""
Execute Polyad health checks in Argo CD's sandbox against lifecycle and hierarchy observations.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from lupa.lua54 import LuaRuntime

from polyad.operator.observability.graph_status import instance_metrics, observed
from polyad_types.resources import GROUP, RESOURCE_TYPES
from tests.test_operator import resource

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
FLUX_CASES = json.loads((ROOT / "pkg/tests/flux/cases.json").read_text())


@pytest.fixture(scope="session")
def argocd_config(tmp_path_factory):
    """
    Render the same explicit customizations that administrators install.
    """
    path = tmp_path_factory.mktemp("argocd") / "argocd-cm.yaml"
    path.write_text(
        subprocess.check_output([sys.executable, str(ROOT / "scripts/gitops/argocd-health.py"), "--format", "configmap"], text=True)
    )
    return path


def assess(tmp_path, argocd_config, obj):
    """
    Run Argo's interpreter when installed, otherwise execute Polyad Lua through Lupa.
    """
    if shutil.which("argocd") is None:
        if obj.get("apiVersion", "").split("/", 1)[0] != GROUP:
            pytest.skip("Argo is required to evaluate built-in Kubernetes health checks")
        return assess_lupa(argocd_config, obj)
    path = tmp_path / "resource.yaml"
    path.write_text(yaml.safe_dump(obj))
    output = subprocess.check_output(
        ["argocd", "admin", "settings", "resource-overrides", "health", str(path), "--argocd-cm-path", str(argocd_config)], text=True
    )
    return yaml.safe_load(output)


def assess_lupa(argocd_config: Path, obj: dict[str, Any]) -> dict[str, str]:
    """
    Execute a generated Polyad health customization in a restricted Lua runtime.

    Args:
        argocd_config (Path): Rendered ConfigMap containing per-kind health scripts.
        obj (dict[str, Any]): Kubernetes resource exposed as Argo's ``obj`` global.

    Returns:
        dict[str, str]: Argo-compatible uppercase status and message fields.
    """
    data = yaml.safe_load(argocd_config.read_text())["data"]
    source = data[f"resource.customizations.health.{GROUP}_{obj['kind']}"]
    runtime = LuaRuntime(  # type: ignore[call-arg]
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=True,
        max_memory=8 * 1024 * 1024,
    )
    runtime.execute("python = nil; require = nil; package = nil; io = nil; os = nil; debug = nil; dofile = nil; loadfile = nil")

    # Execute the generated customization against an Argo-shaped global, not a Python reimplementation.
    runtime.globals()["obj"] = runtime.table_from(obj, recursive=True)
    result = runtime.execute(source, name=f"@argocd/{obj['kind']}/health.lua", mode="t")
    return {"STATUS": str(result["status"]), "MESSAGE": str(result["message"])}


@pytest.mark.parametrize("case", FLUX_CASES, ids=lambda case: case["name"])
def test_flux_cases_match_argocd_lifecycle(tmp_path, argocd_config, case):
    """
    Keep Flux Current/InProgress/Failed equivalent to Argo Healthy/Progressing-or-Suspended/Degraded.
    """
    argo = assess(tmp_path, argocd_config, case["object"])["STATUS"]
    expected = {"Current": "Healthy", "InProgress": ("Progressing", "Suspended"), "Failed": "Degraded"}[case["expected"]]
    assert argo in expected if isinstance(expected, tuple) else argo == expected


def graph(kind="Graph"):
    """
    Produce a real, generation-current status snapshot for an empty completed graph.
    """
    obj = resource(kind, "root", {"nodes": []})
    if kind == "ReplicaGroup":
        obj["spec"] = {"replicas": 0, "template": {"kind": "Graph", "ref": "template"}}
    obj["status"] = {"observedGeneration": 1, "phase": "Completed", "completed": True, "ready": True}
    if kind == "ReplicaGroup":
        obj["status"].update(scaleCurrent=True, observedRemoteScaleIntent="")
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
            subprocess.check_output([sys.executable, str(ROOT / "scripts/gitops/argocd-health.py"), "--format", format_name], text=True)
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


def test_active_sdk_adaptation_marks_its_daemon_definition_progressing(tmp_path, argocd_config):
    """
    Attribute a workload's changing organization to its reusable definition, not its containing graph.
    """
    obj = resource("Daemon", "consumer", {})
    obj["status"] = {
        "observedGeneration": 1,
        "progressing": True,
        "adaptation": {
            "inProgress": True,
            "invocations": {"call": {"node": "source", "strategy": "TopologyStrategy"}},
        },
    }
    result = assess(tmp_path, argocd_config, obj)
    assert result["STATUS"] == "Progressing"
    assert "1 active" in result["MESSAGE"]
    obj["metadata"]["generation"] = 2
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == "Healthy"


def test_lupa_executes_generated_argocd_health(argocd_config: Path) -> None:
    """
    Exercise the embedded fallback even when the Argo CLI is installed.

    Args:
        argocd_config (Path): Rendered Argo configuration fixture.

    Returns:
        None: Assertions verify the generated program's embedded execution.
    """
    obj = graph()
    assert assess_lupa(argocd_config, obj) == {
        "STATUS": "Healthy",
        "MESSAGE": "Completed; graphs 1, leaves 0, pending 0, ready 0, completed 0, failed 0",
    }


@pytest.mark.parametrize("kind", ["Workload", "Daemon", "Resource", "Gate", "ShutdownPolicy", "GraphPolicy"])
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
        ("waiting_descendant", "Progressing"),
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


@pytest.mark.parametrize("kind", ["OperatorPool", "RemoteScale", "DragonflyPool"])
@pytest.mark.parametrize("phase,expected", [("Ready", "Healthy"), ("Pending", "Progressing"), ("Blocked", "Degraded")])
def test_root_control_health(tmp_path, argocd_config, kind, phase, expected):
    """
    Central scale resources report progress without pretending to own graph metrics.
    """
    obj = resource(kind, "remote")
    obj["status"] = {"phase": phase, "observedGeneration": 1}
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == expected


@pytest.mark.parametrize("kind", ["TemporaryConnection", "OperatorPool", "RemoteScale", "DragonflyPool"])
def test_auxiliary_explicit_failure_flag_is_degraded(tmp_path, argocd_config, kind):
    """
    Keep the shared failure flag authoritative for specialized resource phases.
    """
    obj = resource(kind, "resource")
    obj["status"] = {"phase": "Ready", "observedGeneration": 1, "failed": True}
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == "Degraded"


def test_replica_group_waits_for_current_scale_intent(tmp_path, argocd_config):
    """
    Match Flux's metadata-sensitive scale fence before reporting a ready group.
    """
    obj = graph("ReplicaGroup")
    obj["status"]["scaleCurrent"] = False
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == "Progressing"
    obj["status"]["scaleCurrent"] = True
    obj["metadata"]["annotations"] = {"polyad.astrivant.com/remote-scale-intent": "new"}
    assert assess(tmp_path, argocd_config, obj)["STATUS"] == "Progressing"
