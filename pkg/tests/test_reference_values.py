"""
Validate administrator overlays, authentication storage placement and explicit demonstration defaults.
"""

from __future__ import annotations

import json
import runpy
import subprocess

import jsonschema
import pytest
import yaml
from referencing import Registry, Resource

from tests.test_chart import CHART, render

VALUES_CHECK = runpy.run_path(str(CHART.parents[1] / "scripts/validation/check-values.py"))


def test_all_shipped_values_have_valid_types_and_schema_coverage():
    """
    Check defaults and examples as well as reference files without relying on permissive objects.
    """
    paths = VALUES_CHECK["value_files"]()
    assert CHART / "values.yaml" in paths
    assert not list(CHART.glob("values-*.reference.yaml"))
    assert list((CHART / "references").glob("values-*.reference.yaml"))
    assert CHART.parents[1] / "examples/postgresql/operator-values.yaml" in paths
    for path in paths:
        VALUES_CHECK["validate"](path)


def test_istio_reference_values_cover_deployment_features_and_traffic_strategies():
    """
    Keep each customer-facing Istio choice discoverable in a focused, composable overlay.
    """
    bundled = yaml.safe_load((CHART / "references" / "values-istio-bundled.reference.yaml").read_text())
    existing = yaml.safe_load((CHART / "references" / "values-istio-existing.reference.yaml").read_text())
    features = yaml.safe_load((CHART / "references" / "values-istio-features.reference.yaml").read_text())
    traffic_path = CHART / "references" / "values-istio-traffic.reference.yaml"
    traffic = yaml.safe_load(traffic_path.read_text())

    assert bundled["mesh"]["install"] is True
    assert existing["mesh"]["install"] is False
    assert existing["mesh"]["ingress"]["gatewayAPI"]["enabled"] is True
    assert features["mesh"]["telemetry"]["enabled"] is True
    assert features["mesh"]["sidecar"]["enabled"] is True
    assert features["mesh"]["authorization"]["audit"]["enabled"] is True
    assert features["mesh"]["authorization"]["dryRunDeny"]["enabled"] is True
    assert features["mesh"]["egress"]["gateway"]["enabled"] is True
    assert traffic["mesh"]["multicluster"]["routing"]["mode"] == "LocalFirst"
    assert traffic["events"]["istio"]["loadBalancer"] == "LEAST_REQUEST"
    assert "spec.traffic[].resilience" in traffic_path.read_text()


def test_reference_overlays_only_use_configuration_exposed_by_values_yaml():
    """
    Keep values.yaml canonical: references may select or illustrate settings, never define a separate API.
    """
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    missing = []

    def require_keys(reference, canonical, path=""):
        if not isinstance(reference, dict) or not isinstance(canonical, dict):
            return
        for key, value in reference.items():
            location = f"{path}.{key}" if path else key
            if key not in canonical:
                missing.append(location)
                continue

            # Empty mappings and arrays are intentionally expanded by the typed
            # reference examples; their entry shapes remain canonical in the schema.
            if canonical[key]:
                require_keys(value, canonical[key], location)

    for reference_path in (CHART / "references").glob("values-*.reference.yaml"):
        require_keys(yaml.safe_load(reference_path.read_text()), defaults)
    assert not missing, f"settings present only in reference values files: {', '.join(sorted(missing))}"


@pytest.mark.parametrize(
    "path",
    [path for path in VALUES_CHECK["value_files"]() if "examples" in path.parts and path.name != "operator-values.yaml"],
    ids=lambda path: str(path.relative_to(CHART.parents[1])),
)
def test_example_values_render_with_the_full_helm_schema(path):
    """
    Merge example overlays with chart and dependency defaults in their documented namespace.
    """
    namespace, bases = "polyad", []
    if path.parent.name == "helm-workers":
        if path.name == "root-values.yaml":
            bases = [CHART.parents[1] / "examples/root-control-plane/values.yaml"]
        else:
            namespace, bases = "workloads", [CHART / "references" / "values-worker.reference.yaml"]
    command = ["helm", "template", "test", str(CHART), "--namespace", namespace]
    for values in [*bases, path]:
        command += ["-f", str(values)]
    output = subprocess.check_output(command, text=True)
    assert any(obj and obj["kind"] == "Deployment" for obj in yaml.safe_load_all(output))


