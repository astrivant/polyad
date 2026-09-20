"""
Prepare, execute and verify isolated study artifacts locally or across CI jobs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from polyad_benchmarks.identity import new_run_id
from polyad_benchmarks.runner import TERMINAL

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "CHEEGER_STUDIES",
    "LOCAL_STUDIES",
    "PROCESS_STUDIES",
    "REACHABILITY_STUDIES",
    "STUDIES",
    "cluster_study",
    "finish",
    "main",
    "prepare",
    "selected_studies",
    "sources",
    "study_phase",
    "verify_inputs",
    "write_json",
)


STUDIES = ("load",)
REACHABILITY_STUDIES = ("symbiosis", "reachability-state", "reachability-routing")
PROCESS_STUDIES = ("soul", "nature")
CHEEGER_STUDIES = ("cheeger-strategies",)
LOCAL_STUDIES = REACHABILITY_STUDIES + CHEEGER_STUDIES + PROCESS_STUDIES


def sources(project: Path) -> dict[str, str]:
    """
    Fingerprint study recipes, shared client and chart inputs without collecting secrets.

    Args:
        project (Path): Repository root.

    Returns:
        dict[str, str]: Relative source paths and SHA-256 hashes.
    """
    result = {}
    for name in ("soul.py", "nature.py"):
        path = project / name
        if path.is_file():
            result[name] = hashlib.sha256(path.read_bytes()).hexdigest()

    # Strategy studies call the production solver: changes invalidate prepared inputs too.
    for path in sorted((project / "pkg/polyad/graph").glob("*.py")):
        result[str(path.relative_to(project))] = hashlib.sha256(path.read_bytes()).hexdigest()

    # Fingerprint executable sources and recipes, not generated results or
    # figures. Otherwise publishing a result would invalidate its own input set.
    for directory in (
        "pkg/polyad-benchmarks",
        "pkg/polyad-sdk",
        "pkg/polyad-types",
        "charts/polyad-benchmarks",
        "charts/polyad-crds",
        "services",
        "studies",
    ):
        for path in sorted((project / directory).rglob("*")):
            # Archived study recipes are historical evidence, not active refresh
            # inputs. Editing an archive must not invalidate a prepared run.
            if directory == "studies" and (
                path.name == "results.json" or path.relative_to(project / directory).parts[0].endswith("-deprecated")
            ):
                continue
            if path.is_file() and path.suffix in {".py", ".toml", ".lock", ".yaml", ".json"} and "__pycache__" not in path.parts:
                result[str(path.relative_to(project))] = hashlib.sha256(path.read_bytes()).hexdigest()
            elif path.is_file() and path.name == "Dockerfile":
                result[str(path.relative_to(project))] = hashlib.sha256(path.read_bytes()).hexdigest()

    return result


def write_json(path: Path, value: Any) -> None:
    """
    Persist a human-readable artifact after creating its containing directory.

    Args:
        path (Path): Artifact filename.
        value (Any): JSON-compatible artifact.

    Returns:
        None: Replace the file atomically inside its output directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Readers must see either the previous complete document or the new one,
    # never a partly written JSON file if execution stops during serialization.
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def prepare(project: Path, root: Path, studies: tuple[str, ...] = STUDIES) -> dict[str, Any]:
    """
    Snapshot one common input set and derive the matrix from the Python study inventory.

    Args:
        project (Path): Source repository.
        root (Path): New refresh directory; existing runs are never overwritten.
        studies (tuple[str, ...]): Nonempty, unique selection from the cloud and local inventories.

    Returns:
        dict[str, Any]: Provenance and matrix shared by all study jobs.
    """
    if not studies or len(set(studies)) != len(studies) or not set(studies) <= set(STUDIES + LOCAL_STUDIES):
        raise ValueError("select unique registered studies")

    # A run gets a fresh directory and identities before any study starts. All
    # later phases consume these copies, not recipes that may subsequently change.
    root.mkdir(parents=True, exist_ok=False)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=project, text=True).strip()
    for study in studies:
        config = json.loads((project / "studies" / study / "fixtures" / "scenario.json").read_text())
        config["runId"] = new_run_id()
        write_json(root / "inputs" / f"{study}.json", config)

    # Record both the checkout revision and byte-level source hashes: uncommitted
    # local changes must be distinguishable even when the Git revision is unchanged.
    provenance = {
        "revision": revision,
        "preparedAt": datetime.now(UTC).isoformat(),
        "sources": sources(project),
        "inputs": {study: hashlib.sha256((root / "inputs" / f"{study}.json").read_bytes()).hexdigest() for study in studies},
        "matrix": {"study": list(studies)},
    }
    write_json(root / "provenance.json", provenance)
    return provenance


