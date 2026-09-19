"""
Benchmark exact Cheeger cut enumeration at supported graph boundary sizes.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import time
from typing import TYPE_CHECKING

import networkx as nx

from polyad.graph.cheeger import compute_cheeger

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Any


def measure(sizes: Sequence[int], repeats: int) -> list[dict[str, Any]]:
    """
    Time complete path-graph searches with deterministic cut counts and answers.

    Args:
        sizes (Sequence[int]): Vertex counts from two through the supported default ceiling.
        repeats (int): Independent measurements per size.

    Returns:
        list[dict[str, Any]]: Timing distribution, throughput and exact-result evidence.
    """
    records = []
    for vertices in sizes:
        graph = nx.path_graph(vertices)
        expected_cuts = (1 << (vertices - 1)) - 1
        elapsed = []
        for _ in range(repeats):
            started = time.perf_counter()
            result = compute_cheeger(graph)
            elapsed.append(time.perf_counter() - started)
            if not result.exact or result.evaluatedCuts != expected_cuts or result.upperBound != 1 / (vertices // 2):
                raise RuntimeError("Cheeger benchmark produced an unexpected result")
        median = statistics.median(elapsed)
        records.append(
            {
                "vertices": vertices,
                "cuts": expected_cuts,
                "minimumSeconds": min(elapsed),
                "medianSeconds": median,
                "maximumSeconds": max(elapsed),
                "medianCutsPerSecond": expected_cuts / median,
            }
        )
    return records


def main(argv: Sequence[str] | None = None) -> None:
    """
    Parse bounded benchmark settings and print machine-readable measurements.

    Args:
        argv (Sequence[str] | None): Explicit arguments, or None for process arguments.

    Returns:
        None: JSON measurements are written to standard output.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", type=int, nargs="+", default=(12, 16, 18, 20))
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--maximum-seconds", type=float)
    args = parser.parse_args(argv)
    if not args.sizes or any(type(value) is not int or not 2 <= value <= 20 for value in args.sizes):
        parser.error("sizes must be integers from 2 through the supported default ceiling of 20")
    if not 1 <= args.repeats <= 20:
        parser.error("repeats must be from 1 through 20")
    if args.maximum_seconds is not None and (not math.isfinite(args.maximum_seconds) or args.maximum_seconds <= 0):
        parser.error("maximum-seconds must be a positive finite number")
    records = measure(args.sizes, args.repeats)
    print(json.dumps({"implementation": "python-int-bit-count", "records": records}, indent=2, sort_keys=True))
    if args.maximum_seconds is not None and any(record["medianSeconds"] > args.maximum_seconds for record in records):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
