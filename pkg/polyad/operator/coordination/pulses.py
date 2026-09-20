"""
Apply administrator pulse budgets across replicas before starting new decisions.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from polyad.lua import script

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from typing import Any

    from redis.asyncio import Redis

__all__ = (
    "PulseDeferred",
    "PulsePolicy",
)


class PulseDeferred(RuntimeError):
    """
    Retain desired work until its shared cooldown window permits another decision.
    """

    def __init__(self, seconds: float) -> None:
        """
        Report a bounded delay without retaining a mutation payload.

        Args:
            seconds (float): Remaining shared cooldown window.
        """
        self.retry_after = max(1, math.ceil(seconds))
        super().__init__("administrator pulse cooldown is active; retry from fresh state")


@dataclass(frozen=True)
class PulsePolicy:
    """
    Bound new pulses per identity in a shared fixed cooldown window.

    Attributes:
        cooldown (float): Window duration in seconds; zero disables this additional budget.
        burst (int): Pulses admitted before the window closes.
    """

    cooldown: float = 0
    burst: int = 1

    def __post_init__(self) -> None:
        """
        Reject nonnumeric, fractional-count and unbounded settings.

        Returns:
            None: Both Helm and direct environment configuration retain finite bounds.
        """
        if type(self.cooldown) not in (int, float) or not math.isfinite(self.cooldown) or not 0 <= self.cooldown <= 300:
            raise ValueError("pulse cooldown must be a finite number from 0 through 300 seconds")
        if type(self.burst) is not int or not 1 <= self.burst <= 128:
            raise ValueError("pulse burst must be an integer from 1 through 128")

    async def admit(self, client: Redis, identity: str) -> None:
        """
        Charge a decision before reading its actionable state, using the shared server clock.

        Args:
            client (Redis): Existing bounded shared-cache transport.
            identity (str): Namespace, cluster and operation identity; never a credential.

        Returns:
            None: Denials raise PulseDeferred; cache failures never enable a local bypass.
        """
        if self.cooldown == 0:
            return
        digest = hashlib.sha256(identity.encode()).hexdigest()
        milliseconds = await cast(
            "Awaitable[Any]",
            client.eval(
                script("coordination/pulse.lua"), 1, f"polyad:pulses:{{{digest}}}", str(math.ceil(self.cooldown * 1000)), str(self.burst)
            ),
        )
        if int(milliseconds) > 0:
            raise PulseDeferred(int(milliseconds) / 1000)
