"""
Keep optional JAX imports and numerical state inside the analysis process.
"""

from __future__ import annotations

import importlib
import os
import sys
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from typing import Any

    from polyad_sdk.symbiosis.reachability.hj import AnalysisBudget
    from polyad_sdk.symbiosis.reachability.models import QueueModel


def compute(
    send: Connection,
    model: QueueModel,
    horizon: float,
    budget: AnalysisBudget,
    samples: tuple[tuple[float, ...], ...],
) -> None:
    """
    Solve the worst-arrival fixed-routing dynamics and return bounded diagnostics.

    Args:
        send (Connection): Parent pipe for the result or a readable error.
        model (QueueModel): Validated fluid model with one to three state axes.
        horizon (float): Time horizon in seconds.
        budget (AnalysisBudget): Validated numerical resolution and accuracy.
        samples (tuple[tuple[float, ...], ...]): States for numerical/analytic comparison.

    Returns:
        None: Send diagnostics, close the pipe and let the process exit.
    """
    try:
        started = time.perf_counter()

        # Keep this initial study backend on CPU with the float32 storage used
        # by its preflight budget, regardless of the service's JAX environment.
        os.environ["JAX_PLATFORMS"] = "cpu"
        os.environ["JAX_ENABLE_X64"] = "false"
        hj = importlib.import_module("hj_reachability")
        jax = importlib.import_module("jax")
        jnp = importlib.import_module("jax.numpy")
        np = importlib.import_module("numpy")
        imports_seconds = time.perf_counter() - started
        count = len(model.names)
        arrivals = jnp.asarray(model.shares) * model.arrival_bounds[1]
        capacities = jnp.asarray(model.effective_capacities)

        class FixedRouting(hj.Dynamics):  # type: ignore[misc, name-defined]
            """
            Evaluate deterministic worst-arrival dynamics with an optional readiness clock.
            """

            def __call__(self, state: Any, control: Any, disturbance: Any, time: Any) -> Any:
                """
                Advance queues, reflecting empty queues at zero.

                Args:
                    state (Any): JAX state vector in model axis order.
                    control (Any): Singleton control already selected by the model's shares.
                    disturbance (Any): Singleton worst-arrival disturbance.
                    time (Any): Solver time; these dynamics are autonomous.

                Returns:
                    Any: State derivative vector.
                """
                rates = capacities
                if model.warmup_max:
                    rates = rates.at[-1].set(jnp.where(state[-1] > 0, 0, rates[-1]))
                drift = arrivals - rates
                queues = jnp.where(state[:count] > 0, drift, jnp.maximum(drift, 0))
                return jnp.concatenate((queues, jnp.asarray([jnp.where(state[-1] > 0, -1.0, 0.0)]))) if model.warmup_max else queues

            def optimal_control_and_disturbance(self, state: Any, time: Any, grad_value: Any) -> tuple[Any, Any]:
                """
                Return the fixed routing and analytically selected worst arrival.

                Args:
                    state (Any): State vector.
                    time (Any): Solver time.
                    grad_value (Any): Numerical gradient, unused for a singleton choice.

                Returns:
                    tuple[Any, Any]: Singleton control and disturbance vectors.
                """
                return jnp.zeros(1), jnp.zeros(1)

            def partial_max_magnitudes(self, state: Any, time: Any, value: Any, grad_value_box: Any) -> Any:
                """
                Bound derivative magnitudes for the solver's numerical dissipation.

                Args:
                    state (Any): State vector.
                    time (Any): Solver time.
                    value (Any): Current value function.
                    grad_value_box (Any): Gradient bounds.

                Returns:
                    Any: Conservative speed bound for each axis.
                """
                speeds = arrivals + capacities
                return jnp.concatenate((speeds, jnp.ones(1))) if model.warmup_max else speeds

        box = hj.sets.Box(jnp.zeros(1), jnp.zeros(1))
        dynamics = FixedRouting("max", "min", box, box)

        # Include genuinely unsafe states beyond the queue ceilings. A grid
        # ending exactly at the unsafe boundary has no negative target values
        # and can incorrectly erase backward propagation of overflow risk.
        domain = tuple(
            limit + max(limit * 0.25, model.arrival_bounds[1] * share * horizon)
            for limit, share in zip(model.limits, model.shares, strict=True)
        )
        if model.warmup_max:
            domain = (*domain, model.warmup_max)
        grid = hj.Grid.from_lattice_parameters_and_boundary_conditions(
            hj.sets.Box(jnp.zeros(len(domain)), jnp.asarray(domain)),
            (budget.points_per_axis,) * len(model.domain),
        )
        initial = jnp.min(jnp.asarray(model.limits) - grid.states[..., :count], axis=-1)
        settings = hj.SolverSettings.with_accuracy(budget.accuracy, hamiltonian_postprocessor=hj.solver.backwards_reachable_tube)
        started = time.perf_counter()
        values = hj.step(settings, dynamics, grid, 0.0, initial, -horizon, progress_bar=False)
        values.block_until_ready()
        solve_seconds = time.perf_counter() - started
        array = np.asarray(values)
        if not np.isfinite(array).all():
            raise ValueError("solver returned nonfinite grid values")
        comparison = []
        for state in samples:
            peak, _ = model.bounds(state, horizon)
            comparison.append(
                {
                    "state": state,
                    "numericalMargin": float(grid.interpolate(values, jnp.asarray(state))),
                    "analyticMargin": min(limit - value for limit, value in zip(model.limits, peak, strict=True)),
                }
            )
        rss = None
        cpu_seconds = None
        if sys.platform != "win32":
            resource = importlib.import_module("resource")
            usage = resource.getrusage(resource.RUSAGE_SELF)
            rss = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
            cpu_seconds = usage.ru_utime + usage.ru_stime
        send.send(
            {
                "backend": "hj_reachability",
                "versions": {"hj_reachability": hj.__version__, "jax": jax.__version__},
                "importSeconds": imports_seconds,
                "solveSeconds": solve_seconds,
                "peakRssBytes": rss,
                "cpuSeconds": cpu_seconds,
                "platform": "cpu",
                "dtype": str(array.dtype),
                "positiveGridFraction": float(np.mean(array > 0)),
                "computationalDomain": domain,
                "samples": comparison,
                "evidence": "numerical queue-overflow study; terminal targets are checked analytically",
            }
        )
    except Exception as error:
        send.send({"error": f"{type(error).__name__}: {error}. Install polyad-sdk[reachability] for numerical studies."})
    finally:
        send.close()
