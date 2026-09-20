"""
Apply the SDK strategy catalog to real local workers under injected disturbances.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from polyad_benchmarks.studies.soul.strategies import freshness, reachability
from polyad_benchmarks.studies.soul.strategies.capacity import budget, memory, resources, threshold
from polyad_benchmarks.studies.soul.strategies.decisions import guard, observations
from polyad_benchmarks.studies.soul.strategies.observation import callbacks, logging
from polyad_benchmarks.studies.soul.strategies.routing import connections, peers, topology
from polyad_sdk import AdaptiveService, Settings
from polyad_sdk.symbiosis.reachability import QueueModel, compile_envelope
from polyad_types import Event, ServiceEndpoint
from soul import LocalObservations

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

    from polyad_sdk import Change, ConstraintAssessment

__all__ = ("Policy",)


class Policy(AdaptiveService):
    """
    Turn bounded process observations into admission checks and profile proposals.
    """

    def __init__(self, name: str, read: Callable[[], dict[str, Any]]) -> None:
        """
        Construct every strategy before starting the local observation lifecycle.

        Args:
            name (str): Service name within the study population.
            read (Callable[[], dict[str, Any]]): Current worker and disturbance measurements.
        """
        self.name, self.read = name, read
        self.sequence = 0
        self.receipt_uid = "lease-1"
        self.receipt_generation = 1
        self.permission = "Active"
        self.receipt_deadline = time.time() + 300
        self.proposal = "interactive"
        self.coverage: Counter[str] = Counter()
        self.assessments: dict[str, ConstraintAssessment] = {}
        self.changes = 0
        self.last_decision: str | None = None
        self.route_names: tuple[str, ...] = ()
        model = QueueModel(("queue",), (0,), (24,), (24,), (0, 0), (1.0,), unit="jobs")
        self.envelope = compile_envelope(model, "local-worker-v1", horizon=1, lifetime=300)
        guards = tuple(module.build(self) for module in (freshness, peers, connections, guard, budget, memory, reachability))
        self.guards = {component.name: component for component in guards}
        super().__init__(
            ServiceEndpoint("", "study", "Graph", name.lower(), f"study-{name}", "dispatch"),
            LocalObservations(self.topology),
            settings=Settings(max_age_seconds=1, refresh_seconds=0.1),
            strategies=(
                logging.build(self),
                callbacks.build(self),
                topology.build(self),
                resources.build(self),
                observations.build(self),
                threshold.build(self),
                *guards,
            ),
        )

    def topology(self) -> dict[str, Any]:
        """
        Describe current child identities and simulated observation availability.

        Returns:
            dict[str, Any]: Selected-node topology accepted by the SDK.
        """
        data = self.read()
        if data["stale"]:
            raise ConnectionError("study intervention: topology source is unreachable")
        return {
            "graph": {"kind": "Graph", "namespace": "study", "name": self.name.lower(), "uid": f"study-{self.name}"},
            "cursor": f"{self.sequence}-0",
            "revision": str(data["pids"]),
            "observedAt": time.time(),
            "valid": True,
            "templateOnly": False,
            "terminating": False,
            "node": {"name": "dispatch", "desired": True, "executions": []},
            "incoming": [],
            "dependencies": [],
            "dependents": [],
            "outgoing": [
                {
                    "node": {
                        "name": f"worker-{pid}",
                        "desired": True,
                        "executions": [{"uid": str(pid), "name": str(pid), "kind": "Process", "terminating": False}],
                    },
                    "ports": [],
                }
                for pid in data["pids"]
            ],
        }

    def remember(self, assessment: ConstraintAssessment) -> None:
        """
        Record actual guard results for the study's intervention evidence.

        Args:
            assessment (ConstraintAssessment): Satisfied, blocked or unknown condition.

        Returns:
            None: Preserve the result and increment its observed-state count.
        """
        self.assessments[assessment.name] = assessment
        self.coverage[f"{type(self.guards[assessment.name]).__name__}:{assessment.state}"] += 1

    def adapt(self, change: Change) -> None:
        """
        Complete delivery after the configured strategies record their decisions.

        Args:
            change (Change): Completed strategy delivery context.

        Returns:
            None: Worker mutations remain the service loop's responsibility.
        """

    def update(self) -> None:
        """
        Deliver measured resources and a local consent receipt through SDK events.

        Returns:
            None: Refresh topology, expiry, guards and pending worker intent.
        """
        try:
            self.refresh()
        except ConnectionError:
            # The SDK retains the last snapshot but marks it unavailable.
            # Admission and mutation guards must now defer new work.
            return
        data = self.read()
        self.sequence += 1

        # Renewal creates a new receipt incarnation; an expired authorization cannot revive in place.
        if self.permission == "Expired" and data["permission"] != "Expired":
            self.receipt_generation += 1
            self.receipt_uid = f"lease-{self.receipt_generation}"
            self.receipt_deadline = time.time() + 300
        self.permission = data["permission"]
        identity = self.topology()["graph"]
        self.dispatch(
            Event(
                f"{self.sequence}-0",
                "graph",
                {
                    **identity,
                    "apiVersion": "polyad.io/v1alpha1",
                    "resourceVersion": str(self.sequence),
                    "generation": 1,
                    "type": "observation",
                    "owners": [],
                    "ancestry": [],
                    "audit": {},
                    "status": {"throughput": {"phase": data["decision"], "demandValue": data["backlog"]}},
                    "resources": {
                        "backlog": data["backlog"],
                        "projectedWorkers": data["projectedWorkers"],
                        "memoryReserved": data["memoryReserved"],
                        "resourceAssignedBytes": data.get("resourceAssignedBytes", 0),
                        "resourceModeledUsageBytes": data.get("resourceModeledUsageBytes", 0),
                        "resourceAvailableBytes": data.get("resourceAvailableBytes", 0),
                        "resourcePressure": data.get("resourcePressure", False),
                    },
                },
            )
        )
        self.sequence += 1
        self.dispatch(
            Event(
                f"{self.sequence}-0",
                "connection",
                {
                    "type": "connection",
                    "graph": identity,
                    "ancestry": [],
                    "connection": {
                        "requestId": f"local-study-{self.receipt_generation}",
                        "name": self.receipt_uid,
                        "namespace": "study",
                        "uid": self.receipt_uid,
                        "expiresAt": datetime.fromtimestamp(
                            self.receipt_deadline if data["permission"] != "Expired" else time.time() - 1, UTC
                        ).isoformat(),
                        "target": {
                            "kind": "Graph",
                            "graph": self.name.lower(),
                            "graphUid": identity["uid"],
                            "source": "dispatch",
                            "target": "consumer",
                            "ports": [],
                            "bidirectional": False,
                        },
                        "status": {"phase": data["permission"]},
                        "consent": {},
                        "peers": {},
                        "revokeRequested": False,
                    },
                },
            )
        )

    def permits(self, *names: str) -> bool:
        """
        Reevaluate all relevant guards immediately before one action.

        Args:
            *names (str): Guard names applicable to this particular action.

        Returns:
            bool: Every selected condition is satisfied by the current view.
        """
        results = [self.guards[name].evaluate(self.view) for name in names]
        for result in results:
            self.remember(result)
        return all(result.satisfied for result in results)
