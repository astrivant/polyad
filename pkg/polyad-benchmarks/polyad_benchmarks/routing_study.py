"""
Exercise predicted queue envelopes with a real producer and two consumer processes.
"""

from __future__ import annotations

import multiprocessing
import queue
import time
from dataclasses import replace
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.models import Environment
from polyad_sdk.symbiosis.reachability import Observation, ReachabilityStrategy, compile_envelope

if TYPE_CHECKING:
    from multiprocessing.queues import Queue
    from typing import Any

    from polyad_sdk.symbiosis.reachability import QueueModel

__all__ = (
    "consume",
    "json_artifact",
    "produce",
    "rerouting",
    "trial",
)


def produce(output: Queue[Any], count: int, rate: float, start: Any) -> None:
    """
    Offer a finite, paced stream of uniquely identified jobs from its own process.

    Args:
        output (Queue[Any]): Bounded ingress to the study coordinator.
        count (int): Number of jobs to offer.
        rate (float): Jobs per second.
        start (Any): Shared readiness event.

    Returns:
        None: End the stream with a sentinel after all offered jobs.
    """
    start.wait()
    began = time.monotonic()
    for identity in range(count):
        time.sleep(max(0, began + identity / rate - time.monotonic()))
        output.put((identity, time.monotonic()))
    output.put(None)


def consume(index: int, incoming: Queue[Any], results: Queue[Any], capacity: float) -> None:
    """
    Process accepted jobs serially, returning their stable identity and verified square.

    Args:
        index (int): Consumer identity.
        incoming (Queue[Any]): Bounded work queue.
        results (Queue[Any]): Shared readiness and completion channel.
        capacity (float): Nominal work units per second, emulated with a service delay.

    Returns:
        None: Exit only after the parent sends a drain sentinel.
    """
    results.put(("ready", index))
    while (job := incoming.get()) is not None:
        identity, offered = job
        time.sleep(1 / capacity)
        results.put(("completed", index, identity, identity * identity, time.monotonic() - offered))