@pytest.mark.parametrize(
    "value",
    [
        {"ha": "true"},
        {"architecture": {"cheegerMaximum": "1"}},
        {"architecture": {"cheegerMaximum": True}},
        {"architecture": {"cheegerMaximum": 0.5}},
        {"operator": {"cheeger": {"maxVertices": True}}},
        {"operator": {"cheeger": {"maxCuts": 0}}},
        {"operator": {"cheeger": {"timeoutSeconds": "5"}}},
        {"observer": {"resources": {"requests": {"cpu": 1}}}},
        {"observer": {"resources": {"requests": "100m"}}},
        {"postgresql": {"resources": {"limits": {"memory": True}}}},
        {"observer": {"peers": [{"podSelector": {"matchLabels": {"app": True}}}]}},
        {"observer": {"peers": [{"namespaceSelector": {"matchExpressions": [{"key": "team", "operator": "In", "values": "ops"}]}}]}},
        {"rootControlPlane": {"pools": [{"name": "west", "cluster": "west", "replicas": 1, "tolerations": [{"tolerationSeconds": "30"}]}]}},
        {"rootControlPlane": {"pools": [{"name": "west", "cluster": "west", "replicas": 1, "resources": {"requests": {"cpu": []}}}]}},
        {"networkPolicy": {"extraEgress": [{"ports": [{"port": True}]}]}},
        {"networkPolicy": {"extraEgress": [{"ports": [{"protocol": "https"}]}]}},
        {"istioEastWestGateway": {"labels": {"networking.istio.io/gatewayPort": 15443}}},
        {"istioIngress": {"tolerations": [False]}},
        {"istiod": {"env": {"ENABLE_NATIVE_SIDECARS": True}}},
        {"istiod": {"meshConfig": {"enableAutoMtls": "true"}}},
    ],
)
def test_nested_value_types_are_rejected_by_both_schemas(value):
    """
    Invalid administrator overlays fail editor validation and the full Helm schema.
    """
    schema = json.loads((CHART / "values.schema.json").read_text())
    overlay = json.loads((CHART / "values.reference.schema.json").read_text())
    registry = Registry().with_resource("values.schema.json", Resource.from_contents(schema))
    assert not jsonschema.Draft7Validator(overlay, registry=registry).is_valid(value)

    # Validate the supplied top-level subtree against the canonical types without
    # unrelated missing defaults obscuring the type failure under examination.
    for key, subtree in value.items():
        validator = jsonschema.Draft7Validator(schema).evolve(schema=schema["properties"][key])
        assert not validator.is_valid(subtree)


def test_quoted_quantities_and_named_network_ports_remain_valid():
    """
    Stronger typing accepts whole cores, decimal CPU, extended resources and named ports.
    """
    schema = json.loads((CHART / "values.schema.json").read_text())
    overlay = json.loads((CHART / "values.reference.schema.json").read_text())
    registry = Registry().with_resource("values.schema.json", Resource.from_contents(schema))
    jsonschema.Draft7Validator(overlay, registry=registry).validate(
        {
            "observer": {"resources": {"requests": {"cpu": "0.5", "memory": "128Mi"}, "limits": {"cpu": "1", "example.com/device": "1"}}},
            "postgresql": {"resources": {"requests": {"cpu": "250m", "ephemeral-storage": "1Gi"}}},
            "networkPolicy": {"extraEgress": [{"ports": [{"port": "https", "protocol": "TCP"}, {"port": 5432}]}]},
        }
    )


