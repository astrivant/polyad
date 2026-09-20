"""
Load the Helm-projected limits for the operator's internal graph of work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from polyad.operator.coordination.pulses import PulsePolicy
from polyad.operator.coordination.validation import ValidationSettings

__all__ = ("WorkGraphSettings",)


@dataclass(frozen=True)
class WorkGraphSettings:
    """
    Keep planning, admission and worker budgets independently bounded.

    Attributes:
        max_in_flight (int): Concurrent Kubernetes transports per adapter.
        max_pending (int): Additional admitted writes beyond writer slots per adapter.
        planner_parallelism (int): Concurrent mutation callbacks per approved planner batch.
        reconciliation_workers (int): Concurrent reconciliation attempts or remote deliveries per cluster.
        validation (ValidationSettings): Per-adapter validator limits and receipt freshness.
        reconciliation_cooldown (float): Shared per-resource cooldown window before starting new decisions.
        reconciliation_burst (int): Reconciliation attempts permitted in one cooldown window.
    """

    max_in_flight: int = 1
    max_pending: int = 1
    planner_parallelism: int = 1
    reconciliation_workers: int = 1
    validation: ValidationSettings = field(default_factory=ValidationSettings)
    reconciliation_cooldown: float = 0
    reconciliation_burst: int = 1

    def __post_init__(self) -> None:
        """
        Reject invalid worker and admission limits before starting background tasks.

        Returns:
            None: Settings never grant permission to bypass dependency or ownership checks.
        """
        for name, low, high in (
            ("max_in_flight", 1, 32),
            ("max_pending", 0, 128),
            ("planner_parallelism", 1, 32),
            ("reconciliation_workers", 1, 32),
        ):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"work graph {name} must be an integer between {low} and {high}")
        PulsePolicy(self.reconciliation_cooldown, self.reconciliation_burst)

    @classmethod
    def from_environment(cls) -> WorkGraphSettings:
        """
        Load typed limits supplied by the chart or directly by the process environment.

        Returns:
            WorkGraphSettings: Validated configuration with conservative serial defaults.
        """
        return cls(
            max_in_flight=int(os.environ.get("POLYAD_WRITE_MAX_IN_FLIGHT", "1")),
            max_pending=int(os.environ.get("POLYAD_WRITE_QUEUE_MAX_PENDING", "1")),
            planner_parallelism=int(os.environ.get("POLYAD_MUTATION_PLANNER_PARALLELISM", "1")),
            reconciliation_workers=int(os.environ.get("POLYAD_RECONCILIATION_WORKERS", "1")),
            validation=ValidationSettings.from_environment(),
            reconciliation_cooldown=float(os.environ.get("POLYAD_RECONCILIATION_COOLDOWN_SECONDS", "0")),
            reconciliation_burst=int(os.environ.get("POLYAD_RECONCILIATION_BURST", "1")),
        )

    def document(self) -> dict[str, int | float]:
        """
        Report configured limits using the same keys as operator.writeQueue in Helm.

        Returns:
            dict[str, int | float]: Configuration only; counters describe actual pressure separately.
        """
        return {
            "maxInFlight": self.max_in_flight,
            "maxPending": self.max_pending,
            "plannerParallelism": self.planner_parallelism,
            "reconciliationWorkers": self.reconciliation_workers,
            "validationWorkers": self.validation.workers,
            "validationIntervalSeconds": self.validation.interval,
            "validationWindowSeconds": self.validation.window,
            "validationBurst": self.validation.burst,
            "reconciliationCooldownSeconds": self.reconciliation_cooldown,
            "reconciliationBurst": self.reconciliation_burst,
        }
