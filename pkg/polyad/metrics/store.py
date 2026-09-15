"""
Publish immutable telemetry snapshots across the scheduler and HTTP threads.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from typing import TYPE_CHECKING

from prometheus_client import CollectorRegistry, Gauge, generate_latest

from polyad.metrics.workloads import GROUP_SIGNALS, workload_metric

if TYPE_CHECKING:
    from typing import Any


class MetricsStore:
    """
    Replace entire snapshots so scrapes perform no Kubernetes or cache I/O.
    """

    def __init__(self) -> None:
        """
        Initialize an empty snapshot and its publication lock.
        """
        self.lock = threading.Lock()
        self.published: tuple[float, bytes, bytes] | None = None

    def publish(self, snapshot: dict[str, Any], *, graph_labels: bool = False) -> None:
        """
        Render a complete snapshot before exposing it to concurrent HTTP readers.

        Args:
            snapshot (dict[str, Any]): Queue, inventory, and replica observations from the operator loop.
            graph_labels (bool): Include optional per-object identity and resource series.

        Returns:
            None: No return value.
        """
        registry = CollectorRegistry()
        common = {"namespace": snapshot["namespace"], "replica": snapshot["replica"]}

        def gauge(name: str, help_text: str, rows: list[tuple[dict[str, str], float]]) -> None:
            if not rows:
                return
            labels = tuple(rows[0][0])
            metric = Gauge("polyad_" + name, help_text, (*common, *labels), registry=registry)
            for extra, value in rows:
                metric.labels(*common.values(), *(extra[label] for label in labels)).set(value)

        gauge("snapshot_timestamp_seconds", "Unix time of this replica snapshot.", [({}, time.time())])
        gauge("leader", "Whether this replica currently reports planner leadership.", [({}, int(snapshot["leader"]))])
        gauge("owned_shards", "Number of shards assigned to this replica.", [({}, len(snapshot["shards"]))])
        gauge(
            "refresh_queue_entries", "Replica-local queued reconciliation keys, excluding the active attempt.", [({}, snapshot["pending"])]
        )
        writes = snapshot["writes"]
        for field, suffix in (("queued", "queued"), ("inFlight", "in_flight")):
            gauge(
                "kubernetes_writes_" + suffix,
                "Replica-local concrete API mutations by writer.",
                [({"writer": writer}, writes[writer][field]) for writer in ("workloads", "coordination", "compositionIntake")],
            )
        for field, suffix in (("oldestQueuedSeconds", "queued"), ("oldestInFlightSeconds", "in_flight")):
            gauge(
                "kubernetes_writes_oldest_" + suffix + "_seconds",
                "Age of the oldest local API mutation by writer.",
                [({"writer": writer}, writes[writer].get(field, 0)) for writer in ("workloads", "coordination", "compositionIntake")],
            )
        backlog = snapshot["inbound"]
        gauge("inbound_sample_fresh", "Whether shared queue sampling succeeded recently.", [({}, int(backlog["fresh"]))])
        if backlog["sampleAgeSeconds"] is not None:
            gauge("inbound_sample_age_seconds", "Age of the last complete shared queue sample.", [({}, backlog["sampleAgeSeconds"])])
        if backlog["fresh"]:
            gauge(
                "inbound_updates",
                "Shared namespace stream entries; deduplicate replicas before summing shards.",
                [
                    ({"shard": str(shard), "state": state}, value)
                    for shard, (total, pending) in snapshot["shardBacklogs"].items()
                    for state, value in (("queued", total - pending), ("unacknowledged", pending))
                ],
            )
        tracked = snapshot["inventory"]
        gauge("inventory_sample_fresh", "Whether a full namespace inventory succeeded recently.", [({}, int(tracked["fresh"]))])
        if tracked["sampleAgeSeconds"] is not None:
            gauge("inventory_sample_age_seconds", "Age of the last complete namespace inventory.", [({}, tracked["sampleAgeSeconds"])])
        if tracked["fresh"]:
            gauge(
                "tracked_objects",
                "Scanned scheduler CRs; shared namespace counts, not all Kubernetes objects.",
                [({"kind": row["kind"], "role": row["role"]}, row["count"]) for row in tracked["byKind"]],
            )
            gauge("tracked_objects_total_count", "Total scanned scheduler CRs in the namespace.", [({}, tracked["total"])])
            groups: dict[tuple[str, str, str], int] = {}
            for obj in tracked["objects"]:
                key = (str(obj["shard"]) if obj["shard"] is not None else "unknown", obj["kind"], obj["phase"])
                groups[key] = groups.get(key, 0) + 1
            gauge(
                "shard_objects",
                "Scheduler CRs grouped by root-family shard, kind and phase.",
                [({"shard": shard, "kind": kind, "phase": phase}, count) for (shard, kind, phase), count in sorted(groups.items())],
            )
            direct: Counter[str] = Counter()
            coverage: Counter[str] = Counter({"current": 0, "unknown": 0})
            for obj in tracked["objects"]:
                if obj["kind"] not in {"Graph", "PolyGraph", "EphemeralGraph", "Feedback", "ReplicaGroup"} or obj["role"] != "instance":
                    continue
                coverage["current" if obj["statusCurrent"] else "unknown"] += 1
                if obj["statusCurrent"]:
                    direct.update((obj["resources"] or {}).get("byKind", {}))
            gauge(
                "observed_resources",
                "Sum of direct resources in current-generation graph status; never sum with tracked CR counts.",
                [({"kind": kind}, count) for kind, count in sorted(direct.items())],
            )
            gauge(
                "graph_status_observations",
                "Coverage of current-generation graph resource observations.",
                [({"state": state}, count) for state, count in coverage.items()],
            )
            if graph_labels:
                rows = []
                resources: list[tuple[dict[str, str], float]] = []
                shape: list[tuple[dict[str, str], float]] = []
                current_status = []
                for obj in tracked["objects"]:
                    root, parent = obj["root"] or {}, obj["parent"] or {}
                    labels = {
                        "kind": obj["kind"],
                        "name": obj["name"],
                        "root_kind": root.get("kind", ""),
                        "root": root.get("name", ""),
                        "parent_kind": parent.get("kind", ""),
                        "parent": parent.get("name", ""),
                        "role": obj["role"],
                        "shard": str(obj["shard"]) if obj["shard"] is not None else "unknown",
                    }
                    rows.append((labels, 1.0))
                    current_status.append((labels, float(obj["statusCurrent"])))
                    if obj["statusCurrent"] and obj["topology"]:
                        topology = obj["topology"]
                        dimensions = {
                            "nodes": topology["nodeCount"],
                            **{key: topology.get("admission", {}).get(key, 0) for key in ("depth", "breadth", "edgeCount")},
                        }
                        shape.extend(({**labels, "dimension": key}, value) for key, value in dimensions.items())
                    if obj["statusCurrent"] and obj["role"] == "instance":
                        resources.extend(
                            ({**labels, "resource_kind": kind}, count) for kind, count in (obj["resources"] or {}).get("byKind", {}).items()
                        )
                signals = []
                for obj in tracked["objects"]:
                    candidates = [(None, key) for key in GROUP_SIGNALS] if obj["kind"] == "ReplicaGroup" else []
                    candidates += [(None, key) for key in obj.get("boundarySignals", {}) if key not in GROUP_SIGNALS]
                    candidates += [(node, key) for node, entry in (obj.get("workloads") or {}).items() for key in entry["values"]]
                    for node, signal_name in candidates:
                        try:
                            value = workload_metric(snapshot, obj["kind"], obj["name"], signal_name, node)["value"]
                        except (KeyError, ValueError):
                            continue
                        signals.append(({"kind": obj["kind"], "name": obj["name"], "node": node or "", "signal": signal_name}, value))
                gauge("workload_signal", "Fresh workload signals and replica controls; deduplicate operator replicas.", signals)
                gauge("graph_shape", "Declared topology nodes, admission edges, breadth and depth of current graph observations.", shape)
                gauge("graph_status_current", "Whether graph metrics describe the object's current generation.", current_status)
                gauge("hierarchy_info", "Object membership in parent and root controller hierarchies.", rows)
                gauge(
                    "graph_direct_resources",
                    "Direct owned resources from current-generation graph status; excludes recursive rollups.",
                    resources,
                )
        rendered = generate_latest(registry)
        document = json.dumps(snapshot, allow_nan=False).encode()
        with self.lock:
            self.published = time.monotonic(), rendered, document

    def read(self) -> tuple[bytes, bytes] | None:
        """
        Read a consistent snapshot, rejecting stopped or stalled publication.

        Returns:
            tuple[bytes, bytes] | None: Prometheus and JSON bytes, or None after fifteen seconds without publication.
        """
        with self.lock:
            sample = self.published
        if sample is None or time.monotonic() - sample[0] >= 15:
            return None
        return sample[1], sample[2]
