"""
Exercise benchmark bounds, real HTTP transport, chart contracts and refresh barriers offline.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import jsonschema
import pytest
import yaml

from polyad_benchmarks import fixture, plan, refresh, runner
from polyad_benchmarks.config import RunConfig

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "parameters",
    [
        {"rate": 0},
        {"duration": float("inf")},
        {"rate": float("nan")},
        {"concurrency": 129},
        {"max_requests": True},
        {"max_requests": 10001},
        {"request_timeout": 5, "timeout": 1},
        {"poll_interval": 0},
    ],
)
def test_run_bounds(parameters):
    """
    Reject inputs that could silently create unbounded pressure or invalid clocks.
    """
    with pytest.raises(ValueError):
        RunConfig(**parameters)


def test_saturation_skips_arrivals_without_executor_backlog():
    """
    Completion delays must not reduce offered arrivals or create an unbounded pending queue.
    """

    def slow(identity):
        time.sleep(0.15)
        return {"requestId": identity, "phase": "Completed", "elapsedSeconds": 0.15}

    result = runner.run(RunConfig(rate=100, duration=0.04, concurrency=1), "bounded", slow, threading.Event())
    assert result["scheduled"] == 4
    assert result["submitted"] <= 1
    assert result["skipped"] == result["scheduled"] - result["submitted"]


def test_stop_does_not_schedule_more_work():
    """
    An interrupted study retains a report instead of submitting further activations.
    """
    stop = threading.Event()
    stop.set()
    result = runner.run(RunConfig(), "stopped", lambda _: pytest.fail("unexpected work"), stop)
    assert result["submitted"] == result["scheduled"] == 0
    assert result["interrupted"]


def test_fixture_uses_real_http_and_pins_graph_context(monkeypatch):
    """
    Callers cannot redirect the fixture's authority to an arbitrary graph or vertex.
    """
    calls = []

    class Operator:
        def activate(self, **kwargs):
            calls.append(kwargs)
            return {"requestId": kwargs["request_id"], "status": {"phase": "Pending"}}

        def activation(self, identity):
            return {"requestId": identity, "status": {"phase": "Completed"}}

    monkeypatch.setenv("POLYAD_GRAPH_NAME", "actual-graph")
    monkeypatch.setenv("POLYAD_GRAPH_UID", "actual-uid")
    monkeypatch.setenv("POLYAD_BENCHMARK_FIXTURE_TOKEN", "fixture-token")
    monkeypatch.setattr(fixture, "operator_client", lambda _: Operator())
    monkeypatch.setattr(runner, "operator_client", lambda _: Operator())
    with fixture.FixtureServer(("127.0.0.1", 0)) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = runner.exercise(f"http://127.0.0.1:{server.server_port}", "sample-00001", RunConfig(), threading.Event())
            assert result["phase"] == "Completed"
            assert result["acceptanceSeconds"] <= result["elapsedSeconds"]
            assert calls == [{"request_id": "sample-00001", "graph": "actual-graph", "graph_uid": "actual-uid", "node": "batch"}]
            monkeypatch.delenv("POLYAD_BENCHMARK_FIXTURE_TOKEN")
            # The request cannot use the operator token as its fixture credential.
            monkeypatch.setenv("POLYAD_BENCHMARK_FIXTURE_TOKEN", "fixture-token")
            from polyad_client import APIError, Client

            with pytest.raises(APIError) as error:
                Client(f"http://127.0.0.1:{server.server_port}", "wrong").activate(
                    request_id="sample-00002", graph="other", graph_uid="other", node="other"
                )
            assert error.value.status == 401
            assert len(calls) == 1
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_chart_uses_crd_templates_and_explicit_pulses():
    """
    Render actual resource schemas and ensure install does not start load automatically.
    """
    chart = ROOT / "charts/polyad-benchmarks"
    text = subprocess.check_output(
        [
            "helm",
            "template",
            "study",
            str(chart),
            "--namespace",
            "polyad",
            "--values",
            str(ROOT / "studies/load/gke-values.yaml"),
            "--set",
            "polyadResources.variables.secretName=study-access",
            "--set",
            "monitoring.enabled=false,tempo.enabled=false,collector.enabled=false,reloader.enabled=false,observability.enabled=false",
        ],
        text=True,
    )
    objects = {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(text) if obj}
    catalog = json.loads((ROOT / "charts/polyad-crds/files/resource-catalog.json").read_text())
    for (kind, _), obj in objects.items():
        definition = next(item for item in catalog.values() if item["kind"] == kind)
        jsonschema.Draft7Validator(definition["schema"]).validate({key: obj[key] for key in ("metadata", "spec")})
    graph = objects["Graph", "load-study"]["spec"]
    assert graph["mode"] == "persistent"
    assert {node["name"] for node in graph["nodes"]} == {"plan", "discovery", "fixture", "batch", "runner"}
    assert objects["Workload", "load-runner"]["spec"]["activation"]["mode"] == "Reject"
    assert objects["Workload", "load-batch"]["spec"]["activation"]["maxConcurrent"] == 2
    assert not any(kind == "Activation" for kind, _ in objects)

    for kind, name in (("Workload", "load-runner"), ("Workload", "load-batch"), ("Daemon", "load-fixture")):
        pod = objects[kind, name]["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["cloud.google.com/gke-nodepool"] == ("copolyad" if name == "load-runner" else "fixtures")
        pool = "copolyad" if name == "load-runner" else "fixtures"
        assert {"key": "dedicated", "operator": "Equal", "value": pool, "effect": "NoSchedule"} in pod["tolerations"]
        assert pod["automountServiceAccountToken"] is False
    env = {item["name"]: item for item in objects["Workload", "load-runner"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    projected = objects["Resource", "load-plan"]["spec"]["manifest"]
    RunConfig(**json.loads(projected["data"]["plan.json"])["run"])
    assert env["POLYAD_API_TOKEN"]["valueFrom"]["secretKeyRef"] == {"name": "study-access", "key": "operator-token"}
    assert "${nodes.discovery.name}" in env["POLYAD_BENCHMARK_FIXTURE_URL"]["value"]


@pytest.mark.parametrize("profile", ["smoke", "steady", "burst"])
def test_packaged_profiles_project_their_json_plans(profile):
    """
    Every test selection applies its packaged bounds without starting a run at installation.
    """
    chart = ROOT / "charts/polyad-benchmarks"
    values = yaml.safe_load((chart / f"values-{profile}.yaml").read_text())
    plan = json.loads((chart / values["benchmark"]["planFile"]).read_text())
    schema = json.loads((ROOT / "pkg/polyad-benchmarks/polyad_benchmarks/variables.schema.json").read_text())
    jsonschema.validate(plan["variables"], schema)
    RunConfig(**plan["variables"]["run"])
    rendered = subprocess.check_output(["helm", "template", "study", str(chart), "-f", str(chart / f"values-{profile}.yaml")], text=True)
    objects = {(obj["kind"], obj["metadata"]["name"]): obj for obj in yaml.safe_load_all(rendered) if obj}
    projected = json.loads(objects["Resource", "load-plan"]["spec"]["manifest"]["data"]["plan.json"])
    assert projected == {key: plan["variables"][key] for key in ("run", "replicas")}
    assert objects["Daemon", "load-fixture"]["spec"]["replicas"] == projected["replicas"]["fixture"]
    assert objects["Workload", "load-batch"]["spec"]["activation"]["maxConcurrent"] == projected["replicas"]["batch"]
    assert objects["Workload", "load-runner"]["spec"]["activation"]["mode"] == "Reject"
    assert not any(kind == "Activation" for kind, _ in objects)


@pytest.fixture
def prepared(tmp_path):
    """
    Retain an actual repository source fingerprint with copied study inputs.
    """
    root = tmp_path / "refresh"
    manifest = refresh.prepare(ROOT, root)
    assert manifest["matrix"] == {"study": list(refresh.STUDIES)}
    return root


def test_refresh_rejects_input_drift_before_cluster_mutation(prepared, monkeypatch):
    """
    Altered prepared recipes cannot authorize changed cloud work.
    """
    (prepared / "inputs/load.json").write_text("{}")
    monkeypatch.setattr(refresh, "cluster_study", lambda *_: pytest.fail("cluster contacted after input drift"))
    with pytest.raises(ValueError, match="recipe"):
        refresh.study_phase(ROOT, prepared, "load", "test-context")
    status = json.loads((prepared / "statuses/load.json").read_text())
    assert status["success"] is False


@pytest.mark.parametrize("damage", [None, "missing", "failed", "mislabeled", "unexpected", "skipped"])
def test_refresh_publication_barrier(prepared, damage):
    """
    Partial, failed or ambiguous matrices retain artifacts without publishing success.
    """
    status = {"study": "other" if damage == "mislabeled" else "load", "success": damage != "failed"}
    if damage != "missing":
        refresh.write_json(prepared / "statuses/load.json", status)
    if damage == "unexpected":
        refresh.write_json(prepared / "statuses/unknown.json", status)
    refresh.write_json(
        prepared / "outputs/load/results.json",
        {
            "submitted": 2,
            "phases": {"Completed": 2},
            "skipped": int(damage == "skipped"),
            "interrupted": False,
        },
    )
    if damage:
        with pytest.raises(ValueError):
            refresh.finish(ROOT, prepared)
        assert not (prepared / "summary.json").exists()
    else:
        refresh.finish(ROOT, prepared)
        assert json.loads((prepared / "summary.json").read_text())["studies"]["load"]["submitted"] == 2


def test_failed_study_retains_diagnostics_and_cannot_be_repeated(prepared, monkeypatch):
    """
    Reusing a failed refresh must not silently repeat remote side effects.
    """

    def failure(*_):
        raise TimeoutError("synthetic test failure")

    monkeypatch.setattr(refresh, "cluster_study", failure)
    with pytest.raises(TimeoutError):
        refresh.study_phase(ROOT, prepared, "load", "test-context")
    with pytest.raises(ValueError, match="already attempted"):
        refresh.study_phase(ROOT, prepared, "load", "test-context")
    assert json.loads((prepared / "statuses/load.json").read_text())["success"] is False


def test_client_plan_uses_canonical_graph_with_immutable_per_run_settings():
    """
    Configure replica counts through composition while retaining administrator rule references.
    """
    configured = json.loads((ROOT / "studies/load/plan.json").read_text())
    document = plan.composition_plan(configured, ROOT / "charts/polyad-benchmarks", "polyad")
    objects = {item["id"]: item for item in document["objects"]}
    assert document["requestId"] == configured["requestId"]
    assert objects["load-fixture"]["spec"]["replicas"] == 2
    for name, pool in (("load-fixture", "fixtures"), ("load-batch", "fixtures"), ("load-runner", "copolyad")):
        pod = objects[name]["spec"]["template"]["spec"]
        assert pod["nodeSelector"]["cloud.google.com/gke-nodepool"] == pool
        assert {"key": "dedicated", "operator": "Equal", "value": pool, "effect": "NoSchedule"} in pod["tolerations"]
    assert objects["load-batch"]["spec"]["activation"]["maxConcurrent"] == 4
    assert "activation" not in objects["load-runner"]["spec"]
    assert objects["load-runner"]["spec"]["template"]["spec"]["containers"][0]["args"][-2:] == ["--run-id", configured["requestId"]]
    assert not any(item["kind"] == "GraphRule" for item in document["objects"])
    assert objects["load-study"]["spec"]["rules"]
    graph = objects["load-study"]["spec"]
    assert {item["refId"] for item in graph["nodes"]} <= set(objects)
    projected = objects["load-plan"]["spec"]["manifest"]
    assert projected["immutable"] is True
    assert json.loads(projected["data"]["plan.json"])["replicas"] == {"fixture": 2, "batch": 4}


def test_monitoring_and_reloader_target_the_operator_from_fixtures_pool():
    """
    Verify actual dependency manifests, endpoints, controller discovery and scheduling.
    """
    rendered = subprocess.check_output(
        [
            "helm",
            "template",
            "study",
            str(ROOT / "charts/polyad-benchmarks"),
            "-n",
            "polyad",
            "-f",
            str(ROOT / "studies/load/gke-values.yaml"),
        ],
        text=True,
    )
    objects = {(item["kind"], item["metadata"]["name"]): item for item in yaml.safe_load_all(rendered) if item}
    monitor = objects["ServiceMonitor", "study-polyad"]
    assert monitor["spec"]["selector"]["matchLabels"]["app.kubernetes.io/component"] == "metrics"
    prometheus = next(item for (kind, _), item in objects.items() if kind == "Prometheus")
    assert prometheus["spec"]["serviceMonitorSelector"]["matchLabels"].items() <= monitor["metadata"]["labels"].items()
    assert prometheus["spec"]["storage"]["volumeClaimTemplate"]["spec"]["resources"]["requests"]["storage"] == "10Gi"
    datasource = yaml.safe_load(objects["ConfigMap", "benchmarks-monitoring-grafana-datasource"]["data"]["datasource.yaml"])
    assert next(item for item in datasource["datasources"] if item["uid"] == "tempo")["url"] == "http://benchmarks-tempo:3200"
    config = next(item for (kind, _), item in objects.items() if kind == "ConfigMap" and "relay" in item.get("data", {}))
    otel = yaml.safe_load(config["data"]["relay"])
    exporter = otel["service"]["pipelines"]["traces"]["exporters"][0]
    assert otel["exporters"][exporter]["endpoint"] == "benchmarks-tempo:4317"
    for (kind, _), item in objects.items():
        if kind in {"Deployment", "StatefulSet", "DaemonSet", "Job", "Prometheus"}:
            pod = item["spec"] if kind == "Prometheus" else item["spec"]["template"]["spec"]
            assert pod["nodeSelector"]["cloud.google.com/gke-nodepool"] == "fixtures"
            assert any(value.get("key") == "dedicated" and value.get("value") == "fixtures" for value in pod["tolerations"])
    assert objects["Daemon", "load-fixture"]["metadata"]["annotations"]["configmap.reloader.stakater.com/auto"] == "true"
    for name in ("load-batch", "load-runner"):
        assert "configmap.reloader.stakater.com/auto" not in objects["Workload", name]["metadata"].get("annotations", {})
    reloader = next(item for (kind, name), item in objects.items() if kind == "Deployment" and "reloader" in name)
    args = reloader["spec"]["template"]["spec"]["containers"][0]["args"]
    assert "--ignored-workload-types=jobs,cronjobs" in args


@pytest.mark.parametrize("drift", [False, True])
def test_cluster_study_accepts_multiple_fixture_replicas_and_rejects_plan_drift(prepared, monkeypatch, drift):
    """
    Refreshes retain all replica identities and fail if a measured definition changes.
    """
    before = {"metadata": {"uid": "same", "generation": 1}, "spec": {"intent": 1}}
    reads = {}

    def kubectl(command, **kwargs):
        args = command[6:]
        if args[:2] == ["get", "pods"]:
            value = {
                "items": [
                    {
                        "metadata": {"name": name},
                        "spec": {"nodeName": "node"},
                        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    }
                    for name in ("fixture-b", "fixture-a")
                ]
            }
        elif args[0] == "exec":
            assert args[1] == "fixture-a"
            value = {"name": "receipt"}
        elif args[0] == "logs":
            value = {"submitted": 1, "skipped": 0, "interrupted": False, "phases": {"Completed": 1}}
        elif args[:2] == ["get", "activation"]:
            value = {"status": {"phase": "Completed", "execution": {"kind": "Job", "name": "runner"}}}
        else:
            key = tuple(args[:3])
            reads[key] = reads.get(key, 0) + 1
            value = json.loads(json.dumps(before))
            if drift and args[1:3] == ["resource", "load-plan"] and reads[key] > 1:
                value["spec"]["intent"] = 2
        return subprocess.CompletedProcess(command, 0, json.dumps(value), "")

    monkeypatch.setattr(refresh.subprocess, "run", kubectl)
    if drift:
        with pytest.raises(ValueError, match="definition or plan"):
            refresh.cluster_study(prepared, "load", "context")
    else:
        assert refresh.cluster_study(prepared, "load", "context")["submitted"] == 1
    fixtures = json.loads((prepared / "outputs/load/fixture.json").read_text())
    assert [item["name"] for item in fixtures] == ["fixture-a", "fixture-b"]