def trial(model: QueueModel, count: int, rate: float, horizon: float, guarded: bool) -> dict[str, Any]:
    """
    Compare baseline and admitted rerouting with the same workload and processing budget.

    Args:
        model (QueueModel): Exactly two consumers and their queue contract.
        count (int): Number of original jobs; all receive a completion or explicit rejection.
        rate (float): Nominal offered rate in jobs per second.
        horizon (float): Prediction horizon from the initial state.
        guarded (bool): Admit the proposed split through the SDK guard; baseline uses the first consumer.

    Returns:
        dict[str, Any]: Prediction, completions, rejections, latency, peaks and cleanup evidence.
    """
    if type(count) is not int or not 1 <= count <= 1000 or not 0 < rate <= 1000 or count / rate > 30:
        raise ValueError("use at most 1000 jobs, positive bounded rates and at most 30 seconds of arrivals")
    if len(model.names) != 2 or model.warmup_max or min(model.effective_capacities) <= 0 or min(model.limits) < 1:
        raise ValueError("process trials require two ready consumers with positive capacity and queue limits")
    if not model.arrival_bounds[0] <= rate <= model.arrival_bounds[1]:
        raise ValueError("offered rate lies outside the modeled arrival bounds")

    # Hold consumer capacity fixed; only guarded routing can use the spare consumer's share.
    selected = model if guarded else replace(model, shares=(1.0, 0.0))
    envelope = compile_envelope(selected, "process-trial", horizon=horizon)
    predicted, margin = envelope.assess((0, 0))
    pending: list[set[int]] = [set(), set()]
    checks: dict[str, int] = {}
    view = Environment(None, {}, {}, True, None)
    strategy = ReachabilityStrategy(
        "route",
        lambda _: None,
        artifact=lambda: envelope,
        observe=lambda _: Observation(
            tuple(float(len(items)) for items in pending), (1, 1), time.time(), envelope.revision, selected.fingerprint
        ),
    )
    context = multiprocessing.get_context("spawn")
    ingress = context.Queue(maxsize=count + 1)
    work = [context.Queue(maxsize=int(limit)) for limit in model.limits]
    results = context.Queue(maxsize=count + 2)
    start = context.Event()
    consumers = [
        context.Process(target=consume, args=(i, work[i], results, capacity)) for i, capacity in enumerate(model.effective_capacities)
    ]
    producer = context.Process(target=produce, args=(ingress, count, rate, start))
    processes = [*consumers, producer]
    completed: set[int] = set()
    rejected: set[int] = set()
    latencies, peaks, routed = [], [0, 0], [0, 0]
    deadline = time.monotonic() + 60
    began = time.monotonic()
    try:
        for process in processes:
            process.start()
        ready = {results.get(timeout=10)[1] for _ in consumers}
        if ready != {0, 1}:
            raise RuntimeError("consumers did not become ready")
        began = time.monotonic()
        start.set()
        offered_done = False
        while not offered_done or any(pending):
            if time.monotonic() > deadline or any(process.exitcode not in (None, 0) for process in processes):
                raise RuntimeError("process study failed or exceeded its deadline")
            while True:
                try:
                    _, index, identity, value, latency = results.get_nowait()
                except queue.Empty:
                    break
                if identity not in pending[index] or identity in completed or value != identity * identity:
                    raise ValueError("duplicate, unowned or incorrect result")
                pending[index].remove(identity)
                completed.add(identity)
                latencies.append(latency)
            if not offered_done:
                try:
                    job = ingress.get(timeout=0.002)
                except queue.Empty:
                    continue
                if job is None:
                    offered_done = True
                    continue
                identity = job[0]
                assessment = strategy.evaluate(view)
                checks[assessment.state] = checks.get(assessment.state, 0) + 1
                index = min((i for i, share in enumerate(selected.shares) if share), key=lambda i: (routed[i] + 1) / selected.shares[i])
                if (guarded and not assessment.satisfied) or len(pending[index]) >= model.limits[index]:
                    rejected.add(identity)
                    continue
                work[index].put(job, timeout=1)
                routed[index] += 1
                pending[index].add(identity)
                peaks[index] = max(peaks[index], len(pending[index]))
            else:
                time.sleep(0.002)
        elapsed = time.monotonic() - began
        for channel in work:
            channel.put(None, timeout=1)
        for process in processes:
            process.join(timeout=max(0, deadline - time.monotonic()))
        if any(process.is_alive() or process.exitcode != 0 for process in processes):
            raise RuntimeError("process tree did not drain successfully")
        if completed & rejected or completed | rejected != set(range(count)):
            raise ValueError("original work was lost or counted more than once")
        return {
            "mode": "guarded-rerouting" if guarded else "first-consumer",
            "offered": count,
            "predictedAllowed": predicted,
            "predictedMargin": margin,
            "artifact": json_artifact(envelope.dumps()),
            "completed": len(completed),
            "rejected": len(rejected),
            "routed": routed,
            "peakOutstanding": peaks,
            "elapsedSeconds": elapsed,
            "meanLatencySeconds": sum(latencies) / len(latencies) if latencies else None,
            "assessments": checks,
            "allJoined": True,
            "consumerProcesses": 2,
            "producerProcesses": 1,
            "evidence": "paced discrete jobs with real process scheduling; compare observations with the fluid approximation",
        }
    finally:
        for process in processes:
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=1)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
        for channel in (ingress, *work, results):
            channel.cancel_join_thread()
            channel.close()


def json_artifact(source: str) -> dict[str, Any]:
    """
    Embed an envelope's portable JSON object in study evidence.

    Args:
        source (str): Validated envelope JSON.

    Returns:
        dict[str, Any]: Structured artifact suitable for the refresh output.
    """
    import json

    result: dict[str, Any] = json.loads(source)
    return result


def rerouting(config: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Repeat paired baseline and guard-controlled process experiments.

    Args:
        config (dict[str, Any]): Prepared physical rates, model and repeat count.

    Returns:
        list[dict[str, Any]]: Results labeled by repetition and execution order.
    """
    from polyad_benchmarks.reachability import model_from_config

    repetitions = config["repetitions"]
    if type(repetitions) is not int or not 1 <= repetitions <= 10:
        raise ValueError("use one to ten paired process trials")
    model = model_from_config(config)
    records = []
    for repetition in range(repetitions):
        for guarded in (False, True) if repetition % 2 == 0 else (True, False):
            record = trial(model, config["jobs"], config["rate"], config["horizon"], guarded)
            record["repetition"] = repetition
            records.append(record)
    return records