def selected_studies(root: Path) -> tuple[str, ...]:
    """
    Validate the prepared study matrix before reading or publishing its artifacts.

    Args:
        root (Path): Prepared refresh directory.

    Returns:
        tuple[str, ...]: Registered studies selected at preparation time.
    """
    provenance = json.loads((root / "provenance.json").read_text())

    # The prepared matrix is the publication contract. A missing or unexpected
    # study must not silently shrink or extend the set of required results.
    selected = tuple(provenance["matrix"]["study"])
    if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(STUDIES + LOCAL_STUDIES):
        raise ValueError("invalid prepared study inventory")
    if set(provenance["inputs"]) != set(selected):
        raise ValueError("prepared inputs differ from the study inventory")
    return selected


def verify_inputs(project: Path, root: Path) -> None:
    """
    Reject source or recipe drift between preparation and a distributed study job.

    Args:
        project (Path): Restored source checkout.
        root (Path): Restored shared input directory.

    Returns:
        None: Mismatched inputs raise ValueError before any cluster mutation.
    """

    # Recheck at both execution and publication time so a distributed worker
    # cannot combine one version's inputs with another version's measurements.
    provenance = json.loads((root / "provenance.json").read_text())
    if provenance["sources"] != sources(project):
        raise ValueError("study sources differ from the prepared snapshot")
    for study in selected_studies(root):
        if provenance["inputs"][study] != hashlib.sha256((root / "inputs" / f"{study}.json").read_bytes()).hexdigest():
            raise ValueError("study recipe differs from the prepared snapshot")


def cluster_study(root: Path, study: str, context: str) -> dict[str, Any]:
    """
    Activate the installed runner through its fixture and retain live cluster outcomes.

    Args:
        root (Path): Prepared workspace and output destination.
        study (str): Inventory name with an installed fixture Graph.
        context (str): Explicit kubeconfig context; credentials stay in kubectl.

    Returns:
        dict[str, Any]: Runner measurements from real operator-created Jobs.
    """
    config = json.loads((root / "inputs" / f"{study}.json").read_text())
    namespace, graph_name = config["namespace"], config["graph"]
    base = ["kubectl", "--context", context, "--namespace", namespace, "--request-timeout=20s"]
    log_path = root / "logs" / f"{study}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    output = root / "outputs" / study
    output.mkdir(parents=True, exist_ok=True)

    def command(*arguments: str) -> str:
        completed = subprocess.run([*base, *arguments], capture_output=True, text=True, timeout=30, check=False)
        with log_path.open("a") as stream:
            stream.write(f"[{datetime.now(UTC).isoformat()}] kubectl {' '.join(arguments)}\n")
            stream.write(completed.stdout + completed.stderr + "\n")
        if completed.returncode:
            raise RuntimeError(f"kubectl failed; see {log_path}")
        return completed.stdout

    def get(kind: str, name: str) -> dict[str, Any]:
        return json.loads(command("get", kind, name, "-o", "json"))  # type: ignore[no-any-return]

    graph = get("graph", graph_name)
    write_json(output / "graph-before.json", graph)
    definitions = {}
    for kind, names in config["definitions"].items():
        for name in names:
            definition = get(kind, name)
            definitions[kind, name] = definition
            write_json(output / f"{kind}-{name}.json", definition)
    pods = json.loads(command("get", "pods", "-l", config["fixtureSelector"], "-o", "json"))["items"]
    pods = [
        pod
        for pod in pods
        if not pod["metadata"].get("deletionTimestamp")
        and any(item.get("type") == "Ready" and item.get("status") == "True" for item in pod.get("status", {}).get("conditions", []))
    ]
    if not pods:
        raise ValueError("the study requires a ready fixture Pod matching fixtureSelector")
    pods.sort(key=lambda pod: pod["metadata"]["name"])
    write_json(
        output / "fixture.json",
        [
            {
                "name": pod["metadata"]["name"],
                "node": pod["spec"].get("nodeName"),
                "images": pod.get("status", {}).get("containerStatuses", []),
            }
            for pod in pods
        ],
    )
    run_id = config["runId"]
    write_json(output / "submission.json", {"runId": run_id, "graph": graph_name, "namespace": namespace})
    response = command(
        "exec",
        pods[0]["metadata"]["name"],
        "-c",
        "fixture",
        "--",
        "polyad-benchmarks",
        "start",
        "--graph",
        graph_name,
        "--graph-uid",
        graph["metadata"]["uid"],
        "--run-id",
        run_id,
    )
    receipt = json.loads(response)
    write_json(output / "receipt.json", receipt)
    deadline = time.monotonic() + config["timeoutSeconds"]
    while time.monotonic() < deadline:
        resource = get("activation", receipt["name"])
        write_json(output / "activation.json", resource)
        status = resource.get("status", {})
        if status.get("phase") in TERMINAL:
            execution = status.get("execution", {})
            if execution.get("kind") != "Job":
                raise ValueError("runner reached a terminal phase without a Job")
            text = command("logs", "job/" + execution["name"], "-c", "runner")
            (output / "runner.log").write_text(text)
            result = None
            for line in reversed(text.splitlines()):
                try:
                    candidate = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Container logs merge stdout/stderr: a late structured event
                # must not replace the final measurement document.
                if isinstance(candidate, dict) and {"submitted", "skipped", "interrupted", "phases"} <= candidate.keys():
                    result = candidate
                    break
            if result is None:
                raise ValueError("runner logs contain no complete measurement document")
            write_json(output / "results.json", result)
            if result.get("runId") != run_id:
                raise ValueError("runner results belong to a different run identity")
            after = get("graph", graph_name)
            write_json(output / "graph-after.json", after)
            if (after["metadata"]["uid"], after["metadata"]["generation"]) != (graph["metadata"]["uid"], graph["metadata"]["generation"]):
                raise ValueError("fixture graph intent changed during the study")
            for (kind, name), before in definitions.items():
                current = get(kind, name)
                write_json(output / f"{kind}-{name}-after.json", current)
                if current["metadata"]["uid"] != before["metadata"]["uid"] or current["spec"] != before["spec"]:
                    raise ValueError("fixture definition or plan changed during the study")
            if status["phase"] != "Completed":
                raise ValueError("runner did not complete successfully; measurements retained")
            return result
        time.sleep(config["pollIntervalSeconds"])
    raise TimeoutError("study timed out; live work is retained for inspection and explicit cleanup")