def test_values_checker_rejects_duplicate_keys_and_untyped_nested_values():
    """
    A schema accepting arbitrary objects must not silently count as type coverage.
    """
    with pytest.raises(ValueError, match="duplicate YAML key"):
        yaml.load("ha: true\nha: false\n", Loader=VALUES_CHECK["UniqueLoader"])
    schema = {"type": "object", "properties": {"resources": {"type": "object"}}}
    assert list(VALUES_CHECK["type_gaps"]({"resources": {"cpu": True}}, schema, schema)) == ["$.resources.cpu"]


def test_parameter_annotations_use_schema_types_without_changing_values():
    """
    Correct stale and absent tags, preserve descriptions and make repairs idempotent.
    """
    from deepdiff import DeepDiff

    schema = json.loads((CHART / "values.schema.json").read_text())
    source = (
        "## @param global [string] Preserve this explanation.\n"
        "global:\n"
        "  ## @param global.meshID [boolean] An administrator's mesh identity.\n"
        "  meshID: mesh-one\n"
        "ha: false\n"
        "operator:\n"
        "  writeQueue:\n"
        "    maxInFlight: 2\n"
        "    validationIntervalSeconds: 0.5\n"
    )
    annotate = VALUES_CHECK["annotated_values"]
    result = annotate(source, schema)
    assert "## @param global [object] Preserve this explanation." in result
    assert "## @param global.meshID [string] An administrator's mesh identity." in result
    assert "## @param ha [boolean]" in result
    assert "## @param operator.writeQueue.maxInFlight [integer]" in result
    assert "## @param operator.writeQueue.validationIntervalSeconds [number]" in result
    assert not DeepDiff(yaml.safe_load(source), yaml.safe_load(result))
    assert annotate(result, schema) == result


def test_nullable_and_union_annotations_describe_permitted_types():
    """
    Document accepted types rather than inferring a field's contract from its current default.
    """
    schema = {
        "type": "object",
        "properties": {"limit": {"type": ["integer", "null"]}, "port": {"anyOf": [{"type": "integer"}, {"type": "string"}]}},
    }
    result = VALUES_CHECK["annotated_values"]("limit: null\nport: http\n", schema)
    assert "@param limit [integer, nullable]" in result
    assert "@param port [integer, string]" in result


def test_generated_parameter_docs_show_types_and_actual_defaults():
    """
    Guard against the upstream string and collection modifiers replacing published defaults.
    """
    source = (CHART / "README.md").read_text()
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    for name, kind, value in (
        ("global.meshID", "string", values["global"]["meshID"] or '""'),
        ("operator.image.pullPolicy", "string", values["operator"]["image"]["pullPolicy"]),
        ("operator.replicaCount", "integer or null", json.dumps(values["operator"]["replicaCount"])),
        ("ha", "boolean", "false"),
    ):
        row = next(line for line in source.splitlines() if line.startswith(f"| `{name}` "))
        assert f"**Type: {kind}.**" in row
        assert row.rstrip().endswith(f"`{value}` |")


def test_nested_list_parameter_types_and_duplicate_annotations():
    """
    Validate every repeated list-entry annotation and reject obsolete field documentation.
    """
    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                },
            }
        },
    }
    source = (
        "## @param items [array] Repeated entries.\nitems:\n"
        "  ## @param items[].count [string] First entry.\n  - count: 1\n"
        "  ## @param items[].count [boolean] Second entry.\n  - count: 2\n"
    )
    annotate = VALUES_CHECK["annotated_values"]
    updated = annotate(source, schema)
    assert updated.count("items[].count [integer]") == 2
    assert annotate(updated, schema) == updated
    with pytest.raises(ValueError, match="without a typed value"):
        annotate(source + "## @param missing [string] Obsolete.\n", schema)
    with pytest.raises(ValueError, match="duplicate"):
        annotate("## @param items [array] Duplicate.\n" + source, schema)


