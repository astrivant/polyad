"""
Rank ready work and request worthwhile preemption with uncertainty and aging guards.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from polyad.graph.workloads import Estimate, Work


class SchedulingPolicy(ABC):
    """
    Rank ready work and decide whether cooperative preemption is worthwhile.

    The scheduler retains dependency, capacity and checkpoint admission. A policy
    supplies ordering and pause decisions; it never starts or stops workloads.
    """

    def priorities(self, works: Mapping[str, Work]) -> dict[str, int]:
        """
        Assign stable insertion-order priorities unless a policy overrides traversal.

        Args:
            works (Mapping[str, Work]): Validated current graph in insertion order.

        Returns:
            dict[str, int]: Tie-breaking position for every workload.
        """
        return {name: index for index, name in enumerate(works)}

    @abstractmethod
    def rank(self, estimate: Estimate, waiting: float, order: int) -> tuple[float, float, int]:
        """
        Rank a ready workload without changing dependencies or reserving resources.

        Args:
            estimate (Estimate): Latest remaining-time and checkpoint estimates.
            waiting (float): Seconds spent waiting for admission.
            order (int): Stable priority assigned by priorities().

        Returns:
            tuple[float, float, int]: Ascending ordering key.
        """
        ...

    @abstractmethod
    def preempt(self, running: Estimate, waiting: Estimate, elapsed: float, waited: float) -> bool:
        """
        Decide whether to request a cooperative pause from resumable running work.

        Args:
            running (Estimate): Active workload's remaining work and pause costs.
            waiting (Estimate): Candidate workload's latest estimates.
            elapsed (float): Current execution slice in seconds.
            waited (float): Candidate's time waiting in seconds.

        Returns:
            bool: True to request a checkpoint; resources remain held until it completes.
        """
        ...


@dataclass(frozen=True)
class ShortestRemaining(SchedulingPolicy):
    """
    Favor shorter known jobs while giving long-waiting work eventual priority.

    Attributes:
        minimum_run_seconds (float): Minimum execution slice before a pause request.
        minimum_gain_seconds (float): Required benefit beyond checkpoint and resume costs.
        maximum_wait_seconds (float): Waiting time after which FIFO aging takes priority.
    """

    minimum_run_seconds: float = 5
    minimum_gain_seconds: float = 1
    maximum_wait_seconds: float = 60

    def __post_init__(self) -> None:
        """
        Reject invalid scheduling thresholds.

        Returns:
            None: Nonfinite or negative thresholds raise before scheduling begins.
        """
        for value in (self.minimum_run_seconds, self.minimum_gain_seconds, self.maximum_wait_seconds):
            if not math.isfinite(value) or value < 0:
                raise ValueError("policy thresholds must be finite and nonnegative")

    def rank(self, estimate: Estimate, waiting: float, order: int) -> tuple[float, float, int]:
        """
        Rank waiting work deterministically without treating unknown cost as free.

        Args:
            estimate (Estimate): Latest remaining-time estimate.
            waiting (float): Seconds since submission or the last pause.
            order (int): Stable submission order for tie breaking.

        Returns:
            tuple[float, float, int]: Ascending priority key.
        """
        if waiting >= self.maximum_wait_seconds:
            return (0, -waiting, order)
        duration = estimate.remaining_seconds
        return (1, float("inf") if duration is None else duration + estimate.uncertainty_seconds, order)

    def preempt(self, running: Estimate, waiting: Estimate, elapsed: float, waited: float) -> bool:
        """
        Require known pause costs and conservative gain before interrupting useful work.

        Args:
            running (Estimate): Remaining time and overhead of the active workload.
            waiting (Estimate): Candidate's estimated remaining time.
            elapsed (float): Seconds in the current execution slice.
            waited (float): Candidate's queue wait.

        Returns:
            bool: Whether to request a cooperative checkpoint.
        """
        if elapsed < self.minimum_run_seconds or running.checkpoint_seconds is None or running.resume_seconds is None:
            return False
        if waited >= self.maximum_wait_seconds:
            return True
        if running.remaining_seconds is None or waiting.remaining_seconds is None:
            return False
        gain = running.remaining_seconds - running.uncertainty_seconds - waiting.remaining_seconds - waiting.uncertainty_seconds
        return gain > running.checkpoint_seconds + running.resume_seconds + self.minimum_gain_seconds


@dataclass(frozen=True)
class FIFO(ShortestRemaining):
    """
    Preserve submission order and disable preemption for comparison or sensitive workloads.
    """

    def rank(self, estimate: Estimate, waiting: float, order: int) -> tuple[float, float, int]:
        """
        Rank only by submission order.

        Args:
            estimate (Estimate): Ignored cost estimate.
            waiting (float): Ignored queue wait.
            order (int): Stable submission order.

        Returns:
            tuple[float, float, int]: FIFO ordering key.
        """
        return (0, 0, order)

    def preempt(self, running: Estimate, waiting: Estimate, elapsed: float, waited: float) -> bool:
        """
        Keep active workloads running until they complete.

        Args:
            running (Estimate): Active workload cost.
            waiting (Estimate): Pending workload cost.
            elapsed (float): Current execution slice.
            waited (float): Pending queue wait.

        Returns:
            bool: Always False.
        """
        return False


@dataclass(frozen=True)
class BreadthFirst(FIFO):
    """
    Prefer nodes in breadth-first discovery order, subject to prerequisite completion.
    """

    def priorities(self, works: Mapping[str, Work]) -> dict[str, int]:
        """
        Visit roots and then successive successor layers in stable insertion order.

        Args:
            works (Mapping[str, Work]): Current acyclic graph at one scheduling boundary.

        Returns:
            dict[str, int]: Breadth-first discovery positions.
        """
        return _traversal(works, depth_first=False)


@dataclass(frozen=True)
class DepthFirst(FIFO):
    """
    Prefer continuing one discovered branch before starting another ready branch.
    """

    def priorities(self, works: Mapping[str, Work]) -> dict[str, int]:
        """
        Visit each root's descendants before moving to the next root.

        Args:
            works (Mapping[str, Work]): Current acyclic graph at one scheduling boundary.

        Returns:
            dict[str, int]: Depth-first preorder positions.
        """
        return _traversal(works, depth_first=True)


def _traversal(works: Mapping[str, Work], *, depth_first: bool) -> dict[str, int]:
    """
    Traverse prerequisite-to-dependent edges without recursion or repeated expansion.

    Args:
        works (Mapping[str, Work]): Validated graph in stable insertion order.
        depth_first (bool): Use a stack instead of a breadth-first queue.

    Returns:
        dict[str, int]: Discovery positions for every node, including disconnected roots.
    """
    successors: dict[str, list[str]] = {name: [] for name in works}
    roots: list[str] = []
    for name, work in works.items():
        if not work.requires:
            roots.append(name)
        for prerequisite in work.requires:
            successors[prerequisite].append(name)
    frontier = deque(reversed(roots) if depth_first else roots)
    positions: dict[str, int] = {}
    while frontier:
        name = frontier.pop() if depth_first else frontier.popleft()
        if name in positions:
            continue
        positions[name] = len(positions)
        children = successors[name]
        frontier.extend(reversed(children) if depth_first else children)
    return positions
