"""
Verify dependency wiring, fresh-install APIs and cache availability modes.
"""

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
    """
    Render a release with its CRDs and selected values.
    """
    command = ["helm", "template", "test", str(CHART), "--namespace", "test", "--include-crds"]
    for setting in settings:
        command.extend(["--set-string" if setting.startswith("dragonfly.existingSecret=") else "--set", setting])
    return list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))


@pytest.mark.parametrize("ha,persistence", [(False, True), (True, True), (True, False)])
def test_managed_dragonfly_contract(ha, persistence):
    """
    Deploy a primary endpoint with the upstream API and real replication settings.
    """
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
    """
    Read the actual connection configuration used by Polyad.
    """
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    return next(item for item in operator["spec"]["template"]["spec"]["containers"][0]["env"] if item["name"] == "POLYAD_CACHE_URL")


@pytest.mark.parametrize("create,tls", [(False, False), (True, False), (True, True)])
def test_optional_gateway_routes_only_to_composition_service(create, tls):
    """
    Attach to a shared Gateway or create HTTP/HTTPS listeners with valid upstream schemas.
    """
    settings = ["api.enabled=true", "api.gateway.enabled=true", "api.gateway.hostnames[0]=polyad.example.com"]
    settings += (
        ["api.gateway.create=true", "api.gateway.className=example"]
        if create
        else ["api.gateway.name=shared", "api.gateway.namespace=edge"]
    )
    if tls:
        settings += ["api.gateway.tlsSecret=polyad-tls", "api.gateway.sectionName=https"]
    objects = render(*settings)
    route = next(obj for obj in objects if obj["kind"] == "HTTPRoute")
    parent = route["spec"]["parentRefs"][0]
    assert parent["name"] == ("test-polyad-api" if create else "shared")
    assert parent.get("namespace") == (None if create else "edge")
    assert route["spec"]["hostnames"] == ["polyad.example.com"]
    assert route["spec"]["rules"][0]["backendRefs"] == [{"name": "test-polyad-api", "port": 8090}]
    assert {match["path"]["value"] for match in route["spec"]["rules"][0]["matches"]} == {
        "/v1/compositions",
        "/v1/activations",
        "/openapi.json",
    }
    gateways = [obj for obj in objects if obj["kind"] == "Gateway"]
    assert len(gateways) == int(create)
    if create:
        listener = gateways[0]["spec"]["listeners"][0]
        assert listener["protocol"] == ("HTTPS" if tls else "HTTP")
        assert listener["port"] == (443 if tls else 80)
        assert listener["allowedRoutes"]["namespaces"]["from"] == "Same"
        if tls:
            assert listener["tls"]["certificateRefs"] == [{"kind": "Secret", "name": "polyad-tls"}]
    for obj in [route, *gateways]:
        schema = json.loads((CHART / "schemas" / f"{obj['kind'].lower()}-gateway-v1.json").read_text())
        jsonschema.Draft7Validator(schema).validate(obj)


