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

CHART = Path(__file__).resolve().parents[2] / "charts" / "polyad"
EAST_WEST_SETTINGS = (
    "mesh.enabled=true",
    "mesh.multicluster.enabled=true",
    "mesh.multicluster.eastWest.enabled=true",
    "global.meshID=shared",
    "global.network=east-network",
    "global.multiCluster.clusterName=east",
    "istioEastWest.networkGateway=east-network",
)
pytestmark = pytest.mark.skipif(shutil.which("helm") is None, reason="requires Helm and helm dependency build charts/polyad")


def render(*settings, values_files=()):
    """
    Render a release with values fixtures from pkg/tests/data followed by explicit overrides.
    """
    command = ["helm", "template", "test", str(CHART), "--namespace", "test", "--include-crds"]
    for filename in values_files:
        command.extend(["--values", str(Path(__file__).parent / "data" / filename)])
    for setting in settings:
        flag = "--set-string" if setting.startswith(("dragonfly.existingSecret=", "istioEastWest.labels.")) else "--set"
        if setting.startswith(("operator.tuning.", "tracing.samplingRatio=", "events.pollIntervalSeconds=")):
            flag = "--set-json"
        command.extend([flag, setting])
    return list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))


def test_websocket_chart_connects_values_runtime_gateway_and_policy():
    """
    The optional transport uses the existing events port and exposes only its enabled route.
    """
    objects = render(
        "mesh.enabled=true",
        "mesh.operator.enabled=true",
        "mesh.ingress.enabled=true",
        "mesh.ingress.hosts[0]=polyad.example",
        "mesh.ingress.tlsSecret=gateway-tls",
        "mesh.operator.eventPrincipals[0]=cluster.local/ns/test/sa/reader",
        values_files=(CHART / "values-websockets.reference.yaml",),
    )
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POLYAD_EVENTS_WEBSOCKETS_ENABLED"] == "true"
    service = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-events")
    assert [port["port"] for port in service["spec"]["ports"]] == [8091]
    routes = next(obj for obj in objects if obj["kind"] == "VirtualService")["spec"]["http"]
    assert any(match.get("uri", {}).get("exact") == "/v1/events/ws" for route in routes for match in route["match"])
    policies = [obj for obj in objects if obj["kind"] == "AuthorizationPolicy"]
    assert any(
        "/v1/events/ws" in target["operation"].get("paths", [])
        for obj in policies
        for rule in obj["spec"]["rules"]
        for target in rule.get("to", [])
    )
    schema = json.loads((CHART / "values.schema.json").read_text())
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    defaults["events"]["websockets"]["enabled"] = True
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(defaults, schema)
    defaults["events"]["enabled"] = True
    defaults["events"]["websockets"]["enabled"] = "true"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(defaults, schema)


