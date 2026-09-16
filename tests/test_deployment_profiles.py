"""
Verify profile selection, HA floors and management-cluster ownership of remote pools.
"""

from __future__ import annotations

import subprocess

import jsonschema
import pytest

from tests.test_chart import pytestmark as pytestmark
from tests.test_chart import render


@pytest.mark.parametrize(("ha", "mode"), [(False, "Dense"), (True, "Dense"), (True, "Distributed")])
def test_debug_logging_reaches_operator_components_and_observers(ha, mode):
    """
    Propagate the selected Python log level to every process template in each architecture.
    """
    objects = render(
        f"ha={str(ha).lower()}",
        f"architecture.mode={mode}",
        "operator.logLevel=DEBUG",
        "api.enabled=true",
        "metrics.enabled=true",
        "observer.enabled=true",
        "observer.existingSecret=observer-read-token",
        "global.multiCluster.clusterName=east",
    )
    pods = []
    for obj in objects:
        if obj["kind"] == "Deployment" and obj["metadata"]["name"] in {"test-polyad", "test-polyad-observer"}:
            pods.append(obj["spec"]["template"]["spec"])
        elif obj["kind"] == "Daemon":
            pods.append(obj["spec"]["template"]["spec"])
    assert len(pods) == (5 if mode == "Distributed" else 2)
    for pod in pods:
        assert {"name": "POLYAD_LOG_LEVEL", "value": "DEBUG"} in pod["containers"][0]["env"]
        container = pod["containers"][0]
        module = "polyad.operator.observer" if container["name"] == "observer" else "polyad.operator.runtime"
        assert container["command"] == ["/usr/bin/tini", "--", "python", "-m", module]


@pytest.mark.parametrize(("tag", "replicas"), [(None, 1), ("ha", 2), ("singular", 1)])
def test_dense_profiles_select_replica_counts_and_local_services(tag, replicas):
    """
    Route enabled endpoints to the one dense operator Deployment in either profile.
    """
    settings = ("ha=true",) if tag == "ha" else ("ha=false",) if tag else ()
    objects = render(*settings, "api.enabled=true", "events.enabled=true", "metrics.enabled=true", "connections.enabled=true")
    deployments = [obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad"]
    assert len(deployments) == 1
    deployment = deployments[0]
    assert deployment["spec"]["replicas"] == replicas
    assert deployment["metadata"]["labels"]["polyad.astrivant.com/deployment-profile"] == (tag or "singular")
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
    objects = render(
        f"architecture.mode={mode}",
        "api.enabled=true",
        "rootControlPlane.pools[0].nodeSelector.pool=workers",
        values_files=("root-values.yaml", "pool-values.yaml"),
    )
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
        ("ha=invalid",),
        ("tags.unsupported=true",),
        ("ha=false", "operator.replicaCount=2"),
        ("ha=false", "operator.autoscaling.enabled=true"),
        ("ha=false", "architecture.mode=Distributed", "api.enabled=true", "metrics.enabled=true"),
        ("ha=false", "rootControlPlane.enabled=true"),
        ("ha=true", "operator.replicaCount=1"),
        ("ha=true", "operator.autoscaling.enabled=true", "operator.autoscaling.minReplicas=1"),
        ("architecture.components.gateway.minReplicas=1",),
        ("architecture.autoscaling=true",),
    ],
)
def test_conflicting_profiles_fail_before_install(settings):
    """
    Reject ambiguous profiles and unsupported replica floors before installation.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(*settings)


@pytest.mark.parametrize(
    ("root", "settings"),
    [
        pytest.param(False, (), id="missing-root"),
        pytest.param(True, ("rootControlPlane.pools[0].cluster=unregistered",), id="unregistered-cluster"),
        pytest.param(True, ("rootControlPlane.pools[0].unexpected=true",), id="unknown-field"),
        pytest.param(
            True,
            (
                "rootControlPlane.pools[1].name=duplicate",
                "rootControlPlane.pools[1].cluster=west",
                "rootControlPlane.pools[1].replicas=2",
            ),
            id="duplicate-cluster",
        ),
    ],
)
def test_misplaced_pools_fail_before_install(root, settings):
    """
    Reject pools without root management or with invalid or duplicate destinations.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(*settings, values_files=("root-values.yaml", "pool-values.yaml") if root else ("pool-values.yaml",))


def test_pool_can_pause_without_scaling_the_root_to_zero():
    """
    Permit independent execution capacity to drain while the HA coordinator stays running.
    """
    objects = render("rootControlPlane.pools[0].replicas=0", values_files=("root-values.yaml", "pool-values.yaml"))
    pool = next(obj for obj in objects if obj["kind"] == "OperatorPool")
    root = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad")
    assert pool["spec"]["replicas"] == 0
    assert root["spec"]["replicas"] == 2
