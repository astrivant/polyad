"""
Verify independent autoscaling of bundled KEDA serving components in HA releases.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.test_chart import render


def autoscalers(objects):
    """
    Select KEDA component HPAs by their scale targets.
    """
    return {
        obj["spec"]["scaleTargetRef"]["name"]: obj["spec"]
        for obj in objects
        if obj["kind"] == "HorizontalPodAutoscaler" and obj["metadata"]["name"].startswith("test-keda-")
    }


@pytest.mark.parametrize("mode", ["Dense", "Distributed"])
def test_bundled_ha_scales_serving_components_with_independent_resource_metrics(mode):
    """
    Scale both serving components with an HA floor while retaining a fixed operator leader and standby.
    """
    settings = ("metrics.enabled=true", "api.enabled=true", "api.existingSecret=test-api") if mode == "Distributed" else ()
    objects = render("ha=true", "keda.install=true", f"architecture.mode={mode}", *settings)
    deployments = {obj["metadata"]["name"]: obj for obj in objects if obj["kind"] == "Deployment"}
    hpas = autoscalers(objects)
    assert set(hpas) == {"keda-operator-metrics-apiserver", "keda-admission-webhooks"}
    assert deployments["keda-operator"]["spec"]["replicas"] == 2
    for name, spec in hpas.items():
        assert (spec["minReplicas"], spec["maxReplicas"]) == (2, 5)
        assert spec["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 300
        assert spec["scaleTargetRef"]["apiVersion"] == "apps/v1"
        container = deployments[name]["spec"]["template"]["spec"]["containers"][0]
        metrics = {metric["containerResource"]["name"]: metric for metric in spec["metrics"]}
        assert set(metrics) == {"cpu", "memory"}
        for resource, target in (("cpu", 70), ("memory", 80)):
            assert metrics[resource] == {
                "type": "ContainerResource",
                "containerResource": {
                    "name": resource,
                    "container": container["name"],
                    "target": {"type": "Utilization", "averageUtilization": target},
                },
            }
            assert container["resources"]["requests"][resource]


def test_names_thresholds_and_disabled_memory_are_carried_to_the_real_targets():
    """
    Respect renamed upstream components, per-component tuning and an explicit zero stabilization window.
    """
    objects = render(
        "ha=true",
        "keda.install=true",
        "kedaOperator.operator.name=custom-scaler",
        "kedaOperator.webhooks.name=custom-admission",
        "keda.autoscaling.metricsServer.minReplicas=3",
        "keda.autoscaling.metricsServer.maxReplicas=9",
        "keda.autoscaling.metricsServer.targetCPUUtilizationPercentage=55",
        "keda.autoscaling.metricsServer.targetMemoryUtilizationPercentage=null",
        "keda.autoscaling.webhooks.targetMemoryUtilizationPercentage=65",
        "keda.autoscaling.behavior.scaleDown.stabilizationWindowSeconds=0",
    )
    hpas = autoscalers(objects)
    metrics = hpas["custom-scaler-metrics-apiserver"]
    assert (metrics["minReplicas"], metrics["maxReplicas"]) == (3, 9)
    assert metrics["metrics"] == [
        {
            "type": "ContainerResource",
            "containerResource": {
                "name": "cpu",
                "container": "custom-scaler-metrics-apiserver",
                "target": {"type": "Utilization", "averageUtilization": 55},
            },
        }
    ]
    assert hpas["custom-admission"]["metrics"][1]["containerResource"]["target"]["averageUtilization"] == 65
    assert all(spec["behavior"]["scaleDown"]["stabilizationWindowSeconds"] == 0 for spec in hpas.values())


@pytest.mark.parametrize(
    "settings",
    [
        ("ha=false",),
        ("keda.install=false",),
        ("keda.autoscaling.enabled=false",),
        ("kedaOperator.metricsServer.enabled=false", "kedaOperator.webhooks.enabled=false"),
        ("keda.autoscaling.metricsServer.enabled=false", "keda.autoscaling.webhooks.enabled=false"),
    ],
)
def test_inactive_or_external_installations_have_no_self_autoscalers(settings):
    """
    Honor installation ownership and all explicit autoscaling opt-outs.
    """
    assert not autoscalers(render("ha=true", "keda.install=true", *settings))


@pytest.mark.parametrize(
    "component,target", [("metricsServer", "keda-admission-webhooks"), ("webhooks", "keda-operator-metrics-apiserver")]
)
def test_disabling_one_component_retains_the_other_hpa(component, target):
    """
    Disabling an upstream component removes only its own autoscaler.
    """
    assert set(autoscalers(render("ha=true", "keda.install=true", f"kedaOperator.{component}.enabled=false"))) == {target}


@pytest.mark.parametrize(
    "setting",
    [
        "keda.autoscaling.metricsServer.minReplicas=1",
        "keda.autoscaling.webhooks.minReplicas=6",
        "keda.autoscaling.webhooks.maxReplicas=33",
        "keda.autoscaling.metricsServer.targetCPUUtilizationPercentage=0",
        "keda.autoscaling.webhooks.targetMemoryUtilizationPercentage=101",
        "keda.autoscaling.webhooks.enabled=invalid",
        "keda.autoscaling.behavior.scaleDown.stabilizationWindowSeconds=-1",
        "kedaOperator.operator.replicaCount=1",
        "kedaOperator.metricsServer.replicaCount=1",
        "kedaOperator.webhooks.replicaCount=1",
        "kedaOperator.resources.metricServer.requests.cpu=null",
        "kedaOperator.resources.metricServer.requests.cpu=0m",
        "kedaOperator.resources.webhooks.requests.memory=0Mi",
    ],
)
def test_unsafe_autoscaling_settings_fail_before_installation(setting):
    """
    Reject invalid types, bounds, HA floors and missing or zero resource requests.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render("ha=true", "keda.install=true", setting)
