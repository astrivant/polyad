"""
Verify a consistent operator allocation across installation and injection modes.
"""

from __future__ import annotations

import pytest

from tests.test_chart import CHART, render


@pytest.mark.parametrize(
    "profile",
    [
        "values-singular.reference.yaml",
        "values-ha.reference.yaml",
        "values-components.reference.yaml",
        "values-worker.reference.yaml",
        "values-observer.reference.yaml",
        "values-root-control-plane.reference.yaml",
    ],
)
def test_operator_profiles_preserve_guaranteed_resources(profile):
    """
    Shipped profiles retain equal CPU and memory pairs in every operator Pod template.
    """
    objects = render(
        *(("federation.clusters[0].namespace=test",) if profile == "values-worker.reference.yaml" else ()),
        values_files=(CHART / profile,),
    )
    expected = {"requests": {"cpu": "1", "memory": "1Gi"}, "limits": {"cpu": "1", "memory": "1Gi"}}
    count = 0
    for obj in objects:
        if obj["kind"] in {"Deployment", "Daemon"}:
            for container in obj["spec"]["template"]["spec"]["containers"]:
                if "polyad.operator.runtime" in container.get("command", []) or "polyad.operator.observer" in container.get("command", []):
                    assert container["resources"] == expected
                    count += 1
        elif obj["kind"] == "OperatorPool" and "resources" in obj["spec"]:
            assert obj["spec"]["resources"] == expected
    assert count >= (4 if profile == "values-components.reference.yaml" else 1)


@pytest.mark.parametrize("overrides", [(), ("mesh.proxyResources.cpu=200m", "mesh.proxyResources.memory=256Mi")])
def test_meshed_operators_and_observers_preserve_guaranteed_resources(overrides):
    """
    Injection annotations set both requests and limits, including after proxy right-sizing.
    """
    objects = render(
        "global.multiCluster.clusterName=test",
        "mesh.enabled=true",
        "mesh.operator.enabled=true",
        "observer.enabled=true",
        "observer.existingSecret=observer-access",
        "observer.mesh=true",
        "observer.principals[0]=cluster.local/ns/test/sa/root",
        *overrides,
    )
    pods = [
        obj["spec"]["template"]
        for obj in objects
        if obj["kind"] == "Deployment" and obj["metadata"]["name"] in {"test-polyad", "test-polyad-observer"}
    ]
    assert len(pods) == 2
    for pod in pods:
        annotations = pod["metadata"]["annotations"]
        assert (
            annotations["sidecar.istio.io/proxyCPU"] == annotations["sidecar.istio.io/proxyCPULimit"] == ("200m" if overrides else "100m")
        )
        assert (
            annotations["sidecar.istio.io/proxyMemory"]
            == annotations["sidecar.istio.io/proxyMemoryLimit"]
            == ("256Mi" if overrides else "128Mi")
        )
        assert all(container["resources"]["requests"] == container["resources"]["limits"] for container in pod["spec"]["containers"])