def test_operator_autoscaling_behavior_and_runtime_tuning():
    """
    Preserve scaling defaults while passing explicit zero and rate overrides through Helm.
    """
    objects = render("ha=true", "operator.autoscaling.enabled=true")
    hpa = next(obj for obj in objects if obj["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["metrics"] == [
        {"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}
    ]
    assert hpa["spec"]["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 300
    assert hpa["spec"]["behavior"]["scaleUp"]["stabilizationWindowSeconds"] == 0
    objects = render(
        "ha=true",
        "operator.autoscaling.enabled=true",
        "operator.autoscaling.behavior.scaleUp.stabilizationWindowSeconds=45",
        "operator.autoscaling.behavior.scaleDown.stabilizationWindowSeconds=0",
        "operator.autoscaling.behavior.scaleDown.selectPolicy=Min",
        "operator.autoscaling.behavior.scaleDown.policies[0].type=Pods",
        "operator.autoscaling.behavior.scaleDown.policies[0].value=1",
        "operator.autoscaling.behavior.scaleDown.policies[0].periodSeconds=60",
        "operator.tuning.rescanIntervalSeconds=12",
        "operator.tuning.consumeIntervalSeconds=0.25",
        "operator.tuning.metricsIntervalSeconds=2",
        "operator.tuning.backlogIntervalSeconds=3",
    )
    hpa = next(obj for obj in objects if obj["kind"] == "HorizontalPodAutoscaler")
    assert hpa["spec"]["behavior"]["scaleUp"]["stabilizationWindowSeconds"] == 45
    assert hpa["spec"]["behavior"]["scaleDown"] == {
        "stabilizationWindowSeconds": 0,
        "selectPolicy": "Min",
        "policies": [{"type": "Pods", "value": 1, "periodSeconds": 60}],
    }
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    assert "replicas" not in deployment["spec"]
    env = {item["name"]: item.get("value") for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    for name, value in (("RESCAN", "12"), ("CONSUME", "0.25"), ("METRICS", "2"), ("BACKLOG", "3")):
        assert env[f"POLYAD_{name}_INTERVAL_SECONDS"] == value


@pytest.mark.parametrize("mode", ["Dense", "Distributed"])
def test_operator_hpa_scales_dense_or_bootstrap_with_cpu_and_memory(mode):
    """
    Add memory demand to the same HPA without targeting graph-managed components.
    """
    objects = render(
        "ha=true",
        "operator.autoscaling.enabled=true",
        "operator.autoscaling.targetCPUUtilizationPercentage=65",
        "operator.autoscaling.targetMemoryUtilizationPercentage=80",
        "operator.resources.requests.memory=256Mi",
        f"architecture.mode={mode}",
        "api.enabled=true",
        "metrics.enabled=true",
    )
    hpas = [obj for obj in objects if obj["kind"] == "HorizontalPodAutoscaler"]
    assert len(hpas) == 1
    spec = hpas[0]["spec"]
    assert spec["scaleTargetRef"] == {"apiVersion": "apps/v1", "kind": "Deployment", "name": "test-polyad"}
    assert spec["minReplicas"] == 2
    assert spec["maxReplicas"] == 8
    assert spec["metrics"] == [
        {"type": "Resource", "resource": {"name": name, "target": {"type": "Utilization", "averageUtilization": target}}}
        for name, target in [("cpu", 65), ("memory", 80)]
    ]
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    assert "replicas" not in deployment["spec"]
    pod = deployment["spec"]["template"]
    assert pod["metadata"]["labels"]["polyad.astrivant.com/component"] == ("dense" if mode == "Dense" else "bootstrap")
    assert pod["spec"]["containers"][0]["resources"]["requests"]["memory"] == "256Mi"


@pytest.mark.parametrize(("enabled", "memory_target"), [("false", "80"), ("true", "null")])
def test_inactive_memory_autoscaling_does_not_require_memory_requests(enabled, memory_target):
    """
    Allow memory requests to be omitted unless memory scaling is actually active.
    """
    objects = render(
        f"ha={enabled}",
        f"operator.autoscaling.enabled={enabled}",
        f"operator.autoscaling.targetMemoryUtilizationPercentage={memory_target}",
        "operator.resources.requests.memory=null",
    )
    hpas = [obj for obj in objects if obj["kind"] == "HorizontalPodAutoscaler"]
    if enabled == "false":
        assert not hpas
    else:
        assert [metric["resource"]["name"] for metric in hpas[0]["spec"]["metrics"]] == ["cpu"]


def test_memory_autoscaling_requires_memory_requests():
    """
    Reject a memory-utilization HPA without the request needed for its denominator.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(
            "ha=true",
            "operator.autoscaling.enabled=true",
            "operator.autoscaling.targetMemoryUtilizationPercentage=80",
            "operator.resources.requests.memory=null",
        )


@pytest.mark.parametrize(
    "setting",
    [
        "operator.autoscaling.behavior.scaleDown.stabilizationWindowSeconds=-1",
        "operator.autoscaling.behavior.scaleUp.stabilizationWindowSeconds=3601",
        "operator.autoscaling.behavior.scaleUp.selectPolicy=Fast",
        "operator.autoscaling.behavior.scaleDown.policies[0].periodSeconds=1801",
        "operator.autoscaling.behavior.scaleDown.policies[0].value=0",
        "operator.autoscaling.targetMemoryUtilizationPercentage=0",
        "operator.autoscaling.targetMemoryUtilizationPercentage=101",
        "operator.autoscaling.targetMemoryUtilizationPercentage=true",
        "operator.autoscaling.targetMemoryUtilizationPercentage=80Mi",
        "operator.tuning.consumeIntervalSeconds=0",
        "operator.tuning.rescanIntervalSeconds=16",
    ],
)
def test_invalid_operator_performance_settings_fail(setting):
    """
    Reject unusable autoscaler policies and worker periods during chart rendering.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting)


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


@pytest.mark.parametrize("distributed", [False, True])
def test_ha_dragonfly_keda_targets_a_scalable_pool(distributed):
    """
    Dense and split operators expose a real scale interface and the primary metric route.
    """
    settings = ("ha=true", "architecture.mode=Distributed", "api.enabled=true") if distributed else ()
    objects = render("dragonfly.ha.enabled=true", "metrics.authentication.enabled=true", "keda.authentication.enabled=true", *settings)
    pool = next(obj for obj in objects if obj["kind"] == "DragonflyPool")
    crd = next(obj for obj in objects if obj["kind"] == "CustomResourceDefinition" and obj["spec"]["names"]["kind"] == "DragonflyPool")
    version = crd["spec"]["versions"][0]
    jsonschema.Draft7Validator(version["schema"]["openAPIV3Schema"]).validate(pool)
    assert version["subresources"]["scale"] == {
        "specReplicasPath": ".spec.replicas",
        "statusReplicasPath": ".status.replicas",
        "labelSelectorPath": ".status.labelSelector",
    }
    scaler = next(obj for obj in objects if obj["kind"] == "ScaledObject" and obj["metadata"]["name"] == "test-queue")["spec"]
    assert scaler["scaleTargetRef"] == {"apiVersion": "polyad.astrivant.com/v1alpha1", "kind": "DragonflyPool", "name": "test-queue"}
    assert (scaler["minReplicaCount"], scaler["maxReplicaCount"]) == (2, 5)
    trigger = scaler["triggers"][0]
    assert trigger["metricType"] == "AverageValue"
    assert trigger["metadata"]["url"].endswith("/v1/dragonfly/connections")
    assert trigger["metadata"]["targetValue"] == "50"
    assert trigger["metadata"]["authMode"] == "bearer"
    assert trigger["authenticationRef"]["name"] == "test-polyad-metrics"
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = operator["spec"]["template"]["spec"]["containers"][0]["env"]
    assert {"name": "POLYAD_DRAGONFLY_POOL", "value": "test-queue"} in env
    assert {"name": "POLYAD_METRICS_ENABLED", "value": "true"} in env
    metrics = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-metrics")
    assert metrics["spec"]["selector"]["polyad.astrivant.com/component"] == ("telemetry" if distributed else "dense")
    role = next(obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"] == "test-polyad")
    rules = [rule for rule in role["rules"] if rule["apiGroups"] == ["dragonflydb.io"]]
    assert rules == [
        {"apiGroups": ["dragonflydb.io"], "resources": ["dragonflies"], "resourceNames": ["test-queue"], "verbs": ["get", "patch"]}
    ]


@pytest.mark.parametrize(
    "settings", [(), ("dragonfly.enabled=false",), ("dragonfly.ha.enabled=true", "dragonfly.autoscaling.enabled=false")]
)
def test_fixed_or_external_dragonfly_has_no_scaler(settings):
    """
    Single instances, external caches and explicitly fixed HA do not require KEDA.
    """
    objects = render(*settings)
    assert not any(obj["kind"] in {"DragonflyPool", "ScaledObject"} for obj in objects)


@pytest.mark.parametrize(
    "setting",
    [
        "dragonfly.autoscaling.minReplicas=1",
        "dragonfly.autoscaling.maxReplicas=10",
        "dragonfly.autoscaling.connectionsPerReplica=0",
        "dragonfly.autoscaling.minReplicas=6",
        "dragonfly.ha.replicas=6",
        "dragonfly.existingSecret=another-cache",
        "metrics.authentication.enabled=true",
    ],
)
def test_invalid_dragonfly_scaling_rejected(setting):
    """
    Reject unsafe floors, inconsistent bounds and metrics aimed at an unrelated cache.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render("dragonfly.ha.enabled=true", setting)


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
        "/v1/throughput",
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
    for kind in ("Graph", "PolyGraph"):
        deadline = schemas[kind]["properties"]["status"]["properties"]["delays"]["additionalProperties"]
        assert set(deadline["required"]) == {"token", "notBefore"}
        assert deadline["properties"]["token"]["x-kubernetes-preserve-unknown-fields"] is True
    for kind in ("Workload", "Daemon"):
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
    dependency = next(
        entry for entry in yaml.safe_load((CHART / "Chart.lock").read_text())["dependencies"] if entry["name"] == "dragonfly-operator"
    )
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
    assert {"name": "POLYAD_WORKLOAD_API_URL", "value": "http://test-polyad-api.test.svc:8090"} in env
    assert {"name": "POLYAD_WORKLOAD_EVENTS_URL", "value": ""} in env
    assert {"name": "POLYAD_WORKLOAD_METRICS_URL", "value": ""} in env
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
    for kind in ["Graph", "PolyGraph"]:
        spec = schemas[kind]["properties"]["spec"]["properties"]
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
        "mesh.ingress.enabled=true",
        "mesh.ingress.hosts[0]=polyad.example.com",
        "mesh.ingress.tlsSecret=ingress-cert",
    )
    policy = next(obj for obj in objects if obj["kind"] == "NetworkPolicy")
    assert policy["spec"]["podSelector"]["matchLabels"]["app.kubernetes.io/name"] == "polyad"
    assert policy["spec"]["ingress"][0]["ports"][0]["port"] == 8091
    auth = next(obj for obj in objects if obj["kind"] == "AuthorizationPolicy")
    assert auth["spec"]["rules"][1]["from"][0]["source"]["principals"] == ["cluster.local/ns/consumers/sa/reader"]
    assert auth["spec"]["rules"][1]["to"][0]["operation"]["methods"] == ["GET"]
    assert {"/v1/graphs/*", "/v1/discovery"} <= set(auth["spec"]["rules"][1]["to"][0]["operation"]["paths"])
    gateway = next(obj for obj in objects if obj["kind"] == "VirtualService")
    event_route = gateway["spec"]["http"][0]
    assert {"uri": {"prefix": "/v1/graphs/"}} in event_route["match"]
    assert event_route["route"][0]["destination"]["port"]["number"] == 8091
    role = next(obj for obj in objects if obj["kind"] == "Role" and obj["metadata"]["name"] == "test-polyad")
    assert not any("secrets" in rule["resources"] for rule in role["rules"])
    assert not any("deployments" in rule["resources"] and "patch" in rule["verbs"] for rule in role["rules"])
    services = {obj["metadata"]["name"] for obj in objects if obj["kind"] == "Service"}
    assert {"test-polyad-api", "test-polyad-events"} <= services


def test_multicluster_gateways_and_optional_observers():
    """
    Render distinct gateways and read-only observers without changing the default installation.
    """
    default = render()
    assert not any("observer" in item["metadata"]["name"] or "eastwest" in item["metadata"]["name"] for item in default)
    objects = render(
        *EAST_WEST_SETTINGS,
        "mesh.ingress.enabled=true",
        "mesh.ingress.hosts[0]=api.example.com",
        "mesh.ingress.tlsSecret=api-cert",
        "api.enabled=true",
        "observer.enabled=true",
        "observer.existingSecret=read-token",
        "observer.mesh=true",
        "observer.principals[0]=cluster.local/ns/control/sa/reader",
        "networkPolicy.enabled=true",
        "networkPolicy.apiServerCIDRs[0]=10.0.0.1/32",
        "observer.peers[0].podSelector.matchLabels.istio=polyad-eastwest",
    )
    identities = [(item["apiVersion"], item["kind"], item["metadata"]["name"]) for item in objects]
    assert len(identities) == len(set(identities))
    gateway = next(item for item in objects if item["kind"] == "Gateway" and item["metadata"]["name"].endswith("eastwest"))
    assert gateway["spec"]["servers"][0]["tls"] == {"mode": "AUTO_PASSTHROUGH"}
    assert gateway["spec"]["servers"][0]["port"] == {"number": 15443, "name": "tls", "protocol": "TLS"}
    schema = json.loads((CHART / "schemas/gateway-networking-v1.json").read_text())
    jsonschema.Draft7Validator(schema).validate(gateway)
    deployment = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "polyad-eastwest")
    assert gateway["spec"]["selector"].items() <= deployment["spec"]["template"]["metadata"]["labels"].items()
    service = next(item for item in objects if item["kind"] == "Service" and item["metadata"]["name"] == "polyad-eastwest")
    assert {port["port"] for port in service["spec"]["ports"]} == {15012, 15017, 15021, 15443}
    observer = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "test-polyad-observer")
    assert observer["spec"]["template"]["spec"]["containers"][0]["command"] == [
        "/usr/bin/tini",
        "--",
        "python",
        "-m",
        "polyad.operator.observer",
    ]
    role = next(item for item in objects if item["kind"] == "Role" and item["metadata"]["name"] == "test-polyad-observer")
    assert all(set(rule["verbs"]) <= {"get", "list"} for rule in role["rules"])
    assert not any("secrets" in rule["resources"] for rule in role["rules"])
    assert not any(item["kind"] == "Secret" and "observer" in item["metadata"]["name"] for item in objects)


@pytest.mark.parametrize("port,target_port", [(15443, 16443), (16443, 16443), (443, 15443)])
def test_east_west_custom_listener_matches_service_and_discovery(port, target_port):
    """
    Keep custom gateway listeners, Service translation and Istio discovery consistent.
    """
    objects = render(
        *EAST_WEST_SETTINGS,
        f"istioEastWest.networkGatewayPorts.tls.port={port}",
        f"istioEastWest.networkGatewayPorts.tls.targetPort={target_port}",
        rf"istioEastWest.labels.networking\.istio\.io/gatewayPort={port}",
        "mesh.multicluster.eastWest.portName=tls-services",
        "mesh.multicluster.eastWest.hosts[0]=*.svc.corp.example",
        "mesh.multicluster.peers[0].name=west",
        "mesh.multicluster.peers[0].mode=Gateway",
        "mesh.multicluster.peers[0].cidrs[0]=192.0.2.20/32",
        "mesh.multicluster.peers[0].gatewayPort=26443",
    )
    gateway = next(item for item in objects if item["kind"] == "Gateway" and item["metadata"]["name"].endswith("eastwest"))
    assert gateway["spec"]["servers"] == [
        {
            "port": {"number": port, "name": "tls-services", "protocol": "TLS"},
            "tls": {"mode": "AUTO_PASSTHROUGH"},
            "hosts": ["*.svc.corp.example"],
        }
    ]
    schema = json.loads((CHART / "schemas/gateway-networking-v1.json").read_text())
    jsonschema.Draft7Validator(schema).validate(gateway)
    service = next(item for item in objects if item["kind"] == "Service" and item["metadata"]["name"] == "polyad-eastwest")
    assert service["metadata"]["labels"]["networking.istio.io/gatewayPort"] == str(port)
    assert next(item for item in service["spec"]["ports"] if item["name"] == "tls") == {
        "name": "tls",
        "port": port,
        "targetPort": target_port,
        "protocol": "TCP",
    }
    operator = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "test-polyad")
    env = {entry["name"]: entry.get("value") for entry in operator["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert json.loads(env["POLYAD_MESH_PEERS"])[0]["gatewayPort"] == 26443


@pytest.mark.parametrize(
    "setting",
    [
        "istioEastWest.networkGatewayPorts.tls.port=16443",
        r"istioEastWest.labels.networking\.istio\.io/gatewayPort=16443",
        "istioEastWest.networkGatewayPorts.tls.port=0",
        "istioEastWest.networkGatewayPorts.tls.port=65536",
        "istioEastWest.networkGatewayPorts.tls.targetPort=0",
        "istioEastWest.networkGatewayPorts.tls.targetPort=65536",
        "istioEastWest.networkGatewayPorts.tls.protocol=UDP",
        "mesh.multicluster.eastWest.portName=Invalid_Name",
        "mesh.multicluster.eastWest.portName=",
        "mesh.multicluster.eastWest.hosts=[]",
    ],
)
def test_east_west_invalid_listener_settings_fail(setting):
    """
    Reject invalid listeners and discovery mismatches before installing the gateway.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(*EAST_WEST_SETTINGS, setting)


def test_federation_mounts_scoped_credentials_without_secret_read_permissions():
    """
    Project remote credentials and provide writable memory for embedded TLS certificates.
    """
    objects = render(
        "federation.enabled=true",
        "global.multiCluster.clusterName=east",
        "federation.clusters[0].name=west",
        "federation.clusters[0].namespace=workflows",
        "federation.clusters[0].kubeconfigSecret=west-credentials",
    )
    deployment = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "test-polyad")
    pod = deployment["spec"]["template"]["spec"]
    env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
    assert json.loads(env["POLYAD_FEDERATION_CLUSTERS"])[0]["namespace"] == "workflows"
    assert pod["securityContext"]["fsGroup"] == 65532
    assert next(volume for volume in pod["volumes"] if volume["name"] == "remote-transport")["emptyDir"]["medium"] == "Memory"
    mount = next(item for item in pod["containers"][0]["volumeMounts"] if item["name"] == "cluster-west")
    assert mount["readOnly"] and "subPath" not in mount


@pytest.mark.parametrize(
    "setting",
    ["mesh.multicluster.enabled=true", "mesh.multicluster.eastWest.enabled=true", "observer.enabled=true", "federation.enabled=true"],
)
def test_remote_features_fail_closed_without_configuration(setting):
    """
    Require explicit identities and credentials when optional cross-cluster features are enabled.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting)


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
    assert rule["verbs"] == ["get", "list", "watch", "create", "delete"]
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = {entry["name"]: entry.get("value") for entry in operator["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POLYAD_CAPACITY_ENABLED"] == "true"
    assert env["POLYAD_CAPACITY_MAX_PODS"] == "128"
    existing = render("capacity.enabled=true", "capacity.priorityClass.create=false", "capacity.priorityClass.name=spare")
    assert not any(obj["kind"] == "PriorityClass" for obj in existing)


def test_capacity_schemas_come_from_public_models():
    """
    Graphs and rewrites expose matching capacity policy schemas.
    """
    from polyad.compiler.passes.schema import structural_schema
    from polyad.graph import CapacityPlan
    from polyad_types.resources import CapacityStatus

    for kind in ("graphs", "polygraphs", "rewrites"):
        crd = yaml.safe_load((CHART / "crds" / f"{kind}.yaml").read_text())
        props = crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]
        spec = props["spec"]["properties"]
        if kind == "rewrites":
            spec = spec["topology"]["properties"]
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
    assert {"name": "POLYAD_WORKLOAD_METRICS_URL", "value": "http://test-polyad-metrics.test.svc:8092"} in container["env"]
    network = next(obj for obj in objects if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "test-polyad")
    assert any(rule["ports"] == [{"protocol": "TCP", "port": 8092}] for rule in network["spec"]["ingress"])
    mesh = next(obj for obj in objects if obj["kind"] == "AuthorizationPolicy")
    rule = next(rule for rule in mesh["spec"]["rules"] if rule["to"][0]["operation"]["ports"] == ["8092"])
    assert rule["from"][0]["source"]["principals"] == ["cluster.local/ns/monitoring/sa/prometheus"]
    assert rule["to"][0]["operation"]["paths"] == [
        "/metrics",
        "/v1/metrics",
        "/v1/workloads/*",
        "/v1/components/*",
        "/v1/postgresql/connections",
        "/v1/dragonfly/connections",
        "/openapi.json",
    ]


def test_keda_bearer_secret_mount_and_optional_resources():
    """
    Share a dedicated Secret between the metrics listener and reusable KEDA authentication.
    """
    assert not any(item["kind"] in {"TriggerAuthentication", "ExternalSecret"} for item in render())
    objects = render(
        "metrics.enabled=true",
        "metrics.authentication.enabled=true",
        "metrics.authentication.existingSecret=autoscaler-token",
        "metrics.authentication.secretKey=access",
        "keda.authentication.enabled=true",
    )
    auth = next(item for item in objects if item["kind"] == "TriggerAuthentication")
    schema = json.loads((CHART / "schemas/triggerauthentication-keda-v1alpha1.json").read_text())
    jsonschema.Draft7Validator(schema).validate(auth)
    assert auth["metadata"]["namespace"] == "test"
    assert auth["spec"]["secretTargetRef"] == [{"parameter": "token", "name": "autoscaler-token", "key": "access"}]
    pod = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == "test-polyad")["spec"]["template"][
        "spec"
    ]
    volume = next(item for item in pod["volumes"] if item["name"] == "metrics-credentials")
    assert volume["secret"] == {"secretName": "autoscaler-token", "items": [{"key": "access", "path": "token"}]}
    assert {"name": "POLYAD_METRICS_AUTH_ENABLED", "value": "true"} in pod["containers"][0]["env"]
    assert {"name": "POLYAD_METRICS_TOKEN_FILE", "value": "/var/run/polyad/metrics/token"} in pod["containers"][0]["env"]
    assert not any(item["kind"] == "Secret" and item["metadata"]["name"] == "autoscaler-token" for item in objects)
    inline = render(
        "metrics.enabled=true",
        "metrics.authentication.enabled=true",
        "metrics.authentication.existingSecret=",
        "metrics.authentication.key=test-metrics-token",
        "keda.authentication.enabled=true",
    )
    assert next(item for item in inline if item["kind"] == "Secret")["stringData"] == {"token": "test-metrics-token"}
    assert (
        next(item for item in inline if item["kind"] == "TriggerAuthentication")["spec"]["secretTargetRef"][0]["name"]
        == "test-polyad-metrics"
    )


@pytest.mark.parametrize("reload", [False, True])
def test_eso_generates_references_for_endpoint_and_cache_secrets(tmp_path, reload):
    """
    Render provider references without including credentials or duplicating target Secrets.
    """
    config = {
        "metrics": {"enabled": True, "authentication": {"enabled": True}},
        "keda": {"authentication": {"enabled": True}},
        "api": {"enabled": True},
        "events": {"enabled": True},
        "dragonfly": {"enabled": False, "existingSecret": "polyad-cache"},
        "externalSecrets": {
            "enabled": True,
            "reloadOnChange": reload,
            "secretStoreRef": {"name": "vault", "kind": "ClusterSecretStore"},
            "secrets": [
                {
                    "name": f"polyad-{endpoint}",
                    "data": [{"secretKey": key, "remoteRef": {"key": f"production/polyad/{endpoint}", "property": key}}],
                }
                for endpoint, key in (("metrics", "token"), ("api", "token"), ("events", "token"), ("cache", "url"))
            ],
        },
    }
    path = tmp_path / "values.yaml"
    path.write_text(yaml.safe_dump(config))

    def render_config():
        return list(
            filter(
                None,
                yaml.safe_load_all(
                    subprocess.check_output(["helm", "template", "test", str(CHART), "--namespace", "test", "-f", str(path)], text=True)
                ),
            )
        )

    objects = render_config()
    external = [item for item in objects if item["kind"] == "ExternalSecret"]
    assert len(external) == 4
    assert not any(item["kind"] == "Secret" for item in objects)
    for item in external:
        schema = json.loads((CHART / "schemas/externalsecret-external-secrets-v1.json").read_text())
        jsonschema.Draft7Validator(schema).validate(item)
        assert item["apiVersion"] == "external-secrets.io/v1"
        assert item["spec"]["secretStoreRef"] == {"name": "vault", "kind": "ClusterSecretStore"}
        assert item["spec"]["target"]["name"] == item["metadata"]["name"]
        assert "remoteRef" in item["spec"]["data"][0]
        target_annotations = item["spec"]["target"].get("template", {}).get("metadata", {}).get("annotations", {})
        assert target_annotations == ({"reloader.stakater.com/match": "true"} if reload else {})
    pod = next(item for item in objects if item["kind"] == "Deployment")["spec"]["template"]["spec"]
    assert {"name": "POLYAD_CACHE_URL_FILE", "value": "/var/run/polyad/cache/url"} in pod["containers"][0]["env"]
    assert next(item for item in pod["volumes"] if item["name"] == "cache-credentials")["secret"]["secretName"] == "polyad-cache"
    config["externalSecrets"]["secrets"].append(config["externalSecrets"]["secrets"][0])
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(subprocess.CalledProcessError):
        render_config()
    config["externalSecrets"]["secrets"].pop()
    config["externalSecrets"]["secrets"][0]["data"].append(config["externalSecrets"]["secrets"][0]["data"][0])
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(subprocess.CalledProcessError):
        render_config()
    config["externalSecrets"]["secrets"][0]["data"].pop()
    config["externalSecrets"]["secrets"][0]["name"] = "test-polyad-metrics"
    config["metrics"]["authentication"].update(existingSecret="", key="test-only-inline-token")
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(subprocess.CalledProcessError):
        render_config()


@pytest.mark.parametrize("eso,reload", [(False, False), (False, True), (True, False), (True, True)])
def test_secret_reload_annotations_require_eso_and_opt_in(eso, reload):
    """
    Annotate controller metadata and enable Daemon support only with both chart opt-ins.
    """
    objects = render(
        f"externalSecrets.enabled={str(eso).lower()}",
        f"externalSecrets.reloadOnChange={str(reload).lower()}",
        "externalSecrets.secretStoreRef.name=vault",
        "externalSecrets.secrets[0].name=observer-token",
        "externalSecrets.secrets[0].data[0].secretKey=token",
        "externalSecrets.secrets[0].data[0].remoteRef.key=polyad/observer",
        "observer.enabled=true",
        "observer.existingSecret=observer-token",
        "global.multiCluster.clusterName=east",
    )
    expected = "true" if eso and reload else None
    for name in ("test-polyad", "test-polyad-observer"):
        workload = next(item for item in objects if item["kind"] == "Deployment" and item["metadata"]["name"] == name)
        assert workload["metadata"].get("annotations", {}).get("reloader.stakater.com/search") == expected
        assert "reloader.stakater.com/search" not in workload["spec"]["template"]["metadata"].get("annotations", {})
        if name == "test-polyad":
            env = {entry["name"]: entry.get("value") for entry in workload["spec"]["template"]["spec"]["containers"][0]["env"]}
            assert env["POLYAD_ESO_RELOAD_ENABLED"] == (expected or "false")


@pytest.mark.parametrize(
    "settings",
    [
        ["keda.authentication.enabled=true"],
        ["metrics.authentication.enabled=true"],
        ["metrics.enabled=true", "metrics.authentication.enabled=true", "metrics.authentication.key=conflict"],
        ["metrics.enabled=true", "metrics.authentication.enabled=true", "metrics.authentication.existingSecret="],
        ["externalSecrets.enabled=true"],
    ],
)
def test_invalid_endpoint_authentication_configuration_fails(settings):
    """
    Refuse authentication without credentials, a listener or an external store reference.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(*settings)


@pytest.mark.parametrize("scope,namespace", [("Cluster", ""), ("OperatorNamespace", ""), ("Namespace", "chosen")])
def test_optional_connection_api_scope_authentication_and_transport(scope, namespace):
    """
    Render scoped transport and intake permissions with separate authentication review rights.
    """
    defaults = render()
    assert not any(obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-connections" for obj in defaults)
    assert not any("connection-" in obj["metadata"]["name"] for obj in defaults)
    objects = render(
        "connections.enabled=true",
        f"connections.scope={scope}",
        f"connections.namespace={namespace}",
        "networkPolicy.enabled=true",
        "networkPolicy.apiServerCIDRs[0]=10.0.0.1/32",
        "mesh.enabled=true",
        "mesh.operator.enabled=true",
        "mesh.operator.connectionPrincipals[0]=cluster.local/ns/test/sa/worker",
    )
    service = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-connections")
    assert service["spec"]["type"] == "ClusterIP"
    assert service["spec"]["ports"][0]["port"] == 8093
    operator = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    container = operator["spec"]["template"]["spec"]["containers"][0]
    env = {entry["name"]: entry.get("value") for entry in container["env"]}
    assert env["POLYAD_CONNECTIONS_SCOPE"] == scope
    assert env["POLYAD_CONNECTIONS_MAX_TTL"] == "3600"
    assert env["POLYAD_WORKLOAD_CONNECTIONS_URL"] == "http://test-polyad-connections.test.svc:8093"
    review = next(obj for obj in objects if obj["kind"] == "ClusterRole" and obj["metadata"]["name"].endswith("connection-reviews"))
    assert review["rules"] == [
        {"apiGroups": ["authentication.k8s.io"], "resources": ["tokenreviews"], "verbs": ["create"]},
        {"apiGroups": ["authorization.k8s.io"], "resources": ["subjectaccessreviews"], "verbs": ["create"]},
    ]
    intake = [obj for obj in objects if obj["kind"] in {"Role", "ClusterRole"} and obj["metadata"]["name"].endswith("connection-intake")]
    if scope == "OperatorNamespace":
        assert not intake
    else:
        assert intake[0]["kind"] == ("ClusterRole" if scope == "Cluster" else "Role")
        assert intake[0]["metadata"].get("namespace") == ("chosen" if scope == "Namespace" else None)
        rules = {resource: rule for rule in intake[0]["rules"] for resource in rule["resources"]}
        assert rules["temporaryconnections"]["verbs"] == ["get", "create", "patch"]
        for resource in (
            "graphs",
            "polygraphs",
            "replicagroups",
            "pods",
            "serviceaccounts",
            "replicasets",
            "deployments",
            "statefulsets",
            "daemonsets",
            "jobs",
        ):
            assert rules[resource]["verbs"] == ["get"]
    network = next(obj for obj in objects if obj["kind"] == "NetworkPolicy" and obj["metadata"]["name"] == "test-polyad")
    rule = next(rule for rule in network["spec"]["ingress"] if rule["ports"] == [{"protocol": "TCP", "port": 8093}])
    assert rule["from"][0]["namespaceSelector"] == (
        {} if scope == "Cluster" else {"matchLabels": {"kubernetes.io/metadata.name": "test" if scope == "OperatorNamespace" else "chosen"}}
    )
    mesh = next(obj for obj in objects if obj["kind"] == "AuthorizationPolicy")
    rule = next(rule for rule in mesh["spec"]["rules"] if rule["to"][0]["operation"]["ports"] == ["8093"])
    assert rule["to"][0]["operation"]["methods"] == ["GET", "POST", "DELETE"]


@pytest.mark.parametrize(
    "setting",
    [
        "connections.scope=Other",
        "connections.scope=Namespace",
        "connections.namespace=unexpected",
        "connections.maxTtlSeconds=0",
        "connections.maxTtlSeconds=86401",
        "connections.retentionSeconds=-1",
    ],
)
def test_invalid_connection_settings_fail_rendering(setting):
    """
    Reject ambiguous namespace selection and unbounded TTL or retention values.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting)


def test_root_control_plane_requires_reachable_credentials_and_exposes_scale_crds():
    """
    Root mode is explicit, passes worker bootstrap inputs and supplies KEDA scale endpoints.
    """
    settings = (
        "ha=true",
        "rootControlPlane.enabled=true",
        "rootControlPlane.kubeconfigSecret=root-access",
        "federation.enabled=true",
        "global.multiCluster.clusterName=management",
        "federation.clusters[0].name=west",
        "federation.clusters[0].namespace=workloads",
        "federation.clusters[0].kubeconfigSecret=west-access",
        "dragonfly.existingSecret=root-cache",
        "metrics.enabled=true",
    )
    objects = render(*settings)
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    env = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["POLYAD_ROOT_ENABLED"]["value"] == "true"
    assert env["POLYAD_ROOT_DEPLOYMENT"]["value"] == "test-polyad"
    assert any(item.get("secret", {}).get("secretName") == "root-access" for item in deployment["spec"]["template"]["spec"]["volumes"])
    for kind in ("OperatorPool", "RemoteScale"):
        crd = next(obj for obj in objects if obj["kind"] == "CustomResourceDefinition" and obj["spec"]["names"]["kind"] == kind)
        version = crd["spec"]["versions"][0]
        assert version["subresources"]["scale"]["specReplicasPath"] == ".spec.replicas"
        spec = {"cluster": "west", "replicas": 2}
        if kind == "RemoteScale":
            spec["target"] = {"name": "consumers", "uid": "exact-uid", "generation": 2}
        jsonschema.Draft7Validator(version["schema"]["openAPIV3Schema"]).validate({"spec": spec})
    for invalid in ("rootControlPlane.kubeconfigSecret=", "federation.enabled=false", "dragonfly.existingSecret=", "metrics.enabled=false"):
        with pytest.raises(subprocess.CalledProcessError):
            render(*settings, invalid)


@pytest.mark.parametrize("ha", [False, True])
def test_optional_postgresql_persists_state_and_scales_from_operator_connections(ha):
    """
    Deploy durable storage with a single primary or HA and target the CNPG scale subresource.
    """
    objects = render("postgresql.enabled=true", f"postgresql.ha.enabled={str(ha).lower()}", "metrics.enabled=true")
    cluster = next(obj for obj in objects if obj["kind"] == "Cluster")
    assert cluster["spec"]["instances"] == (3 if ha else 1)
    assert ("synchronous" in cluster["spec"]["postgresql"]) == ha
    assert cluster["spec"]["storage"]["size"] == "10Gi"
    assert cluster["spec"]["bootstrap"]["initdb"]["postInitApplicationSQL"] == [
        'REVOKE ALL ON DATABASE "polyad" FROM PUBLIC;',
        "REVOKE ALL ON SCHEMA public FROM PUBLIC;",
    ]
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    pod = deployment["spec"]["template"]["spec"]
    env = {entry["name"]: entry.get("value") for entry in pod["containers"][0]["env"]}
    assert env["POLYAD_POSTGRES_DSN_FILE"] == "/var/run/polyad/postgresql/uri"
    volume = next(volume for volume in pod["volumes"] if volume["name"] == "postgres-credentials")
    assert volume["secret"]["secretName"] == "test-state-app"
    scaled = render("postgresql.enabled=true", "postgresql.autoscaling.enabled=true", "metrics.enabled=true")
    target = next(obj for obj in scaled if obj["kind"] == "ScaledObject")
    assert target["spec"]["scaleTargetRef"] == {"apiVersion": "postgresql.cnpg.io/v1", "kind": "Cluster", "name": "test-state"}
    assert target["spec"]["minReplicaCount"] >= 1
    assert target["spec"]["triggers"][0]["metricType"] == "AverageValue"
    assert target["spec"]["triggers"][0]["metadata"]["url"].endswith("/v1/postgresql/connections")


def test_postgresql_default_off_and_external_connection_secret():
    """
    Keep PostgreSQL optional in both architectures and allow administrator-managed databases.
    """
    assert not any(obj["apiVersion"] == "postgresql.cnpg.io/v1" for obj in render())
    objects = render("postgresql.enabled=true", "postgresql.managed=false", "postgresql.existingSecret=state-access")
    assert not any(obj["kind"] == "Cluster" for obj in objects)
    deployment = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    volume = next(volume for volume in deployment["spec"]["template"]["spec"]["volumes"] if volume["name"] == "postgres-credentials")
    assert volume["secret"]["secretName"] == "state-access"


@pytest.mark.parametrize(
    "settings",
    [
        ["postgresql.autoscaling.enabled=true"],
        ["postgresql.enabled=true", "postgresql.managed=false"],
        ["postgresql.enabled=true", "postgresql.autoscaling.enabled=true"],
        [
            "postgresql.enabled=true",
            "postgresql.ha.enabled=true",
            "postgresql.autoscaling.enabled=true",
            "postgresql.autoscaling.minInstances=1",
            "metrics.enabled=true",
        ],
        ["architecture.mode=Distributed"],
        ["ha=true", "architecture.mode=Distributed", "metrics.enabled=true"],
    ],
)
def test_invalid_state_and_component_configurations_fail_before_install(settings):
    """
    Reject absent storage credentials, unsafe replica floors and unservable split deployments.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(*settings)


def test_distributed_components_form_a_real_constrained_graph_without_postgresql():
    """
    Compose executable Daemons, independently scaled groups and correctly routed stable Services.
    """
    objects = render(
        "ha=true", "architecture.mode=Distributed", "architecture.autoscaling=true", "api.enabled=true", "metrics.enabled=true"
    )
    assert not any(obj["kind"] == "Cluster" for obj in objects)
    graph = next(obj for obj in objects if obj["kind"] == "Graph")
    assert graph["spec"]["rules"] == ["test-control-plane"]
    assert len(graph["spec"]["nodes"]) == 3
    assert len(graph["spec"]["connections"]) == 2
    assert next(obj for obj in objects if obj["kind"] == "GraphRule")["spec"]["cheeger"] == {"minimum": 1}
    groups = [obj for obj in objects if obj["kind"] == "ReplicaGroup"]
    assert len(groups) == 3 and all(obj["spec"]["templateOnly"] for obj in groups)
    scaled = [obj for obj in objects if obj["kind"] == "ScaledObject"]
    assert {obj["spec"]["scaleTargetRef"]["name"] for obj in scaled} == {obj["metadata"]["name"] for obj in groups}
    daemons = [obj for obj in objects if obj["kind"] == "Daemon"]
    for daemon in daemons:
        role = daemon["metadata"]["name"].removeprefix("test-")
        pod = daemon["spec"]["template"]
        assert daemon["spec"]["replicas"] == 1
        assert pod["metadata"]["labels"]["polyad.astrivant.com/component"] == role
        assert "polyad.astrivant.com/bootstrap" not in pod["metadata"]["labels"]
        env = {entry["name"]: entry.get("value") for entry in pod["spec"]["containers"][0]["env"]}
        assert env["POLYAD_COMPONENT"] == role
        assert env["POLYAD_SELF_GRAPH"] == graph["metadata"]["name"]
    for service in (
        obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] in {"test-polyad-api", "test-polyad-metrics"}
    ):
        expected = "gateway" if service["metadata"]["name"].endswith("-api") else "telemetry"
        assert service["spec"]["selector"]["polyad.astrivant.com/component"] == expected
    schemas = {
        obj["spec"]["names"]["kind"]: obj["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        for obj in objects
        if obj["kind"] == "CustomResourceDefinition"
    }
    for obj in objects:
        if obj["kind"] in {"Daemon", "GraphRule", "Graph", "ReplicaGroup"}:
            assert obj["metadata"]["labels"]["polyad.astrivant.com/internal"] == "true"
            jsonschema.Draft7Validator(schemas[obj["kind"]]).validate(obj)


@pytest.mark.parametrize("profile", [None, "values-ha.reference.yaml", "values-components.reference.yaml", "values-worker.reference.yaml"])
def test_cheeger_ceilings_reach_every_executor_profile(profile):
    """
    Dense, HA, component and administrator-installed worker containers use the same ceilings.
    """
    objects = render(
        "operator.cheeger.maxVertices=22",
        "operator.cheeger.maxCuts=2097151",
        "operator.cheeger.timeoutSeconds=15",
        *(("federation.clusters[0].namespace=test",) if profile == "values-worker.reference.yaml" else ()),
        values_files=(CHART / profile,) if profile else (),
    )
    pods = [obj["spec"]["template"] for obj in objects if obj["kind"] in {"Deployment", "Daemon"} and "template" in obj["spec"]]
    operators = [container for pod in pods for container in pod["spec"]["containers"] if container["name"] == "operator"]
    assert operators
    for container in operators:
        env = {item["name"]: item.get("value") for item in container["env"]}
        assert env["POLYAD_CHEEGER_MAX_VERTICES"] == "22"
        assert env["POLYAD_CHEEGER_MAX_CUTS"] == "2097151"
        assert env["POLYAD_CHEEGER_TIMEOUT_SECONDS"] == "15"


def test_component_graph_accepts_the_optional_cheeger_maximum():
    """
    Render both inclusive bounds into the policy enforcing the existing component chain.
    """
    from polyad.graph import StructuralRule, evaluate_rule
    from polyad_types.codec import converter
    from polyad_types.topology import topology

    objects = render("architecture.cheegerMaximum=1", values_files=(CHART / "values-components.reference.yaml",))
    rule = next(obj for obj in objects if obj["kind"] == "GraphRule")
    graph = next(obj for obj in objects if obj["kind"] == "Graph")
    assert rule["spec"]["cheeger"] == {"minimum": 1, "maximum": 1}
    verdict = evaluate_rule(
        converter.structure(rule["spec"], StructuralRule), topology(graph["spec"], "Graph"), expanded_nodes=9, nesting_depth=2
    )
    assert verdict["allowed"] and verdict["measurements"]["cheeger"] == 1