def study_phase(project: Path, root: Path, study: str, context: str) -> None:
    """
    Keep each matrix job's status and diagnostics separate, even when execution fails.

    Args:
        project (Path): Restored source repository.
        root (Path): Prepared workspace.
        study (str): Study inventory entry.
        context (str): Explicit Kubernetes context.

    Returns:
        None: Failed studies retain diagnostics and raise for CI.
    """
    if study not in selected_studies(root) or (study in STUDIES and not context):
        raise ValueError("select a prepared study; cloud studies also require an explicit Kubernetes context")
    if (root / "statuses" / f"{study}.json").exists():
        raise ValueError("study already attempted; prepare a new refresh directory")

    # Start pessimistically. A failed measurement or renderer keeps diagnostics
    # but cannot masquerade as a successfully completed study during finish.
    status: dict[str, Any] = {"study": study, "success": False, "startedAt": datetime.now(UTC).isoformat()}
    try:
        verify_inputs(project, root)
        if study not in PROCESS_STUDIES:
            from polyad_benchmarks.studies.plotting import renderer

            # Discover a missing optional dependency before any cluster action.
            renderer(study)

        # Import only the requested execution backend. Local Cheeger experiments
        # need the production solver but neither a cluster nor process orchestration.
        if study in PROCESS_STUDIES:
            from polyad_benchmarks.studies.runner import run as run_processes

            run_processes(project, root, study)
        elif study == "cheeger-strategies":
            from polyad_benchmarks.studies.cheeger_strategies.experiment import run as run_strategies

            run_strategies(root)
        elif study in LOCAL_STUDIES:
            from polyad_benchmarks.reachability import run

            run(root, study)
        else:
            cluster_study(root, study, context)

        # Measurements are persisted before rendering. Figure generation consumes
        # those observations rather than collecting another set of timings.
        if study not in PROCESS_STUDIES:
            from polyad_benchmarks.studies.plotting import render

            output = root / "outputs" / study
            result = json.loads((output / "results.json").read_text())
            render(study, result, output)
            write_json(output / "results.json", result)

        status["success"] = True
    except Exception as error:
        status["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        status["finishedAt"] = datetime.now(UTC).isoformat()

        # Save failure status too, so finish can explain why publication is blocked.
        write_json(root / "statuses" / f"{study}.json", status)


def finish(project: Path, root: Path, publish: bool = False) -> None:
    """
    Publish only a complete successful matrix while retaining every raw run artifact.

    Args:
        project (Path): Source repository containing study publication directories.
        root (Path): Merged prepared and study artifacts.
        publish (bool): Also copy verified measurements into studies/NAME/results.json.

    Returns:
        None: Missing, unexpected or failed results prevent publication.
    """

    # Validate the entire matrix before copying any study into its published
    # directory. Partial success must not replace a previous complete result set.
    verify_inputs(project, root)
    paths = list((root / "statuses").glob("*.json"))
    if {path.stem for path in paths} != set(selected_studies(root)):
        raise ValueError("study matrix is incomplete or contains unexpected results")
    results = {}
    from polyad_benchmarks.studies.plotting import verify as verify_figures

    # Names alone are insufficient: each result must belong to the prepared
    # run identity and satisfy its local or cloud completion requirements.
    for path in paths:
        status = json.loads(path.read_text())
        if status.get("study") != path.stem or status.get("success") is not True:
            raise ValueError("failed or mislabeled study cannot be published")
        result = json.loads((root / "outputs" / path.stem / "results.json").read_text())
        expected = json.loads((root / "inputs" / f"{path.stem}.json").read_text())["runId"]
        if result.get("runId") != expected:
            raise ValueError("results from a different run identity cannot be published")
        if path.stem in LOCAL_STUDIES:
            if result.get("study") != path.stem or result.get("complete") is not True or not result.get("records"):
                raise ValueError("incomplete local measurements cannot be published")
            if path.stem in PROCESS_STUDIES:
                from polyad_benchmarks.studies.artifacts import verify

                recipe = json.loads((root / "inputs" / f"{path.stem}.json").read_text())
                verify(result, recipe, root / "outputs" / path.stem)
        elif (
            not result["submitted"] or result["skipped"] or result["interrupted"] or result["phases"] != {"Completed": result["submitted"]}
        ):
            raise ValueError("incomplete measurements cannot be published as a successful benchmark")

        # Figure inventories and hashes prevent missing or altered plots from
        # being published alongside otherwise successful numerical measurements.
        verify_figures(path.stem, result, root / "outputs" / path.stem)
        results[path.stem] = result

    write_json(root / "summary.json", {"provenance": json.loads((root / "provenance.json").read_text()), "studies": results})

    # Verification alone leaves published studies untouched. Copy only when the
    # caller explicitly requests publication, retaining the raw run directory.
    if publish:
        for name in results:
            destination = project / "studies" / name / "results.json"
            if name in PROCESS_STUDIES:
                from polyad_benchmarks.studies.artifacts import compact

                write_json(
                    destination,
                    {"provenance": json.loads((root / "provenance.json").read_text()), "studies": {name: compact(results[name])}},
                )
            else:
                shutil.copyfile(root / "summary.json", destination)
            artifacts = project / "studies" / name / "figures"
            artifacts.mkdir(exist_ok=True)
            for filename in results[name]["figures"]:
                shutil.copyfile(root / "outputs" / name / filename, artifacts / filename)


def main() -> None:
    """
    Expose the same prepare, study and finish phases to local commands and CI.

    Returns:
        None: Write CI matrix outputs during prepare and fail on incomplete experiments.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ci-phase", choices=("prepare", "study", "finish"), required=True)
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--study", choices=STUDIES + LOCAL_STUDIES, default="load")
    parser.add_argument(
        "--suite",
        choices=("cluster", "local", "reachability", "cheeger", "process", "all"),
        default="cluster",
        help="Study inventory selected during prepare",
    )
    parser.add_argument("--context", default="")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()

    # The same phase functions serve local commands and CI. Preparation emits
    # the selected matrix so worker jobs cannot invent a different study inventory.
    try:
        if args.ci_phase == "prepare":
            studies = {
                "cluster": STUDIES,
                "local": LOCAL_STUDIES,
                "reachability": REACHABILITY_STUDIES,
                "cheeger": CHEEGER_STUDIES,
                "process": PROCESS_STUDIES,
                "all": STUDIES + LOCAL_STUDIES,
            }[args.suite]
            provenance = prepare(args.project, args.root, studies)
            if destination := os.environ.get("GITHUB_OUTPUT"):
                with Path(destination).open("a") as stream:
                    stream.write(f"matrix={json.dumps(provenance['matrix'])}\nrevision={provenance['revision']}\n")
            print(json.dumps(provenance["matrix"]))
        elif args.ci_phase == "study":
            study_phase(args.project, args.root, args.study, args.context)
        else:
            finish(args.project, args.root, args.publish)
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"Refresh failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