@pytest.mark.parametrize("path", sorted((CHART / "references").glob("values-*.reference.yaml")), ids=lambda path: path.name)
def test_reference_overlays_are_typed_and_render_independently(path):
    """
    Editor validation accepts partial overrides while Helm validates the complete merged configuration.
    """
    source = path.read_text()
    assert source.startswith("# yaml-language-server: $schema=../values.reference.schema.json")
    schema = json.loads((CHART / "values.schema.json").read_text())
    overlay = json.loads((CHART / "values.reference.schema.json").read_text())
    registry = Registry().with_resource("values.schema.json", Resource.from_contents(schema))
    validator = jsonschema.Draft7Validator(overlay, registry=registry)
    validator.validate(yaml.safe_load(source))
    assert not validator.is_valid({"ha": "true"})
    assert not validator.is_valid({"authentication": {"storage": {"enabled": "true"}}})
    namespace = "workloads" if path.name == "values-worker.reference.yaml" else "default"
    objects = yaml.safe_load_all(
        subprocess.check_output(["helm", "template", "test", str(CHART), "--namespace", namespace, "-f", str(path)], text=True)
    )
    assert any(obj and obj["kind"] == "Deployment" for obj in objects)


def test_demo_removes_credential_volumes_and_authentication_quotas():
    """
    A public demonstration can render without pre-created endpoint credentials.
    """
    objects = render(
        "authentication.mode=Disabled",
        "api.enabled=true",
        "events.enabled=true",
        "metrics.enabled=true",
        "api.existingSecret=",
        "events.existingSecret=",
    )
    pod = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")["spec"]["template"][
        "spec"
    ]
    env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
    assert env["POLYAD_AUTH_MODE"] == "Disabled"
    assert env["POLYAD_API_RATE_LIMIT_ENABLED"] == "false"
    assert not any(name in env for name in ("POLYAD_API_TOKEN_FILE", "POLYAD_EVENTS_TOKEN_FILE", "POLYAD_METRICS_TOKEN_FILE"))
    assert not any(volume["name"] in {"api-token", "events-token", "metrics-token"} for volume in pod.get("volumes", []))


@pytest.mark.parametrize("storage", ["managed", "external", "shared"])
def test_authentication_database_placement_and_credentials(storage):
    """
    Provision the dedicated login and volume, or mount only the explicitly selected DSN Secret.
    """
    settings = ["authentication.storage.enabled=true"]
    if storage == "external":
        settings += ["authentication.storage.managed=false", "authentication.storage.existingSecret=external-auth"]
    if storage == "shared":
        settings += ["authentication.storage.separateDatabase=false", "postgresql.enabled=true"]
    command = ["helm", "template", "test", str(CHART), "-f", str(CHART / "references" / "values-authentication.reference.yaml")]
    for value in settings:
        command += ["--set", value]
    objects = list(filter(None, yaml.safe_load_all(subprocess.check_output(command, text=True))))
    clusters = [obj for obj in objects if obj["kind"] == "Cluster"]
    if storage == "managed":
        bootstrap = clusters[0]["spec"]["bootstrap"]["initdb"]
        assert bootstrap["database"] == "polyad-authentication"
        assert bootstrap["owner"] == "polyad_authentication"
        assert 'REVOKE ALL ON DATABASE "polyad-authentication" FROM PUBLIC;' in bootstrap["postInitApplicationSQL"]
    elif storage == "external":
        assert not clusters
    pod = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")["spec"]["template"][
        "spec"
    ]
    volume = next(item for item in pod["volumes"] if item["name"] == "authentication-database")
    assert (
        volume["secret"]["secretName"]
        == {"managed": "test-authentication-app", "external": "external-auth", "shared": "test-state-app"}[storage]
    )


def test_daemonset_pool_cannot_request_a_replica_count():
    """
    Helm rejects incompatible node-based scheduling before the scale request can reach Kubernetes.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render("rootControlPlane.pools[0].controller=DaemonSet", values_files=("root-values.yaml", "pool-values.yaml"))
    objects = render(
        "rootControlPlane.pools[0].controller=DaemonSet",
        "rootControlPlane.pools[0].replicas=1",
        values_files=("root-values.yaml", "pool-values.yaml"),
    )
    assert next(obj for obj in objects if obj["kind"] == "OperatorPool")["spec"]["controller"] == "DaemonSet"
