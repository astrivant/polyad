"""
Compare interaction models, state representations and numerical queue envelopes.
"""

from __future__ import annotations

import json
import platform
import time
from dataclasses import asdict, replace
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.models import Environment
from polyad_sdk.symbiosis.reachability import Interaction, Observation, QueueModel, ReachabilityStrategy, Relationship, compile_envelope
from polyad_sdk.symbiosis.reachability.hj import AnalysisBudget, analyze

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any


def model_from_config(config: dict[str, Any]) -> QueueModel:
    """
    Load a scenario's explicit two-consumer model with validated numerical fields.

    Args:
        config (dict[str, Any]): Prepared scenario containing the model mapping.

    Returns:
        QueueModel: Fixed routing and work-unit contract shared by each experiment.
    """
    values = dict(config["model"])
    for name in ("names", "capacities", "limits", "targets", "arrival_bounds", "shares"):
        values[name] = tuple(values[name])
    return QueueModel(**values)


def measure_guard(model: QueueModel, horizon: float, iterations: int) -> dict[str, Any]:
    """
    Measure ordinary service-side guard calls separately from JAX startup and solve time.

    Args:
        model (QueueModel): Model whose closed-form envelope is evaluated.
        horizon (float): Contract duration in seconds.
        iterations (int): Bounded number of local evaluation calls.

    Returns:
        dict[str, Any]: Artifact bytes, assessment counts and average call duration.
    """
    if type(iterations) is not int or not 1 <= iterations <= 100000:
        raise ValueError("guard iterations must be between 1 and 100000")
    artifact = compile_envelope(model, "study-v1", horizon=horizon)
    now = time.time()
    state = (0.0,) * len(model.domain)
    observation = Observation(state, state, now, artifact.revision, model.fingerprint)
    strategy = ReachabilityStrategy(
        "queue-envelope", lambda _: None, artifact=lambda: artifact, observe=lambda _: observation, clock=lambda: now
    )
    view = Environment(None, {}, {}, True, None)
    counts: dict[str, int] = {}
    started = time.perf_counter()
    for _ in range(iterations):
        result = strategy.evaluate(view)
        counts[result.state] = counts.get(result.state, 0) + 1
    elapsed = time.perf_counter() - started
    return {
        "iterations": iterations,
        "meanSeconds": elapsed / iterations,
        "artifactBytes": len(artifact.dumps().encode()),
        "states": counts,
    }


def interactions(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Measure how signed relationship effects change queue safety under identical demand.

    Args:
        config (dict[str, Any]): Scenario with explicit relationship effects and numerical budgets.

    Returns:
        list[dict[str, Any]]: Model identities, analytic bounds and optional numerical diagnostics.
    """
    base = model_from_config(config)
    records = []
    for case in config["interactions"]:
        interaction = Interaction(Relationship(case["relationship"]), tuple(case["effects"]))
        model = replace(base, interaction=interaction)
        state = tuple(config["initialState"])
        envelope = compile_envelope(model, "study-v1", horizon=config["horizon"])
        allowed, margin = envelope.assess(state)
        record = {
            "relationship": interaction.relationship,
            "effects": interaction.effects,
            "model": asdict(model),
            "fingerprint": model.fingerprint,
            "allowed": allowed,
            "margin": margin,
            "guard": measure_guard(model, config["horizon"], config["guardIterations"]),
        }
        if config["numerical"]:
            record["numerical"] = analyze(model, horizon=config["horizon"], budget=AnalysisBudget(**config["budget"]), samples=(state,))
        records.append(record)
    return records


def state_variables(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Compare pooled capacity, separate queues and a readiness clock against one full model.

    Args:
        config (dict[str, Any]): Shared physical assumptions, observations and grid resolutions.

    Returns:
        list[dict[str, Any]]: Resource costs and classifications, including optimistic reduced models.
    """
    full = model_from_config(config)
    separate = replace(full, warmup_max=0)
    pooled = QueueModel(
        ("pooled",), (sum(full.capacities),), (sum(full.limits),), (sum(full.targets),), full.arrival_bounds, (1.0,), unit=full.unit
    )
    records = []
    for label, model in (("pooled", pooled), ("queues", separate), ("queues-and-readiness", full)):
        probes: list[dict[str, Any]] = []
        for raw in config["observations"]:
            state = tuple(raw)
            projection = (sum(state[:2]),) if label == "pooled" else state[: len(model.domain)]
            full_result = compile_envelope(full, "reference", horizon=config["horizon"]).assess(state)[0]
            reduced_result = compile_envelope(model, label, horizon=config["horizon"]).assess(projection)[0]
            probes.append({"fullState": state, "state": projection, "referenceAllowed": full_result, "allowed": reduced_result})
        for resolution in config["resolutions"]:
            budget = AnalysisBudget(**{**config["budget"], "points_per_axis": resolution})
            record = {
                "representation": label,
                "dimensions": len(model.domain),
                "model": asdict(model),
                "resolution": resolution,
                "resources": budget.estimate(len(model.domain)),
                "probes": probes,
                "optimisticCount": sum(item["allowed"] and not item["referenceAllowed"] for item in probes),
                "conservativeCount": sum(not item["allowed"] and item["referenceAllowed"] for item in probes),
                "guard": measure_guard(model, config["horizon"], config["guardIterations"]),
            }
            if config["numerical"]:
                record["numerical"] = analyze(
                    model, horizon=config["horizon"], budget=budget, samples=tuple(item["state"] for item in probes)
                )
            records.append(record)
    return records


def run(root: Path, study: str) -> dict[str, Any]:
    """
    Execute a prepared local study with correlated results and model artifacts.

    Args:
        root (Path): Prepared refresh directory containing immutable scenario snapshots.
        study (str): Registered local study name.

    Returns:
        dict[str, Any]: Complete records and local runtime identity, ready for publication checks.
    """
    from polyad_benchmarks.cheeger_reduction import reduction_study
    from polyad_benchmarks.refresh import write_json
    from polyad_benchmarks.routing_study import rerouting

    config = json.loads((root / "inputs" / f"{study}.json").read_text())
    runners = {
        "symbiosis": interactions,
        "reachability-state": state_variables,
        "reachability-routing": rerouting,
        "cheeger-reduction": reduction_study,
    }
    records = runners[study](config)
    result = {
        "study": study,
        "runId": config["runId"],
        "complete": True,
        "recipe": config,
        "records": records,
        "environment": {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine()},
    }
    write_json(root / "outputs" / study / "results.json", result)
    return result
