"""
Check the GitOps test environment against the real chart and resource registry.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml
from deepdiff import DeepDiff

from polyad.compiler.registry import DEFINITION_KINDS

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]
TERRAFORM = ROOT / "terraform"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and chart dependencies")


def bootstrap(*settings: str, namespace: str = "argocd") -> list[dict[str, Any]]:
    """
    Render the local GitOps bootstrap chart without contacting a cluster.

    Args:
        *settings (str): Helm set expressions overriding bootstrap values.
        namespace (str): Namespace containing the selected GitOps controller.

    Returns:
        list[dict[str, Any]]: Rendered Kubernetes resources.
    """
    command = ["helm", "template", "polyad-bootstrap", str(TERRAFORM / "bootstrap"), "--namespace", namespace]
    for setting in settings:
        command.extend(["--set", setting])
    return list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))


@pytest.fixture(scope="module")
def installation():
    """
    Render the same values file Argo fetches from the chosen Git revision.
    """
    command = [
        "helm",
        "template",
        "polyad",
        str(ROOT / "charts/polyad"),
        "--namespace",
        "polyad",
        "--include-crds",
        "--skip-tests",
        "--values",
        str(TERRAFORM / "polyad-values.yaml"),
    ]
    return list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))


def test_git_application_preserves_autoscaling(installation):
    """
    Every component autoscaler target must retain its live count during sync.
    """
    resources = {(obj["kind"], obj["metadata"]["name"]): obj for obj in bootstrap()}
    project, application = resources["AppProject", "polyad"], resources["Application", "polyad"]
    spec = application["spec"]
    assert spec["source"]["repoURL"] == "https://github.com/astrivant/polyad.git"
    assert spec["source"]["path"] == "charts/polyad"
    assert spec["source"]["targetRevision"] == "main"
    assert spec["source"]["helm"]["skipTests"] is True
    assert "RespectIgnoreDifferences=true" in spec["syncPolicy"]["syncOptions"]
    assert "ServerSideApply=true" in spec["syncPolicy"]["syncOptions"]
    assert not application["metadata"].get("finalizers")

    exclusions = {(item["group"], item["kind"], item["name"]): item for item in spec["ignoreDifferences"]}
    for resource in installation:
        if resource["kind"] == "HorizontalPodAutoscaler":
            target = resource["spec"]["scaleTargetRef"]
            identity = (target["apiVersion"].split("/")[0], target["kind"], target["name"])
            assert exclusions[identity]["managedFieldsManagers"] == ["kube-controller-manager"]
            assert exclusions[identity]["namespace"] == "polyad"
        if resource["kind"] != "ScaledObject":
            continue
        target = resource["spec"]["scaleTargetRef"]
        identity = (target["apiVersion"].split("/")[0], target["kind"], target["name"])
        assert exclusions[identity]["jsonPointers"] == ["/spec/replicas"]
        assert exclusions[identity]["namespace"] == "polyad"

    permitted = {destination["namespace"] for destination in project["spec"]["destinations"]}
    assert {resource["metadata"].get("namespace", "polyad") for resource in installation} <= permitted
    assert project["spec"]["sourceRepos"] == [spec["source"]["repoURL"]]
    for path in spec["source"]["helm"]["valueFiles"]:
        assert (ROOT / "charts/polyad" / path).is_file()


def test_freezing_automatic_sync():
    """
    Frozen experiments keep a manual sync policy and the requested Git revision.
    """
    application = next(
        obj
        for obj in bootstrap("automatedSync=false", "revision=test-run")
        if obj["metadata"]["name"] == "polyad" and obj["kind"] == "Application"
    )
    assert "automated" not in application["spec"]["syncPolicy"]
    assert application["spec"]["source"]["targetRevision"] == "test-run"


def test_load_test_profile_has_graph_scalers_and_internal_metrics(installation):
    """
    Verify the baseline's operational graph rather than only its values keys.
    """
    objects = {(item["kind"], item["metadata"]["name"]): item for item in installation}
    graph = objects["Graph", "polyad-control-plane"]
    assert len(graph["spec"]["nodes"]) == 3
    for component in ("gateway", "executor", "telemetry"):
        group = objects["ReplicaGroup", f"polyad-{component}"]
        assert not DeepDiff(
            {key: group["spec"][key] for key in ("replicas", "minReplicas", "maxReplicas")},
            {"replicas": 2, "minReplicas": 2, "maxReplicas": 8},
        )
        assert ("ScaledObject", f"polyad-{component}") in objects
    assert ("Deployment", "keda-operator") in objects
    metrics = objects["Service", "polyad-polyad-metrics"]
    assert metrics["spec"].get("type", "ClusterIP") == "ClusterIP"
    assert not any(item["spec"].get("type") == "LoadBalancer" for item in installation if item["kind"] == "Service")
    bootstrap_deployment = objects["Deployment", "polyad-polyad"]
    assert bootstrap_deployment["spec"]["replicas"] == 2
    env = {entry["name"]: entry.get("value") for entry in bootstrap_deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POLYAD_AUTH_MODE"] == "Disabled"


def test_terraform_health_definition_registry_stays_current():
    """
    A newly introduced reusable kind must not be mistaken for an executing graph.
    """
    source = (TERRAFORM / "argocd.tf").read_text()
    definitions = re.search(r"definition_kinds\s*=\s*(\[[^\n]+\])", source)
    assert definitions is not None
    assert set(json.loads(definitions[1])) == DEFINITION_KINDS
    assert 'file("${path.module}/../integrations/argocd/health.lua")' in source


def test_every_operator_component_stays_on_dedicated_pool(installation):
    """
    Include controllers, generated Daemon templates and the cache's custom Pod spec.
    """
    checked = set()
    for obj in installation:
        kind = obj["kind"]
        if kind in ("Deployment", "StatefulSet", "DaemonSet", "Daemon", "Job"):
            spec = obj["spec"]["template"]["spec"]
        elif kind in ("Dragonfly", "Pod"):
            spec = obj["spec"]
        else:
            continue
        for container in spec.get("containers", []):
            if "polyad.operator.runtime" in container.get("command", []):
                assert container["resources"] == {
                    "requests": {"cpu": "1", "memory": "1Gi"},
                    "limits": {"cpu": "1", "memory": "1Gi"},
                }
        assert spec["nodeSelector"]["cloud.google.com/gke-nodepool"] == "polyad", obj["metadata"]["name"]
        assert {"key": "dedicated", "operator": "Equal", "value": "polyad", "effect": "NoSchedule"} in spec["tolerations"]
        checked.add(obj["metadata"]["name"])
    assert {
        "polyad-polyad",
        "polyad-executor",
        "polyad-gateway",
        "polyad-telemetry",
        "polyad-queue",
        "polyad-dragonfly-operator",
        "keda-operator",
        "keda-operator-metrics-apiserver",
        "keda-admission-webhooks",
    } <= checked


@pytest.mark.parametrize("automatic", [False, True])
def test_benchmark_application_inspects_fixtures_without_starting_load(automatic):
    """
    Track the fixture graph in the same UI without claiming shared CRDs or pulsing jobs.
    """
    objects = bootstrap(f"benchmarks.automatedSync={str(automatic).lower()}", "revision=test-run")
    application = next(obj for obj in objects if obj["metadata"]["name"] == "polyad-benchmarks")
    spec = application["spec"]
    assert spec["project"] == "polyad"
    assert spec["destination"]["namespace"] == "polyad"
    assert spec["source"]["targetRevision"] == "test-run"
    assert spec["source"]["path"] == "charts/polyad-benchmarks"
    helm = spec["source"]["helm"]
    assert helm["releaseName"] == "benchmarks"
    assert helm["skipCrds"] is True
    assert helm["skipTests"] is True
    assert ("automated" in spec["syncPolicy"]) is automatic
    assert not application["metadata"].get("finalizers")
    command = ["helm", "template", "benchmarks", str(ROOT / spec["source"]["path"]), "-n", "polyad", "--skip-tests"]
    for path in helm["valueFiles"]:
        command.extend(["-f", str(ROOT / spec["source"]["path"] / path)])
    rendered = list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))
    assert not any(obj["kind"] in {"Activation", "CustomResourceDefinition"} for obj in rendered)
    runner = next(obj for obj in rendered if obj["kind"] == "Workload" and obj["metadata"]["name"] == "load-runner")
    assert runner["spec"]["activation"]["mode"] == "Reject"
    assert runner["spec"]["template"]["spec"]["nodeSelector"]["cloud.google.com/gke-nodepool"] == "copolyad"


def test_benchmark_registration_is_optional_and_manual_by_default():
    """
    Registration does not immediately install fixtures or duplicate an existing administrator release.
    """
    application = next(obj for obj in bootstrap() if obj["metadata"]["name"] == "polyad-benchmarks")
    assert "automated" not in application["spec"]["syncPolicy"]
    assert not any(obj["metadata"]["name"] == "polyad-benchmarks" for obj in bootstrap("benchmarks.enabled=false"))


def test_flux_bootstrap_reuses_values_and_canonical_health() -> None:
    """
    Manage both charts from one read-only source with their existing health contract.
    """
    objects = bootstrap("controller=flux", namespace="flux-system")
    assert {obj["kind"] for obj in objects} == {"GitRepository", "HelmRelease"}
    assert all(obj["metadata"]["namespace"] == "flux-system" for obj in objects)
    source = next(obj["spec"] for obj in objects if obj["kind"] == "GitRepository")
    assert source["url"] == "https://github.com/astrivant/polyad.git"
    assert source["ref"] == {"branch": "main"} and source["interval"] == "1m"
    releases = {obj["metadata"]["name"]: obj["spec"] for obj in objects if obj["kind"] == "HelmRelease"}
    health = json.loads((TERRAFORM / "bootstrap/files/flux-health.json").read_text())["spec"]["healthCheckExprs"]

    # Each list retains chart defaults and translates chart-relative Argo paths.
    for name, spec in releases.items():
        assert spec["targetNamespace"] == spec["storageNamespace"] == "polyad"
        assert spec["releaseName"] == ("benchmarks" if name == "polyad-benchmarks" else "polyad")
        chart = spec["chart"]["spec"]
        assert chart["sourceRef"] == {"kind": "GitRepository", "name": "polyad"}
        assert chart["reconcileStrategy"] == "Revision"
        assert chart["ignoreMissingValuesFiles"] is False
        assert chart["valuesFiles"][0] == f"charts/{name}/values.yaml"
        assert all((ROOT / path).is_file() for path in chart["valuesFiles"])
        assert spec["healthCheckExprs"] == health
        assert spec["waitStrategy"] == {"name": "poller"}
        assert spec["test"]["enable"] is False
        assert spec["install"]["strategy"]["name"] == spec["upgrade"]["strategy"]["name"] == "RetryOnFailure"
        assert spec["install"]["serverSideApply"] is False
        assert spec["upgrade"]["serverSideApply"] == "disabled"
    assert releases["polyad"]["chart"]["spec"]["valuesFiles"] == ["charts/polyad/values.yaml", "terraform/polyad-values.yaml"]
    assert releases["polyad-benchmarks"]["chart"]["spec"]["valuesFiles"] == [
        "charts/polyad-benchmarks/values.yaml",
        "charts/polyad-benchmarks/values-smoke.yaml",
        "studies/load/fixtures/gke-values.yaml",
    ]
    assert releases["polyad"]["suspend"] is False and releases["polyad-benchmarks"]["suspend"] is True
    assert releases["polyad-benchmarks"]["dependsOn"] == [{"name": "polyad"}]
    for action in ("install", "upgrade"):
        assert releases["polyad"][action]["crds"] == "CreateReplace"
        assert releases["polyad-benchmarks"][action]["crds"] == "Skip"


def test_flux_drift_exclusions_match_argo_autoscaler_targets(installation: list[dict[str, Any]]) -> None:
    """
    Exclude only live replica counts, without disabling drift checks for whole resources.

    Args:
        installation (list[dict[str, Any]]): Rendered default Polyad installation.
    """
    argo = next(obj["spec"] for obj in bootstrap() if obj["kind"] == "Application" and obj["metadata"]["name"] == "polyad")
    flux = next(obj["spec"] for obj in bootstrap("controller=flux") if obj["kind"] == "HelmRelease" and obj["metadata"]["name"] == "polyad")
    drift = flux["driftDetection"]
    assert drift["mode"] == "enabled" and len(drift["ignore"]) == len(argo["ignoreDifferences"])
    identities = [{key: item[key] for key in ("group", "kind", "name", "namespace")} for item in argo["ignoreDifferences"]]

    # Regex selectors must match exactly, including literal dots in API groups.
    for item in drift["ignore"]:
        assert item["paths"] == ["/spec/replicas"]
        assert sum(all(re.fullmatch(item["target"][key], value) for key, value in identity.items()) for identity in identities) == 1
        assert item["target"]["namespace"] == "^polyad$"
    for resource in installation:
        if resource["kind"] not in {"HorizontalPodAutoscaler", "ScaledObject"}:
            continue
        target = resource["spec"]["scaleTargetRef"]
        assert any(
            re.fullmatch(item["target"]["name"], target["name"]) and re.fullmatch(item["target"]["kind"], target["kind"])
            for item in drift["ignore"]
        )


@pytest.mark.parametrize(("reference_type", "revision"), [("tag", "v0.0.1-alpha1"), ("commit", "a" * 40)])
def test_flux_revision_selection_and_freezing(reference_type: str, revision: str) -> None:
    """
    Pin sources independently of suspension and never create disabled benchmark releases.

    Args:
        reference_type (str): Flux source reference discriminator.
        revision (str): Tag or full Git commit to fetch.
    """
    objects = bootstrap(
        "controller=flux",
        f"flux.revisionType={reference_type}",
        f"revision={revision}",
        "flux.interval=5m",
        "automatedSync=false",
        "benchmarks.enabled=false",
        "values=operator: {image: {tag: experiment-42}}",
    )
    assert len(objects) == 2
    source = next(obj["spec"] for obj in objects if obj["kind"] == "GitRepository")
    release = next(obj["spec"] for obj in objects if obj["kind"] == "HelmRelease")
    assert source["ref"] == {reference_type: revision}
    assert source["interval"] == release["interval"] == "5m"
    assert release["suspend"] is True
    assert release["values"]["operator"]["image"]["tag"] == "experiment-42"


def test_flux_benchmarks_can_be_resumed_without_starting_load() -> None:
    """
    Enable only fixture reconciliation, preserving its dependency on a ready operator.
    """
    spec = next(
        obj["spec"]
        for obj in bootstrap("controller=flux", "benchmarks.automatedSync=true")
        if obj["metadata"]["name"] == "polyad-benchmarks"
    )
    assert spec["suspend"] is False and spec["dependsOn"] == [{"name": "polyad"}]
    assert spec["values"] == {} and spec["test"]["enable"] is False


@pytest.mark.parametrize(
    "settings",
    [
        ("controller=both",),
        ("flux.revisionType=unknown",),
        ("flux.revisionType=commit", "revision=abc123"),
        ("flux.interval=0m",),
        ("values=[not-a-mapping]",),
        ("valueFiles[0]=../../../outside.yaml",),
        ("valueFiles[0]=/outside.yaml",),
    ],
)
def test_flux_rejects_invalid_bootstrap_inputs(settings: tuple[str, ...]) -> None:
    """
    Reject ambiguous source configuration and values paths escaping the repository.

    Args:
        settings (tuple[str, ...]): Invalid Helm settings for this case.
    """
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap("controller=flux", *settings)
