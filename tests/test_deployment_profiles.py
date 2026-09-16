"""
Verify profile selection, HA floors and management-cluster ownership of remote pools.
"""

from __future__ import annotations

import subprocess

import jsonschema
import pytest

from tests.test_chart import pytestmark as pytestmark
from tests.test_chart import render

ROOT = (
    "tags.ha=true",
    "rootControlPlane.enabled=true",
    "rootControlPlane.kubeconfigSecret=root-access",
    "federation.enabled=true",
    "global.multiCluster.clusterName=management",
    "federation.clusters[0].name=west",
    "federation.clusters[0].namespace=workloads",
    "federation.clusters[0].kubeconfigSecret=west-access",
    "dragonfly.enabled=false",
    "dragonfly.existingSecret=root-cache",
    "metrics.enabled=true",
)
POOL = (
    "rootControlPlane.pools[0].name=west-workers",
    "rootControlPlane.pools[0].cluster=west",
    "rootControlPlane.pools[0].replicas=2",
)


@pytest.mark.parametrize(("tag", "replicas"), [(None, 2), ("ha", 2), ("singular", 1)])
def test_dense_profiles_select_replica_counts_and_local_services(tag, replicas):
    """
    Route enabled endpoints to the one dense operator Deployment in either profile.
    """
    settings = (f"tags.{tag}=true",) if tag else ()
    objects = render(*settings, "api.enabled=true", "events.enabled=true", "metrics.enabled=true", "connections.enabled=true")
    deployments = [obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad"]
    assert len(deployments) == 1
    deployment = deployments[0]
    assert deployment["spec"]["replicas"] == replicas
    assert deployment["metadata"]["labels"]["polyad.astrivant.com/deployment-profile"] == (tag or "ha")
    assert not any(obj["kind"] in {"Graph", "Daemon", "ReplicaGroup", "OperatorPool"} for obj in objects)
    pod_labels = deployment["spec"]["template"]["metadata"]["labels"]
    for service in (obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"].startswith("test-polyad-")):
        assert service["spec"]["selector"]["polyad.astrivant.com/component"] == "dense"
        assert all(pod_labels[key] == value for key, value in service["spec"]["selector"].items())


@pytest.mark.parametrize("mode", ["Dense", "Distributed"])
def test_ha_root_pools_belong_to_management_release(mode):
    """
    Declare remote execution intent at the root while keeping endpoint Services local.
    """
    objects = render(*ROOT, *POOL, f"architecture.mode={mode}", "api.enabled=true", "rootControlPlane.pools[0].nodeSelector.pool=workers")
    pool = next(obj for obj in objects if obj["kind"] == "OperatorPool")
    assert pool["metadata"]["namespace"] == "test"
    assert pool["metadata"]["name"] == "west-workers"
    assert pool["spec"] == {"cluster": "west", "replicas": 2, "nodeSelector": {"pool": "workers"}}
    crd = next(obj for obj in objects if obj["kind"] == "CustomResourceDefinition" and obj["spec"]["names"]["kind"] == "OperatorPool")
    jsonschema.Draft7Validator(crd["spec"]["versions"][0]["schema"]["openAPIV3Schema"]).validate(pool)
    assert not any(obj["kind"] == "Deployment" and obj["metadata"]["name"] == "west-workers" for obj in objects)
    assert all(obj["metadata"].get("namespace", "test") != "workloads" for obj in objects)
    api = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-api")
    metrics = next(obj for obj in objects if obj["kind"] == "Service" and obj["metadata"]["name"] == "test-polyad-metrics")
    assert api["spec"]["selector"]["polyad.astrivant.com/component"] == ("gateway" if mode == "Distributed" else "dense")
    assert metrics["spec"]["selector"]["polyad.astrivant.com/component"] == ("telemetry" if mode == "Distributed" else "dense")
    groups = [obj for obj in objects if obj["kind"] == "ReplicaGroup"]
    assert len(groups) == (3 if mode == "Distributed" else 0)
    assert all(obj["spec"]["minReplicas"] >= 2 for obj in groups)


@pytest.mark.parametrize(
    "settings",
    [
        ("tags.singular=true", "tags.ha=true"),
        ("tags.unsupported=true",),
        ("tags.singular=true", "operator.replicaCount=2"),
        ("tags.singular=true", "operator.autoscaling.enabled=true"),
        ("tags.singular=true", "architecture.mode=Distributed", "api.enabled=true", "metrics.enabled=true"),
        ("tags.singular=true", "rootControlPlane.enabled=true"),
        ("tags.ha=true", "operator.replicaCount=1"),
        ("tags.ha=true", "operator.autoscaling.enabled=true", "operator.autoscaling.minReplicas=1"),
        ("architecture.components.gateway.minReplicas=1",),
        ("architecture.autoscaling=true",),
        POOL,
        (*ROOT, *POOL, "rootControlPlane.pools[0].cluster=unregistered"),
        (*ROOT, *POOL, "rootControlPlane.pools[0].unexpected=true"),
        (
            *ROOT,
            *POOL,
            "rootControlPlane.pools[1].name=duplicate",
            "rootControlPlane.pools[1].cluster=west",
            "rootControlPlane.pools[1].replicas=2",
        ),
    ],
)
def test_conflicting_profiles_and_misplaced_pools_fail_before_install(settings):
    """
    Reject ambiguous profiles, unsupported replica floors and unregistered pool destinations.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(*settings)


def test_pool_can_pause_without_scaling_the_root_to_zero():
    """
    Permit independent execution capacity to drain while the HA coordinator stays running.
    """
    objects = render(*ROOT, *POOL, "rootControlPlane.pools[0].replicas=0")
    pool = next(obj for obj in objects if obj["kind"] == "OperatorPool")
    root = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    assert pool["spec"]["replicas"] == 0
    assert root["spec"]["replicas"] == 2
