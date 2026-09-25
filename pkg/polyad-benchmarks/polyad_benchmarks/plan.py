"""
Render benchmark plans from the fixture chart and submit them through the standalone client.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml  # type: ignore[import-untyped]

from polyad_benchmarks.config import RunConfig, operator_client, request_prefix
from polyad_benchmarks.identity import grafana_path, log_event, new_run_id
from polyad_types import CompositionRequest, converter

if TYPE_CHECKING:
    from typing import Any

    from polyad_sdk import Client

__all__ = (
    "composition_plan",
    "main",
    "submit_plan",
)


def composition_plan(plan: dict[str, Any], chart: Path, namespace: str) -> dict[str, Any]:
    """
    Use Helm's canonical fixture definitions to produce a client composition with per-run settings.

    Args:
        plan (dict[str, Any]): Chart variable overrides and an optional requestId for an explicit retry.
        chart (Path): Benchmark chart with its locked dependencies already built.
        namespace (str): Operator API namespace; must match the receiving operator.

    Returns:
        dict[str, Any]: Validated immutable composition document; administrator policies stay references.
    """
    if set(plan) - {"requestId", "variables"} or not isinstance(plan.get("variables", {}), dict):
        raise ValueError("plans contain a variables mapping and an optional requestId for retries")
    variables = plan.get("variables", {})
    RunConfig(**variables.get("run", {}))
    identity = plan["requestId"] if "requestId" in plan else new_run_id()
    if not isinstance(identity, str):
        raise ValueError("requestId must be a string")
    request_prefix(identity)

    # A retry of one run keeps the same rendered identities; a new run gets an independent release.
    release = "bench-" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    with tempfile.TemporaryDirectory(prefix="polyad-plan-") as directory:
        values = Path(directory) / "values.json"
        values.write_text(json.dumps({"polyadResources": {"variables": variables}, "reloader": {"enabled": False}}))
        rendered = subprocess.check_output(
            [
                "helm",
                "template",
                release,
                str(chart.resolve()),
                "--namespace",
                namespace,
                "--values",
                str(values),
                "--skip-tests",
            ],
            text=True,
        )
    objects = []
    for resource in yaml.safe_load_all(rendered):
        if not resource or resource.get("apiVersion") != "polyad.astrivant.com/v1alpha1" or resource["kind"] == "GraphPolicy":
            continue
        kind, name = resource["kind"], resource["metadata"]["name"]
        spec = copy.deepcopy(resource["spec"])
        if name == "load-plan":
            spec["manifest"]["immutable"] = True
        if kind == "Graph":
            for node in spec["nodes"]:
                node["id"], node["refId"] = node.pop("name"), node.pop("ref")
                node.pop("kind")
                for requirement in node.get("requires", []):
                    requirement["nodeId"] = requirement.pop("node")
            for connection in spec.get("connections", []):
                connection["sourceId"], connection["targetId"] = connection.pop("source"), connection.pop("target")
        if name == "load-runner":
            # Composing a new run is itself explicit intent to run it once.
            spec.pop("activation", None)
            spec["template"]["spec"]["containers"][0]["args"] += ["--run-id", identity]
        objects.append({"id": name, "kind": kind, "spec": spec})
    document = {"requestId": identity, "rootId": "load-study", "objects": objects}
    converter.structure(document, CompositionRequest)
    return document


def submit_plan(client: Client, plan: dict[str, Any], chart: Path, namespace: str) -> dict[str, Any]:
    """
    Submit a new run without Kubernetes write credentials or changing operator replica counts.

    Args:
        client (Client): Authorized operator composition client.
        plan (dict[str, Any]): Fixture variables; an optional requestId deliberately reuses a previous run.
        chart (Path): Canonical fixture chart location.
        namespace (str): Namespace served by the target operator.

    Returns:
        dict[str, Any]: Composition receipt; acceptance does not mean completion.
    """
    document = composition_plan(plan, chart, namespace)
    return _submit(client, document, namespace)


def _submit(client: Client, document: dict[str, Any], namespace: str) -> dict[str, Any]:
    """
    Report the generated identity before submitting so uncertain writes remain traceable.

    Args:
        client (Client): Authorized composition client.
        document (dict[str, Any]): Fully rendered composition with a fixed request ID.
        namespace (str): Operator namespace.

    Returns:
        dict[str, Any]: Receipt with the submitted run key and dashboard link.
    """
    run_id = document["requestId"]
    log_event("benchmark.run.submitting", run_id, namespace=namespace, requestId=run_id)
    receipt = client.compose(document)
    log_event("benchmark.run.submitted", run_id, namespace=namespace, requestId=run_id)
    return {**receipt, "runId": run_id, "grafanaPath": grafana_path(run_id, namespace)}


def main() -> None:
    """
    Render a reviewable plan artifact or submit it through Polyad's composition API.

    Returns:
        None: Emit the plan/receipt; no automatic retries or cloud cleanup are performed.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--chart", type=Path, default=Path("charts/polyad-benchmarks"))
    parser.add_argument("--namespace", default="polyad")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--run-id", help="Reuse a previously generated ID only when retrying that same run")
    args = parser.parse_args()
    configured = json.loads(args.plan.read_text())
    if args.run_id is not None:
        configured["requestId"] = request_prefix(args.run_id)
    document = composition_plan(configured, args.chart, args.namespace)
    print(json.dumps(document if args.render_only else _submit(operator_client(30), document, args.namespace), indent=2))


if __name__ == "__main__":
    main()
