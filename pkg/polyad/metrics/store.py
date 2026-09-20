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

from polyad.metrics.graphs import HELP as GRAPH_HELP
from polyad.metrics.graphs import graph_rows
from polyad.metrics.workloads import GROUP_SIGNALS, workload_metric

if TYPE_CHECKING:
    from typing import Any

__all__ = ("MetricsStore",)


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
        gauge(
            "operator_interval_seconds",
            "Configured pause after each operator loop pass, not measured execution duration.",
            [
                ({"loop": name}, snapshot["tuning"][name])
                for name in ("rescan", "consume", "metrics", "backlog")
                if name in snapshot.get("tuning", {})
            ],
        )
        gauge("leader", "Whether this replica currently reports planner leadership.", [({}, int(snapshot["leader"]))])
        gauge("owned_shards", "Number of shards assigned to this replica.", [({}, len(snapshot["shards"]))])
        postgres = snapshot.get("postgresql", {})
        if postgres.get("enabled"):
            gauge(
                "postgresql_sample_fresh", "Whether the operator's PostgreSQL connection sample is current.", [({}, int(postgres["fresh"]))]
            )
            gauge(
                "postgresql_state_fresh",
                "Whether this process committed a complete state scan recently.",
                [({}, int(postgres["stateFresh"]))],
            )
            if postgres["fresh"]:
                gauge(
                    "postgresql_connections",
                    "All primary sessions from this control plane; deduplicate scrape replicas.",
                    [({}, postgres["connections"])],
                )
        for field, suffix in (("inUse", "in_use"), ("limit", "limit"), ("waiting", "waiting")):
            gauge(
                "connection_pool_" + suffix,
                "Process-local connection pool occupancy or configured capacity; idle sockets are not demand.",
                [({"pool": name}, entry[field]) for name, entry in snapshot.get("connectionPools", {}).items()],
            )

        # Publish shared component demand only with freshness evidence; stale values are not zero demand.
        components = snapshot.get("components", {})
        cache = snapshot.get("dragonfly", {})
        if cache.get("enabled"):
            gauge("dragonfly_sample_fresh", "Whether primary client sampling succeeded.", [({}, int(cache["fresh"]))])
            if cache["fresh"]:
                gauge("dragonfly_connections", "Primary connected clients; deduplicate scrape replicas.", [({}, cache["connections"])])
        gauge(
            "component_sample_fresh",
            "Whether all recent component processes reported fresh demand.",
            [({}, int(components.get("fresh", False)))],
        )
        if components.get("fresh"):
            for field, suffix in (("inUse", "in_use"), ("limit", "limit"), ("waiting", "waiting")):
                gauge(
                    "component_connection_pool_" + suffix,
                    "Global local-component pool observations; deduplicate scrape replicas.",
                    [
                        ({"component": component, "pool": name}, entry[field])
                        for component, group in components["roles"].items()
                        if group.get("connectionFresh")
                        for name, entry in group.get("connectionPools", {}).items()
                    ],
                )
            gauge(
                "component_connection_pressure",
                "Sum of busiest pool fractions per local component process; deduplicate scrape replicas.",
                [
                    ({"component": name}, entry["connectionPressure"])
                    for name, entry in components["roles"].items()
                    if entry.get("connectionFresh")
                ],
            )
            gauge(
                "component_reporting_replicas",
                "Fresh process reports per component; deduplicate scrape replicas.",
                [({"component": name}, entry["replicas"]) for name, entry in components["roles"].items() if "replicas" in entry],
            )
            for field, suffix in (("requestsPerSecond", "requests_per_second"), ("inFlight", "requests_in_flight")):
                gauge(
                    "component_" + suffix,
                    "Global component HTTP demand; deduplicate scrape replicas.",
                    [({"component": name}, entry[field]) for name, entry in components["roles"].items()],
                )
        gauge(
            "refresh_queue_entries", "Replica-local queued reconciliation keys, excluding the active attempt.", [({}, snapshot["pending"])]
        )
        writes = snapshot["writes"]
        for field, suffix in (("queued", "queued"), ("inFlight", "in_flight")):
            gauge(
                "kubernetes_writes_" + suffix,
                "Replica-local concrete API mutations by writer.",
                [({"writer": writer}, writes[writer][field]) for writer in ("workloads", "coordination", "apiIntake")],
            )
        for field, suffix in (("oldestQueuedSeconds", "queued"), ("oldestInFlightSeconds", "in_flight")):
            gauge(
                "kubernetes_writes_oldest_" + suffix + "_seconds",
                "Age of the oldest local API mutation by writer.",
                [({"writer": writer}, writes[writer].get(field, 0)) for writer in ("workloads", "coordination", "apiIntake")],
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
                if obj["kind"] not in {"Graph", "PolyGraph", "ReplicaGroup"} or obj["role"] != "instance":
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
                service_states = []
                service_values = []
                adaptation_values = []
                service_freshness = []
                for obj in tracked["objects"]:
                    service = obj.get("serviceLevel") or {}
                    if service:
                        labels = {"kind": obj["kind"], "name": obj["name"]}
                        service_freshness.append((labels, float(obj.get("serviceLevelFresh", False))))
                        for state in ("Compliant", "Degraded", "Unavailable"):
                            service_states.append(({**labels, "state": state}, float(service.get("state") == state)))
                        for statistic in ("availability", "latencyCompliance", "errorBudgetRemaining"):
                            if statistic in service:
                                service_values.append(({**labels, "statistic": statistic}, service[statistic]))
                        for statistic, value in service.get("counters", {}).items():
                            service_values.append(({**labels, "statistic": statistic}, value))
                    adaptation = obj.get("adaptation") or {}
                    for statistic, value in adaptation.get("statistics", {}).items():
                        adaptation_values.append(({"kind": obj["kind"], "name": obj["name"], "statistic": statistic}, value))
                gauge("service_level_state", "Current Daemon service-contract classification.", service_states)
                gauge(
                    "service_level_sample_fresh",
                    "Whether the latest Daemon service observation remains within its age bound.",
                    service_freshness,
                )
                gauge("service_level", "Current service-level ratios and fixed-window counters.", service_values)
                gauge("adaptation", "Current-generation SDK adaptation attempt and duration totals.", adaptation_values)
                gauge("graph_shape", "Declared topology nodes, admission edges, breadth and depth of current graph observations.", shape)
                gauge("graph_status_current", "Whether graph metrics describe the object's current generation.", current_status)
                gauge("hierarchy_info", "Object membership in parent and root controller hierarchies.", rows)
                gauge(
                    "graph_direct_resources",
                    "Direct owned resources from current-generation graph status; excludes recursive rollups.",
                    resources,
                )
        clusters = snapshot.get("clusters", {})
        workers = snapshot.get("workers", {})
        gauge(
            "worker_sample_fresh",
            "Root-held worker heartbeat and pressure sample availability.",
            [({"worker": name}, int(report.get("fresh", False))) for name, report in workers.items()],
        )
        gauge(
            "worker_writes_queued",
            "Worker API writes observed centrally; deduplicate root scrape replicas.",
            [({"worker": name}, report["writes"]["queued"]) for name, report in workers.items() if report.get("fresh", False)],
        )
        gauge(
            "worker_writes_in_flight",
            "Worker API writes awaiting completion, observed centrally; deduplicate root scrape replicas.",
            [({"worker": name}, report["writes"]["inFlight"]) for name, report in workers.items() if report.get("fresh", False)],
        )
        gauge(
            "worker_refresh_queue_entries",
            "Worker-local waiting reconciliation keys observed centrally; excludes active attempts.",
            [({"worker": name}, report["pending"]) for name, report in workers.items() if report.get("fresh", False)],
        )
        gauge(
            "cluster_inventory_sample_fresh",
            "Fresh root-held remote inventory.",
            [({"cluster": cluster}, int(sample["inventory"]["fresh"])) for cluster, sample in clusters.items()],
        )
        gauge(
            "cluster_inbound_sample_fresh",
            "Whether root-held remote stream backlog is fresh independently of inventory.",
            [({"cluster": cluster}, int(sample.get("inbound", {}).get("fresh", False))) for cluster, sample in clusters.items()],
        )
        gauge(
            "cluster_inbound_updates",
            "Remote stream backlog in root storage; deduplicate root replicas.",
            [
                ({"cluster": cluster, "shard": str(shard), "state": state}, value)
                for cluster, sample in clusters.items()
                if sample["inventory"]["fresh"] and sample.get("inbound", {}).get("fresh")
                for shard, (total, pending) in sample.get("shardBacklogs", {}).items()
                for state, value in (("queued", total - pending), ("unacknowledged", pending))
            ],
        )
        remote_signals = []
        if graph_labels:
            for cluster, sample in clusters.items():
                if not sample["inventory"]["fresh"]:
                    continue
                for obj in sample["inventory"]["objects"]:
                    candidates = [(None, key) for key in GROUP_SIGNALS] if obj["kind"] == "ReplicaGroup" else []
                    candidates += [(None, key) for key in obj.get("boundarySignals", {}) if key not in GROUP_SIGNALS]
                    candidates += [(node, key) for node, entry in (obj.get("workloads") or {}).items() for key in entry["values"]]
                    for node, signal_name in candidates:
                        try:
                            value = workload_metric(sample, obj["kind"], obj["name"], signal_name, node)["value"]
                        except (KeyError, ValueError):
                            continue
                        remote_signals.append(
                            (
                                {"cluster": cluster, "kind": obj["kind"], "name": obj["name"], "node": node or "", "signal": signal_name},
                                value,
                            )
                        )
        gauge("cluster_workload_signal", "Fresh remote workload signals observed by the root.", remote_signals)
        if graph_labels:
            for family, rows in graph_rows(snapshot).items():
                gauge(family, GRAPH_HELP[family], rows)
        rendered = generate_latest(registry)
        document = json.dumps(snapshot, allow_nan=False).encode()

        # Swap both encodings together so JSON and Prometheus readers see the same observation.
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
