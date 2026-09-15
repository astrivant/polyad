"""Verify dependency wiring, fresh-install APIs and cache availability modes."""

from __future__ import annotations

import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import jsonschema
import pytest
import yaml

CHART = Path(__file__).resolve().parents[1] / "charts" / "polyad"
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and helm dependency build charts/polyad")


def render(*settings):
    """Render a release with its CRDs and selected values."""
    command = ["helm", "template", "test", str(CHART), "--namespace", "test", "--include-crds"]
    for setting in settings:
        command.extend(["--set-string" if setting.startswith("dragonfly.existingSecret=") else "--set", setting])
    return list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))


@pytest.mark.parametrize("ha,persistence", [(False, True), (True, True), (True, False)])
def test_managed_dragonfly_contract(ha, persistence):
    """Deploy a primary endpoint with the upstream API and real replication settings."""
    objects = render(f"dragonfly.ha.enabled={str(ha).lower()}", f"dragonfly.persistence.enabled={str(persistence).lower()}")
    cache = next(obj for obj in objects if obj["kind"] == "Dragonfly")
    assert cache["metadata"]["name"] == "test-queue"
    assert cache["spec"]["replicas"] == (2 if ha else 1)
    assert ("snapshot" in cache["spec"]) is persistence
    if ha:
        assert cache["spec"]["enableReplicationReadinessGate"] is True
        assert cache["spec"]["pdb"] == {"maxUnavailable": 1}
        rule = cache["spec"]["affinity"]["podAntiAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"][0]
        assert rule == {"labelSelector": {"matchLabels": {"app": "test-queue"}}, "topologyKey": "kubernetes.io/hostname"}
    else:
        assert "affinity" not in cache["spec"]
    crds = [obj for obj in objects if obj["kind"] == "CustomResourceDefinition" and obj["metadata"]["name"] == "dragonflies.dragonflydb.io"]
    assert len(crds) == 1  # Registered in crds/, never duplicated in templates/.
    assert objects.index(crds[0]) < objects.index(cache)
    schema = crds[0]["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    jsonschema.Draft7Validator(schema).validate(cache)
    assert not any(obj["kind"] == "StatefulSet" for obj in objects)  # Owned by the upstream controller.
    controller = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-dragonfly-operator")
    assert controller["spec"]["replicas"] == 2
    manager = next(c for c in controller["spec"]["template"]["spec"]["containers"] if c["name"] == "manager")
    assert "--leader-elect" in manager["args"]
    assert cache_url(objects) == {"name": "POLYAD_CACHE_URL", "value": "redis://test-queue:6379/0"}


def cache_url(objects):
    """Read the actual connection configuration used by Polyad."""
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    return next(item for item in operator["spec"]["template"]["spec"]["containers"][0]["env"] if item["name"] == "POLYAD_CACHE_URL")


def test_operator_can_use_a_separate_node_group():
    """Place operator replicas independently from workload graph placement rules."""
    objects = render("nodeSelector.pool=operators", "tolerations[0].key=control", "tolerations[0].operator=Exists")
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    pod = operator["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"pool": "operators"}
    assert pod["tolerations"] == [{"key": "control", "operator": "Exists"}]


def test_admission_and_deadline_fields_survive_crd_pruning():
    """Keep delay observations structural and expose enforcement and storage validation rules."""
    objects = render()
    schemas = {
        obj["spec"]["names"]["kind"]: obj["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        for obj in objects
        if obj["kind"] == "CustomResourceDefinition" and obj["spec"]["group"] == "polyad.astrivant.com"
    }
    for kind in ("Graph", "PolyGraph", "EphemeralGraph", "Feedback"):
        deadline = schemas[kind]["properties"]["status"]["properties"]["delays"]["additionalProperties"]
        assert set(deadline["required"]) == {"token", "notBefore"}
        assert deadline["properties"]["token"]["x-kubernetes-preserve-unknown-fields"] is True
    for kind in ("Workload", "Daemon", "Ephemeral"):
        spec = schemas[kind]["properties"]["spec"]["properties"]
        assert spec["placement"]["properties"]["enforce"]["default"] is True
        assert spec["persistence"]["x-kubernetes-validations"]
    assert schemas["Gate"]["properties"]["spec"]["x-kubernetes-validations"]


def test_external_cache_disables_dependency():
    """External URLs and Secrets disable both the cache and its controller."""
    objects = render("dragonfly.enabled=false", "dragonfly.externalUrl=rediss://cache.example:6379/2")
    assert not any(obj["kind"] == "Dragonfly" for obj in objects)
    assert [obj["metadata"]["name"] for obj in objects if obj["kind"] == "Deployment"] == ["test-polyad"]
    assert cache_url(objects)["value"] == "rediss://cache.example:6379/2"
    objects = render("dragonfly.enabled=false", "dragonfly.existingSecret=cache-url", "dragonfly.externalUrl=")
    assert cache_url(objects)["valueFrom"] == {"secretKeyRef": {"name": "cache-url", "key": "url"}}
    objects = render("dragonfly.enabled=false", "dragonfly.existingSecret=123")
    assert cache_url(objects)["valueFrom"]["secretKeyRef"]["name"] == "123"


@pytest.mark.parametrize(
    "setting",
    [
        "dragonfly.ha.replicas=1",
        "dragonfly.ha.topologyKey=",
        "dragonflyOperator.crds.install=true",
        "dragonflyOperator.manager.extraArgs.bad=value",
        "dragonflyOperator.rbacProxy.extraArgs.bad=value",
    ],
)
def test_invalid_cache_configuration_rejected(setting):
    """Reject non-HA replica counts and duplicate upstream CRD management."""
    result = subprocess.run(["helm", "template", "test", str(CHART), "--set", setting], capture_output=True, text=True)
    assert result.returncode != 0
    assert "schema" in result.stderr


def test_vendored_crd_matches_locked_dependency(tmp_path):
    """Detect upstream API drift whenever the dependency version changes."""
    dependency = yaml.safe_load((CHART / "Chart.lock").read_text())["dependencies"][0]
    archive = CHART / "charts" / f"dragonfly-operator-{dependency['version']}.tgz"
    with tarfile.open(archive) as package:
        package.extractall(tmp_path, filter="data")
    rendered = subprocess.check_output(
        ["helm", "template", "upstream", str(tmp_path / "dragonfly-operator"), "--show-only", "templates/crds.yaml"], text=True
    )
    assert yaml.safe_load(rendered) == yaml.safe_load((CHART / "crds" / "dragonflies.yaml").read_text())
    schema = yaml.safe_load(rendered)["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
    assert json.loads((CHART / "schemas" / "dragonfly-dragonflydb-v1alpha1.json").read_text()) == schema


def test_composition_service_and_policy_rbac():
    """Expose the optional service with Secret auth while keeping policy writes administrator-only."""
    objects = render("api.enabled=true", "api.existingSecret=composition-token")
    service = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-api")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["targetPort"] == "composition"
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = operator["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "POLYAD_API_ENABLED", "value": "true"} in env
    assert next(item for item in env if item["name"] == "POLYAD_API_TOKEN")["valueFrom"]["secretKeyRef"] == {
        "name": "composition-token",
        "key": "token",
    }
    role = next(obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"] == "test-polyad")
    policy = [rule for rule in role["rules"] if "graphrules" in rule["resources"]]
    assert len(policy) == 1 and set(policy[0]["verbs"]) == {"get", "list", "watch"}
    assert not any(obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-api" for obj in render())
    schemas = {
        obj["spec"]["names"]["kind"]: obj["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        for obj in objects
        if obj["kind"] == "CustomResourceDefinition"
    }
    assert schemas["Composition"]["properties"]["spec"]["x-kubernetes-validations"][0]["rule"] == "self == oldSelf"
    for kind in ["Graph", "PolyGraph", "EphemeralGraph", "Feedback"]:
        spec = schemas[kind]["properties"]["spec"]["properties"]
        if kind == "Feedback":
            spec = spec["graph"]["properties"]
        assert spec["rules"]["x-kubernetes-list-type"] == "set"
        assert spec["nodes"]["items"]["properties"]["id"]["maxLength"] == 63
