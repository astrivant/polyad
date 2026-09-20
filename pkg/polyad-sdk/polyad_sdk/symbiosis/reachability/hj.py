"""
Run optional Hamilton-Jacobi queue-safety studies in a bounded child process.
"""

from __future__ import annotations

import math
import multiprocessing
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.reachability.models import finite

if TYPE_CHECKING:
    from typing import Any

    from polyad_sdk.symbiosis.reachability.models import QueueModel

__all__ = (
    "AnalysisBudget",
    "DEFAULT_BUDGET",
    "analyze",
)


@dataclass(frozen=True)
class AnalysisBudget:
    """
    Bound numerical resolution, estimated workspace and child-process lifetime.

    The workspace estimate includes state arrays and a conservative allowance
    for numerical temporaries. JAX compilation/runtime overhead is additional;
    use a container memory limit for a hard resident-memory ceiling.

    Attributes:
        points_per_axis (int): Uniform grid resolution, including both domain endpoints.
        max_points (int): Maximum product of all axis sizes, checked before importing JAX.
        max_workspace_bytes (int): Maximum estimated numerical workspace, excluding the runtime.
        timeout_seconds (float): Wall deadline including child startup and JIT compilation.
        accuracy (str): Upstream low or medium finite-difference/integration setting.
    """

    points_per_axis: int = 17
    max_points: int = 100000
    max_workspace_bytes: int = 256 * 1024 * 1024
    timeout_seconds: float = 60
    accuracy: str = "low"

    def __post_init__(self) -> None:
        """
        Require finite positive computation limits.

        Returns:
            None: Invalid settings raise ValueError before worker creation.
        """
        for value in (self.points_per_axis, self.max_points, self.max_workspace_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("grid and memory budgets must be positive integers")
        finite(self.timeout_seconds, minimum=0.001)
        if not 5 <= self.points_per_axis <= 257 or self.timeout_seconds > 3600 or self.accuracy not in {"low", "medium"}:
            raise ValueError("use 5..257 points, at most one hour, and low or medium accuracy")

    def estimate(self, dimensions: int) -> dict[str, int]:
        """
        Reject an oversized grid before importing the optional numerical backend.

        Args:
            dimensions (int): Number of queue and clock state axes.

        Returns:
            dict[str, int]: Grid points, one float32 value array and estimated workspace bytes.
        """
        if type(dimensions) is not int or not 1 <= dimensions <= 3:
            raise ValueError("this backend supports one to three state axes")

        # Grid cost grows exponentially with state dimensions. Reject oversized
        # requests before importing JAX or allocating numerical workspace.
        points = self.points_per_axis**dimensions
        workspace = points * 4 * (24 + 4 * dimensions)
        if points > self.max_points or workspace > self.max_workspace_bytes:
            raise ValueError("requested reachability grid exceeds its point or workspace budget")
        return {"gridPoints": points, "valueBytes": 4 * points, "estimatedWorkspaceBytes": workspace}


DEFAULT_BUDGET = AnalysisBudget()


def analyze(
    model: QueueModel,
    *,
    horizon: float = 10,
    budget: AnalysisBudget = DEFAULT_BUDGET,
    samples: tuple[tuple[float, ...], ...] = (),
) -> dict[str, Any]:
    """
    Compute a numerical backward reachable tube for queue overflow.

    This studies fixed routing at the worst allowed arrival rate. The child
    imports JAX and returns diagnostics, never an admission certificate. The
    service guard uses the independently derived analytic queue bound.

    Args:
        model (QueueModel): Queue dynamics and optional consumer warmup clock.
        horizon (float): Safety horizon in seconds.
        budget (AnalysisBudget): Grid, workspace and wall-time ceilings.
        samples (tuple[tuple[float, ...], ...]): Up to 256 states to compare with the analytic bound.

    Returns:
        dict[str, Any]: Timings, peak RSS, solver versions, numerical and analytic sample results.
    """
    finite(horizon, minimum=0.001)
    if horizon > 3600 or len(samples) > 256:
        raise ValueError("limit the horizon to one hour and samples to 256")
    estimate = budget.estimate(len(model.domain))
    for sample in samples:
        model.bounds(sample, horizon)
        if any(value > upper for value, upper in zip(sample, model.domain, strict=True)):
            raise ValueError("numerical sample lies outside the modeled domain")
    from polyad_sdk.symbiosis.reachability._numerical import compute

    # Isolate numerical work in a spawned process so the parent can enforce a
    # deadline even when a native numerical kernel does not cooperate with cancellation.
    context = multiprocessing.get_context("spawn")
    receive, send = context.Pipe(duplex=False)
    worker = context.Process(target=compute, args=(send, model, horizon, budget, samples))
    started = time.monotonic()
    try:
        worker.start()
        send.close()
        if not receive.poll(max(0, budget.timeout_seconds - (time.monotonic() - started))):
            raise TimeoutError("reachability analysis exceeded its wall deadline")
        try:
            result: dict[str, Any] = receive.recv()
        except EOFError as error:
            raise RuntimeError("reachability worker exited without a result") from error
        if "error" in result:
            raise RuntimeError(result["error"])
        worker.join(timeout=max(0, budget.timeout_seconds - (time.monotonic() - started)))
        if worker.is_alive():
            raise TimeoutError("reachability worker did not stop within its deadline")
        if worker.exitcode != 0:
            raise RuntimeError("reachability worker failed after returning a result")

        # A returned payload is insufficient: require clean worker exit and finite
        # sample margins before presenting the numerical analysis as successful.
        result.update(estimate, wallSeconds=time.monotonic() - started, fingerprint=model.fingerprint)
        if not all(math.isfinite(item["numericalMargin"]) for item in result["samples"]):
            raise RuntimeError("numerical solver produced nonfinite values")
        return result
    finally:
        if worker.pid is not None:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=1)
            if worker.is_alive():
                worker.kill()
                worker.join()
            worker.close()
        receive.close()
        send.close()
