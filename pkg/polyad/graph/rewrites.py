"""
Describe atomic dependency-graph rewrites and boundary-local named registries.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polyad.graph.workloads import Workload


@dataclass(frozen=True)
class Rewrite:
    """
    Describe one complete proposed structural change.

    Attributes:
        additions (tuple[Workload, ...]): New or replacement implementations.
        removals (tuple[str, ...]): Existing units to remove.
        links (tuple[tuple[str, tuple[str, ...]], ...]): Complete replacement prerequisite lists.
    """

    additions: tuple[Workload, ...] = ()
    removals: tuple[str, ...] = ()
    links: tuple[tuple[str, tuple[str, ...]], ...] = ()

    @classmethod
    def replace(cls, removed: tuple[str, ...], added: tuple[Workload, ...], links: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> Rewrite:
        """
        Replace a subgraph with explicit external reconnections.

        Args:
            removed (tuple[str, ...]): Units being replaced.
            added (tuple[Workload, ...]): Replacement implementations.
            links (tuple[tuple[str, tuple[str, ...]], ...]): Reconnected prerequisite lists.

        Returns:
            Rewrite: A replacement transaction.
        """
        return cls(added, removed, links)

    @classmethod
    def prune(cls, removed: tuple[str, ...], links: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> Rewrite:
        """
        Remove unnecessary units with explicit reconnections.

        Args:
            removed (tuple[str, ...]): Units whose work is unnecessary.
            links (tuple[tuple[str, tuple[str, ...]], ...]): Surviving prerequisites after removal.

        Returns:
            Rewrite: A removal transaction; no edge bypass is inferred.
        """
        return cls(removals=removed, links=links)

    @classmethod
    def splice(cls, unit: Workload, target: str, requires: tuple[str, ...]) -> Rewrite:
        """
        Insert a unit and replace the target's prerequisites in the same transaction.

        Args:
            unit (Workload): Inserted implementation with its upstream prerequisites.
            target (str): Existing downstream unit.
            requires (tuple[str, ...]): Full new target prerequisites, including the inserted unit.

        Returns:
            Rewrite: An insertion transaction.
        """
        if unit.work.name not in requires:
            raise ValueError("splice target must depend on the inserted unit")
        return cls(additions=(unit,), links=((target, requires),))

    @classmethod
    def split(cls, original: str, partitions: tuple[Workload, ...], links: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> Rewrite:
        """
        Replace one unit with application-defined partitions and optional join work.

        Args:
            original (str): Unstarted unit to replace.
            partitions (tuple[Workload, ...]): Partition and join implementations.
            links (tuple[tuple[str, tuple[str, ...]], ...]): External consumers' new prerequisites.

        Returns:
            Rewrite: A split transaction with explicit data ownership.
        """
        if len(partitions) < 2:
            raise ValueError("split requires at least two replacement units")
        return cls(partitions, (original,), links)

    @classmethod
    def fuse(cls, originals: tuple[str, ...], unit: Workload, links: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> Rewrite:
        """
        Replace multiple units with an application-supplied fused implementation.

        Args:
            originals (tuple[str, ...]): Unstarted units to combine.
            unit (Workload): Implementation responsible for their combined behavior.
            links (tuple[tuple[str, tuple[str, ...]], ...]): External consumers' new prerequisites.

        Returns:
            Rewrite: A fusion transaction; semantic equivalence is application-owned.
        """
        if len(originals) < 2:
            raise ValueError("fuse requires at least two original units")
        return cls((unit,), originals, links)

    @classmethod
    def replicate(cls, replicas: tuple[Workload, ...], links: tuple[tuple[str, tuple[str, ...]], ...] = ()) -> Rewrite:
        """
        Add explicitly named replicas and application-defined result selection.

        Args:
            replicas (tuple[Workload, ...]): Independent implementations with distinct identities.
            links (tuple[tuple[str, tuple[str, ...]], ...]): Consumers or a result-selection unit.

        Returns:
            Rewrite: A replica insertion transaction.
        """
        if len(replicas) < 2:
            raise ValueError("replicate requires at least two units")
        return cls(additions=replicas, links=links)


class RewriteRegistry:
    """
    Own immutable named proposals independently at each graph boundary.
    """

    def __init__(self) -> None:
        """
        Create an empty registry and its synchronization lock.
        """
        self._entries: dict[str, Rewrite] = {}
        self._lock = RLock()

    def register(self, name: str, rewrite: Rewrite) -> None:
        """
        Register a proposal without replacing an existing name.

        Args:
            name (str): Boundary-local operation name.
            rewrite (Rewrite): Complete structural proposal.

        Returns:
            None: No return value.
        """
        with self._lock:
            if not name or name in self._entries:
                raise ValueError("rewrite names must be nonempty and unique within their registry")
            self._entries[name] = rewrite

    def get(self, name: str) -> Rewrite:
        """
        Retrieve a named proposal safely.

        Args:
            name (str): Boundary-local operation name.

        Returns:
            Rewrite: Registered immutable proposal.
        """
        with self._lock:
            return self._entries[name]

    def names(self) -> tuple[str, ...]:
        """
        List registered operations in stable name order.

        Returns:
            tuple[str, ...]: Boundary-local rewrite names.
        """
        with self._lock:
            return tuple(sorted(self._entries))