def test_gateway_defaults_and_rate_limit_wiring():
    """
    Keep routing opt-in while passing the shared quota to every API replica.
    """
    assert not any(obj["kind"] in {"Gateway", "HTTPRoute"} for obj in render())
    objects = render("api.enabled=true", "api.rateLimit.requestsPerMinute=12")
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = {item["name"]: item.get("value") for item in operator["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POLYAD_API_REQUESTS_PER_MINUTE"] == "12"
    assert env["POLYAD_API_RATE_LIMIT_ENABLED"] == "true"


@pytest.mark.parametrize("change", ["disabled-api", "missing-gateway", "missing-class", "zero-quota"])
def test_gateway_and_rate_limit_values_reject_invalid_configuration(change):
    """
    Reject routing without its prerequisites and invalid request budgets during Helm validation.
    """
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    api = values["api"]
    api["enabled"] = True
    api["gateway"].update(enabled=True, name="edge")
    if change == "disabled-api":
        api["enabled"] = False
    elif change == "missing-gateway":
        api["gateway"]["name"] = ""
    elif change == "missing-class":
        api["gateway"].update(create=True, name="")
    else:
        api["rateLimit"]["requestsPerMinute"] = 0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(values, json.loads((CHART / "values.schema.json").read_text()))


def test_operator_can_use_a_separate_node_group():
    """
    Place operator replicas independently from workload graph placement rules.
    """
    objects = render(
        "operator.nodeSelector.pool=operators", "operator.tolerations[0].key=control", "operator.tolerations[0].operator=Exists"
    )
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    pod = operator["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"pool": "operators"}
    assert pod["tolerations"] == [{"key": "control", "operator": "Exists"}]


def test_admission_and_deadline_fields_survive_crd_pruning():
    """
    Keep delay observations structural and expose enforcement and storage validation rules.
    """
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
    """
    External URLs and Secrets disable both the cache and its controller.
    """
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
    """
    Reject non-HA replica counts and duplicate upstream CRD management.
    """
    result = subprocess.run(["helm", "template", "test", str(CHART), "--set", setting], capture_output=True, text=True)
    assert result.returncode != 0
    assert "schema" in result.stderr


def test_vendored_crd_matches_locked_dependency(tmp_path):
    """
    Detect upstream API drift whenever the dependency version changes.
    """
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
    """
    Expose the optional service with Secret auth while keeping policy writes administrator-only.
    """
    objects = render("api.enabled=true", "api.existingSecret=composition-token")
    service = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-api")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["targetPort"] == "composition"
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = operator["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "POLYAD_API_ENABLED", "value": "true"} in env
    assert next(item for item in env if item["name"] == "POLYAD_API_TOKEN_FILE")["value"] == "/var/run/polyad/api/token"
    assert operator["spec"]["template"]["spec"]["volumes"][0]["secret"]["secretName"] == "composition-token"
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


def test_optional_network_policies_and_mesh_auth_are_separate_from_workloads():
    """
    Render endpoint isolation, explicit service identities and Secret projections without credential write RBAC.
    """
    objects = render(
        "api.enabled=true",
        "events.enabled=true",
        "networkPolicy.enabled=true",
        "networkPolicy.apiServerCIDRs[0]=10.0.0.1/32",
        "networkPolicy.eventPeers[0].namespaceSelector.matchLabels.team=consumers",
        "mesh.enabled=true",
        "mesh.operator.enabled=true",
        "mesh.operator.eventPrincipals[0]=cluster.local/ns/consumers/sa/reader",
    )
    policy = next(obj for obj in objects if obj["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "polyad"
    assert policy["spec"]["ingress"][0]["ports"][0]["port"] == 8091
    auth = next(obj for obj in objects if obj["kind"] == "AuthorizationPolicy")
    assert auth["spec"]["rules"][1]["from"][0]["source"]["principals"] == ["cluster.local/ns/consumers/sa/reader"]
    assert auth["spec"]["rules"][1]["to"][0]["operation"]["methods"] == ["GET"]
    role = next(obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"] == "test-polyad")
    assert not any("secrets" in rule["resources"] for rule in role["rules"])
    assert not any("deployments" in rule["resources"] and "patch" in rule["verbs"] for rule in role["rules"])
    services = {obj["metadata"]["name"] for obj in objects if obj["kind"] == "Service"}
    assert {"test-polyad-api", "test-polyad-events"} <= services


def test_inline_keys_create_secrets_and_checksum_rollouts():
    """
    Inline credentials live in Secrets; existing credentials are projected without subPath.
    """
    objects = render("api.enabled=true", "api.existingSecret=", "api.key=test-secret")
    secret = next(obj for obj in objects if obj["kind"] == "Secret" and obj["metadata"]["name"] == "test-polyad-api")
    assert secret["stringData"] == {"token": "test-secret"}
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    assert deployment["spec"]["template"]["metadata"]["annotations"]["checksum/credentials"]
    mounts = deployment["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert mounts[0]["readOnly"] and "subPath" not in mounts[0]


def test_capacity_permissions_and_priority_are_opt_in():
    """
    Capacity plans receive bounded configuration and namespaced helper permissions.
    """
    default = render()
    assert not any(obj["kind"] == "PriorityClass" for obj in default)
    objects = render("capacity.enabled=true")
    priority = next(obj for obj in objects if obj["kind"] == "PriorityClass")
    assert priority["value"] == -5 and priority["preemptionPolicy"] == "Never"
    assert priority["metadata"]["name"] == "test-test-polyad-capacity"
    role = next(obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"] == "test-polyad")
    rule = next(rule for rule in role["rules"] if "provisioningrequests" in rule["resources"])
    assert rule["verbs"] == ["get", "list", "create", "delete"]
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = {entry["name"]: entry.get("value") for entry in operator["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POLYAD_CAPACITY_ENABLED"] == "true"
    assert env["POLYAD_CAPACITY_MAX_PODS"] == "128"
    existing = render("capacity.enabled=true", "capacity.priorityClass.create=false", "capacity.priorityClass.name=spare")
    assert not any(obj["kind"] == "PriorityClass" for obj in existing)


def test_capacity_schemas_come_from_public_models():
    """
    Graphs, Feedback epochs and rewrites expose matching capacity policy schemas.
    """
    from polyad.compiler.asts import CapacityStatus
    from polyad.compiler.schema import structural_schema
    from polyad.graph import CapacityPlan

    for kind in ("graphs", "polygraphs", "ephemeralgraphs", "feedbacks", "rewrites"):
        crd = yaml.safe_load((CHART / "crds" / f"{kind}.yaml").read_text())
        props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]
        spec = props["spec"]["properties"]
        if kind in {"feedbacks", "rewrites"}:
            spec = spec["graph" if kind == "feedbacks" else "topology"]["properties"]
        assert spec["capacity"] == structural_schema(CapacityPlan)
        if kind != "rewrites":
            assert props["status"]["properties"]["capacity"] == structural_schema(CapacityStatus)


def test_optional_metrics_service_and_access_policies():
    """
    Match monitoring traffic to a dedicated port under both network and mesh isolation.
    """
    assert not any(obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-metrics" for obj in render())
    objects = render(
        "metrics.enabled=true",
        "metrics.graphLabels=true",
        "networkPolicy.enabled=true",
        "networkPolicy.apiServerCIDRs[0]=10.0.0.1/32",
        "networkPolicy.metricsPeers[0].namespaceSelector.matchLabels.name=monitoring",
        "mesh.enabled=true",
        "mesh.operator.enabled=true",
        "mesh.operator.metricsPrincipals[0]=cluster.local/ns/monitoring/sa/prometheus",
    )
    service = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-metrics")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"] == [{"name": "metrics", "port": 8092, "targetPort": "metrics"}]
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert {"name": "metrics", "containerPort": 8092} in container["ports"]
    assert {"name": "POLYAD_METRICS_GRAPH_LABELS", "value": "true"} in container["env"]
    network = next(obj for obj in objects if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "test-polyad")
    assert any(rule["ports"] == [{"protocol": "TCP", "port": 8092}] for rule in network["spec"]["ingress"])
    mesh = next(obj for obj in objects if obj["kind"] == "AuthorizationPolicy")
    rule = next(rule for rule in mesh["spec"]["rules"] if rule["to"][0]["operation"]["ports"] == ["8092"])
    assert rule["from"][0]["source"]["principals"] == ["cluster.local/ns/monitoring/sa/prometheus"]
    assert rule["to"][0]["operation"]["paths"] == ["/metrics", "/v1/metrics", "/openapi.json"]
