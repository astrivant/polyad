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
    assert CHART.parents[1] / "examples/postgresql/operator-values.yaml" in paths
    for path in paths:
        VALUES_CHECK["validate"](path)


@pytest.mark.parametrize(
    "path",
    [path for path in VALUES_CHECK["value_files"]() if "examples" in path.parts and path.name != "operator-values.yaml"],
    ids=lambda path: str(path.relative_to(CHART.parents[1])),
)
def test_example_values_render_with_the_full_helm_schema(path):
    """
    Merge example overlays with chart and dependency defaults in their documented namespace.
    """
    output = subprocess.check_output(["helm", "template", "test", str(CHART), "--namespace", "polyad", "-f", str(path)], text=True)
    assert any(obj and obj["kind"] == "Deployment" for obj in yaml.safe_load_all(output))


@pytest.mark.parametrize(
    "value",
    [
        {"ha": "true"},
        {"observer": {"resources": {"requests": {"cpu": 1}}}},
        {"observer": {"resources": {"requests": "100m"}}},
        {"postgresql": {"resources": {"limits": {"memory": True}}}},
        {"observer": {"peers": [{"podSelector": {"matchLabels": {"app": True}}}]}},
        {"observer": {"peers": [{"namespaceSelector": {"matchExpressions": [{"key": "team", "operator": "In", "values": "ops"}]}}]}},
        {"rootControlPlane": {"pools": [{"name": "west", "cluster": "west", "replicas": 1, "tolerations": [{"tolerationSeconds": "30"}]}]}},
        {"rootControlPlane": {"pools": [{"name": "west", "cluster": "west", "replicas": 1, "resources": {"requests": {"cpu": []}}}]}},
        {"networkPolicy": {"extraEgress": [{"ports": [{"port": True}]}]}},
        {"networkPolicy": {"extraEgress": [{"ports": [{"protocol": "https"}]}]}},
        {"istioEastWest": {"labels": {"networking.istio.io/gatewayPort": 15443}}},
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


@pytest.mark.parametrize("path", sorted(CHART.glob("values-*.reference.yaml")), ids=lambda path: path.name)
def test_reference_overlays_are_typed_and_render_independently(path):
    """
    Editor validation accepts partial overrides while Helm validates the complete merged configuration.
    """
    source = path.read_text()
    assert source.startswith("# yaml-language-server: $schema=values.reference.schema.json")
    schema = json.loads((CHART / "values.schema.json").read_text())
    overlay = json.loads((CHART / "values.reference.schema.json").read_text())
    registry = Registry().with_resource("values.schema.json", Resource.from_contents(schema))
    validator = jsonschema.Draft7Validator(overlay, registry=registry)
    validator.validate(yaml.safe_load(source))
    assert not validator.is_valid({"ha": "true"})
    assert not validator.is_valid({"authentication": {"storage": {"enabled": "true"}}})
    objects = yaml.safe_load_all(subprocess.check_output(["helm", "template", "test", str(CHART), "-f", str(path)], text=True))
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
    command = ["helm", "template", "test", str(CHART), "-f", str(CHART / "values-authentication.reference.yaml")]
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
