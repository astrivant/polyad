"""
Generate bounded arrivals and measure activation acceptance and convergence separately.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import signal
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from polyad_benchmarks.config import RunConfig, operator_client, request_prefix
from polyad_benchmarks.identity import grafana_path, log_event, new_run_id, parent_run_id, plan_hash
from polyad_sdk import APIError, Client

if TYPE_CHECKING:
    from collections.abc import Callable
    from concurrent.futures import Future
    from typing import Any

TERMINAL = {"Completed", "Failed", "Stopped", "Rejected", "Superseded", "Invalidated"}


def exercise(fixture_url: str, request_id: str, config: RunConfig, stop: threading.Event) -> dict[str, Any]:
    """
    Request one execution and observe its receipt without retrying an uncertain mutation.

    Args:
        fixture_url (str): Internal fixture Service URL.
        request_id (str): Stable identity retained even after transport failures.
        config (RunConfig): Request and polling deadlines.
        stop (threading.Event): Graceful termination signal.

    Returns:
        dict[str, Any]: Outcome, identity and measured latencies; never includes credentials.
    """
    started = time.monotonic()
    result: dict[str, Any] = {"requestId": request_id, "runId": parent_run_id(request_id), "phase": "Unknown"}
    try:
        # The fixture implements the activation submission route; only it submits to Polyad.
        fixture = Client(fixture_url, os.environ.get("POLYAD_BENCHMARK_FIXTURE_TOKEN") or None, timeout=config.request_timeout)
        fixture.activate(request_id=request_id, graph="fixture", graph_uid="fixture", node="batch")
        result["acceptanceSeconds"] = time.monotonic() - started
        client = operator_client(config.request_timeout)
        while not stop.is_set() and time.monotonic() - started < config.timeout:
            receipt = client.activation(request_id)
            phase = receipt.get("status", {}).get("phase", "Pending")
            if phase in TERMINAL:
                result["phase"] = phase
                break
            stop.wait(config.poll_interval)
        else:
            result["phase"] = "Cancelled" if stop.is_set() else "TimedOut"
    except APIError as error:
        result.update(phase="HttpError", httpStatus=error.status)
    except (OSError, ValueError, TimeoutError) as error:
        result.update(phase="TransportError", errorType=type(error).__name__)
    result["elapsedSeconds"] = time.monotonic() - started
    log_event("benchmark.request.finished", result["runId"], **{key: value for key, value in result.items() if key != "runId"})
    return result


def run(
    config: RunConfig,
    prefix: str,
    action: Callable[[str], dict[str, Any]],
    stop: threading.Event,
) -> dict[str, Any]:
    """
    Pace arrivals without building an unbounded executor queue or catching up missed bursts.

    Args:
        config (RunConfig): Validated experiment bounds.
        prefix (str): Stable run identity.
        action (Callable[[str], dict[str, Any]]): One bounded request and observation operation.
        stop (threading.Event): Stop scheduling new requests when set.

    Returns:
        dict[str, Any]: JSON-compatible measurements, including skipped offered arrivals.
    """
    request_prefix(prefix)
    started_at = datetime.now(UTC)
    started = time.monotonic()
    pending: set[Future[dict[str, Any]]] = set()
    outcomes: list[dict[str, Any]] = []
    scheduled = skipped = 0
    count = min(config.max_requests, math.ceil(config.duration * config.rate))
    with ThreadPoolExecutor(max_workers=config.concurrency, thread_name_prefix="load-study") as pool:
        for ordinal in range(count):
            due = started + ordinal / config.rate
            if stop.wait(max(0, due - time.monotonic())):
                break
            done = {future for future in pending if future.done()}
            outcomes.extend(future.result() for future in done)
            pending -= done
            scheduled += 1
            if len(pending) >= config.concurrency or time.monotonic() - due >= 1 / config.rate:
                skipped += 1
                continue
            pending.add(pool.submit(action, f"{prefix}-{ordinal:05d}"))
        outcomes.extend(future.result() for future in pending)
    phases = dict(Counter(item["phase"] for item in outcomes))
    completed = sorted(float(item["elapsedSeconds"]) for item in outcomes if item["phase"] == "Completed")
    acceptance = sorted(float(item["acceptanceSeconds"]) for item in outcomes if "acceptanceSeconds" in item)

    def p95(values: list[float]) -> float | None:
        return values[math.ceil(0.95 * len(values)) - 1] if values else None

    return {
        "runId": prefix,
        "startedAt": started_at.isoformat(),
        "finishedAt": datetime.now(UTC).isoformat(),
        "grafanaPath": grafana_path(
            prefix,
            os.environ.get("POLYAD_POD_NAMESPACE", "polyad"),
            start=int(started_at.timestamp() * 1000),
            end=int(time.time() * 1000),
        ),
        "scheduled": scheduled,
        "submitted": len(outcomes),
        "skipped": skipped,
        "interrupted": stop.is_set(),
        "elapsedSeconds": time.monotonic() - started,
        "phases": phases,
        "acceptanceP95Seconds": p95(acceptance),
        "completionP95Seconds": p95(completed),
        "requests": sorted(outcomes, key=lambda item: item["requestId"]),
    }


def main() -> None:
    """
    Start an explicit study activation or execute the runner inside its managed Job.

    Returns:
        None: Exit nonzero on incomplete work, skipped arrivals or interruption.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start", help="Activate the chart's runner node explicitly")
    start.add_argument("--graph", default="load-study")
    start.add_argument("--graph-uid", required=True)
    start.add_argument("--run-id", help="Reuse a previously generated ID only when retrying that same run")
    execute = commands.add_parser("run", help="Run inside a graph-managed workload")
    execute.add_argument("--fixture-url", default=os.environ.get("POLYAD_BENCHMARK_FIXTURE_URL"))
    execute.add_argument("--run-id", default=os.environ.get("POLYAD_ACTIVATION_ID"))
    execute.add_argument("--output", default="/tmp/results.json")
    execute.add_argument("--plan", default=os.environ.get("POLYAD_BENCHMARK_PLAN"))
    args = parser.parse_args()
    try:
        prefix = request_prefix(args.run_id if args.run_id is not None else new_run_id())
        if args.command == "start":
            log_event("benchmark.run.submitting", prefix, graph=args.graph, graphUid=args.graph_uid)
            receipt = operator_client(10).activate(request_id=prefix, graph=args.graph, graph_uid=args.graph_uid, node="runner")
            receipt.update(runId=prefix, grafanaPath=grafana_path(prefix, receipt.get("namespace", "polyad")))
            log_event("benchmark.run.submitted", prefix, requestId=prefix, receiptName=receipt.get("name"))
            print(json.dumps(receipt), flush=True)
            return
        plan_source = (
            Path(args.plan).read_text() if args.plan else json.dumps({"run": json.loads(os.environ.get("POLYAD_BENCHMARK_CONFIG", "{}"))})
        )
        plan = json.loads(plan_source)
        config = RunConfig(**plan["run"])
        if not args.fixture_url:
            raise ValueError("POLYAD_BENCHMARK_FIXTURE_URL or --fixture-url is required")
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        log_event(
            "benchmark.run.started",
            prefix,
            planHash=plan_hash(plan_source),
            configuration=asdict(config),
            replicas=plan.get("replicas", {}),
        )
        result = run(config, prefix, lambda request_id: exercise(args.fixture_url, request_id, config, stop), stop)
        result["plan"] = plan
        result["planHash"] = plan_hash(plan_source)
        result["configuration"] = asdict(config)
        result["graph"] = {key: os.environ.get(key, "") for key in ("POLYAD_GRAPH_NAME", "POLYAD_GRAPH_UID", "POLYAD_POD_NAME")}
        log_event("benchmark.run.finished", prefix, submitted=result["submitted"], skipped=result["skipped"], phases=result["phases"])
        serialized = json.dumps(result, sort_keys=True)
        Path(args.output).write_text(serialized + "\n")
        print(serialized, flush=True)
        if result["interrupted"] or result["skipped"] or result["phases"].get("Completed", 0) != result["submitted"]:
            raise SystemExit(1)
    except (ValueError, TypeError, KeyError, OSError, APIError) as error:
        parser.exit(2, f"Load study failed: {type(error).__name__}\n")


if __name__ == "__main__":
    main()
