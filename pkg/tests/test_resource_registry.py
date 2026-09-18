"""
Keep resource routing, inventory, typed counts and reconciliation capabilities in agreement.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from attrs import fields
from attrs.exceptions import FrozenInstanceError

from polyad.compiler.registry import (
    AUXILIARY_KINDS,
    BOUNDARY_KINDS,
    CAPACITY_KINDS,
    COMPOSABLE_KINDS,
    DEFINITION_KINDS,
    GRAPH_OWNED_KINDS,
    NETWORK_POLICY_KINDS,
    POLYAD_KINDS,
    RECONCILED_KINDS,
    RESOURCE_MODELS,
    RESOURCE_TYPES,
)
from polyad.operator.adapters.kubernetes import API, BUILTINS, KINDS, WORKLOAD_KINDS
from polyad.operator.lifecycle.handlers import KINDS as WATCHED_KINDS
from polyad.operator.observability.graph_status import instance_metrics
from polyad_types.resources import GROUP, ResourceCounts
from polyad_types.resources.resources import RESOURCE_CLASSES
from tests.test_operator import resource


def test_catalog_is_complete_and_immutable():
    """
    Every model has one documented descriptor and callers cannot mutate shared capabilities.
    """
    assert len(RESOURCE_CLASSES) == len(RESOURCE_TYPES) == len(RESOURCE_MODELS)
    for kind, descriptor in RESOURCE_TYPES.items():
        assert descriptor.kind == kind
        assert RESOURCE_MODELS[kind].resource_type is descriptor
        assert descriptor.description
        if descriptor.boundary:
            assert descriptor.graph_owned and descriptor.reconciled and descriptor.composable
        if descriptor.auxiliary:
            assert descriptor.graph_owned and not descriptor.definition
    with pytest.raises(TypeError):
        RESOURCE_TYPES["Job"] = RESOURCE_TYPES["Deployment"]
    with pytest.raises(TypeError):
        RESOURCE_MODELS["Job"] = RESOURCE_MODELS["Deployment"]
    with pytest.raises(FrozenInstanceError):
        RESOURCE_TYPES["Job"].description = "changed"


def test_crd_identity_matches_registry():
    """
    Reject drift in kind, API group, version and plural between Python routing and shipped CRDs.
    """
    discovered = set()
    for path in (Path(__file__).resolve().parents[2] / "charts/polyad-crds/crds").glob("*.yaml"):
        document = yaml.safe_load(path.read_text())
        spec = document["spec"]
        kind = spec["names"]["kind"]
        if spec["group"] != GROUP:
            continue
        assert kind in POLYAD_KINDS
        descriptor = RESOURCE_TYPES[kind]
        version = next(version["name"] for version in spec["versions"] if version["storage"])
        assert descriptor.api_group == spec["group"]
        assert descriptor.api_version == f"{spec['group']}/{version}"
        assert descriptor.plural == spec["names"]["plural"]
        assert descriptor.namespaced == (spec["scope"] == "Namespaced")
        discovered.add(kind)
    assert discovered == POLYAD_KINDS


def test_inventory_and_reconciliation_roles():
    """
    Derived capabilities preserve ownership boundaries and keep definitions out of execution duties.
    """
    assert set(KINDS) == POLYAD_KINDS
    assert set(WORKLOAD_KINDS) == GRAPH_OWNED_KINDS - BOUNDARY_KINDS - {"Activation", "TemporaryConnection"}
    assert set(WATCHED_KINDS) == RECONCILED_KINDS == BOUNDARY_KINDS | {"Rewrite", "Composition", "Activation", "TemporaryConnection"}
    assert DEFINITION_KINDS.isdisjoint(RECONCILED_KINDS)
    assert COMPOSABLE_KINDS == BOUNDARY_KINDS | (DEFINITION_KINDS - {"GraphRule"})
    assert AUXILIARY_KINDS == CAPACITY_KINDS | NETWORK_POLICY_KINDS | {
        "Activation",
        "TemporaryConnection",
        "VirtualService",
        "DestinationRule",
    }
    assert RESOURCE_TYPES["Lease"].graph_owned is False
    assert RESOURCE_TYPES["ConfigMap"].api_group == ""
    assert BUILTINS["Job"] == ("/apis/batch/v1", "jobs")


def test_every_owned_kind_is_counted_in_typed_status():
    """
    New inventory kinds must remain representable in the published status schema.
    """
    observed_only = {"Cluster", "Dragonfly"}
    assert observed_only.isdisjoint(GRAPH_OWNED_KINDS)
    assert observed_only <= RESOURCE_TYPES.keys()
    counted = GRAPH_OWNED_KINDS | observed_only
    assert {field.name for field in fields(ResourceCounts)} - {"extra"} == counted
    parent = resource("Graph", "root", {"nodes": []})
    children = [resource(kind, kind.lower(), {}) for kind in sorted(counted)]
    metrics = instance_metrics(parent, children)
    assert metrics["resources"]["total"] == len(counted)
    assert metrics["resources"]["byKind"] == dict.fromkeys(counted, 1)


@pytest.mark.parametrize("mesh,capacity", [(False, False), (False, True), (True, False), (True, True)])
def test_inventory_respects_optional_api_features(monkeypatch, mesh, capacity):
    """
    Disabled integrations must not issue inventory requests against their resource endpoints.
    """
    monkeypatch.setenv("POLYAD_MESH_ENABLED", str(mesh).lower())
    monkeypatch.setenv("POLYAD_CAPACITY_ENABLED", str(capacity).lower())
    requested = []

    async def request(self, method, kind, namespace, **kwargs):
        requested.append(kind)
        return {"items": []}

    monkeypatch.setattr(API, "request", request)
    api = object.__new__(API)
    assert asyncio.run(api.owned("test", "owner")) == []
    expected = set(GRAPH_OWNED_KINDS)
    if not mesh:
        expected -= {"AuthorizationPolicy", "PeerAuthentication", "VirtualService", "DestinationRule"}
    if not capacity:
        expected -= {"Pod", "PodTemplate", "ProvisioningRequest"}
    assert set(requested) == expected
    assert len(requested) == len(expected)
