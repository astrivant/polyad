"""
Bound per-boundary adaptive scheduler history without weakening Cheeger certificates.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
from typing import TYPE_CHECKING

from polyad.graph.pid import AccuracyTargetPID, CacheTargetConfig, CacheTargetPID, RefreshConfig, RefreshPID

if TYPE_CHECKING:
    import networkx as nx

    from polyad_types.graphs.rules import CheegerReduction

__all__ = ("RefreshTicket", "begin_refresh", "clear_refresh_cache", "finish_refresh")


@dataclass
class _State:
    inner: RefreshPID
    outer: CacheTargetPID | AccuracyTargetPID
    revision: int = 0


@dataclass(frozen=True)
class RefreshTicket:
    """
    Snapshot one scheduling decision without holding a lock during graph computation.

    Attributes:
        key (tuple[object, ...]): Boundary, membership and settings identity.
        state (_State): Process-local controller pair retained by this calculation.
        revision (int): Version preventing concurrent calls from double-updating history.
        scheduled (bool): Whether this observation should skip cache and refresh immediately.
        target_seconds (float): Administrator-owned soft computation-time objective.
        target_relative_error (float): Administrator-owned soft certified relative-error objective.
    """

    key: tuple[object, ...]
    state: _State
    revision: int
    scheduled: bool
    target_seconds: float
    target_relative_error: float


_STATES: OrderedDict[tuple[object, ...], _State] = OrderedDict()
_LOCK = RLock()


def begin_refresh(graph: nx.Graph[str], settings: CheegerReduction) -> RefreshTicket:
    """
    Select cache or refresh using past observations from this boundary only.

    Args:
        graph (nx.Graph[str]): Current simple graph, optionally carrying a cheegerCacheScope identity.
        settings (CheegerReduction): Effective administrator-approved scheduler and reduction controls.

    Returns:
        RefreshTicket: Causal decision; membership or tuning changes start independent history.
    """
    key = (
        graph.graph.get("cheegerCacheScope", ""),
        tuple(sorted(graph)),
        settings.components,
        settings.supernodes,
        settings.maxEdgeChurn,
        settings.targetSeconds,
        settings.feedback,
        settings.targetRelativeError,
    )
    with _LOCK:
        state = _STATES.get(key)
        if state is None:
            config = CacheTargetConfig()
            outer = AccuracyTargetPID(config) if settings.feedback == "CertificateGap" else CacheTargetPID(config)
            state = _State(RefreshPID(RefreshConfig(targetCacheRate=config.initialCacheRate)), outer)
            _STATES[key] = state
        _STATES.move_to_end(key)

        # Controller memory has the same finite entry ceiling as partition memory.
        # An evicted in-flight ticket can finish, but cannot revive old history.
        while len(_STATES) > settings.cacheEntries:
            _STATES.popitem(last=False)
        scheduled = state.inner.refresh_due() or (isinstance(state.outer, AccuracyTargetPID) and state.outer.target == 0)
        return RefreshTicket(key, state, state.revision, scheduled, settings.targetSeconds, settings.targetRelativeError)


def finish_refresh(
    ticket: RefreshTicket,
    duration: float,
    *,
    cache_attempted: bool,
    refreshed: bool,
    lower: float = 0.0,
    upper: float | None = None,
) -> dict[str, object]:
    """
    Update both loops from completed work, never from exact truth or a future graph.

    Args:
        ticket (RefreshTicket): Decision snapshot created before computation.
        duration (float): Whole calculation wall time, including exact fallback when necessary.
        cache_attempted (bool): Entering cache is the inner controller's failure signal, even on a hit.
        refreshed (bool): Whether fresh spectral work actually completed.
        lower (float): Last reduced lower bound, before any exact fallback.
        upper (float | None): Last reduced upper witness; absent means full uncertainty.

    Returns:
        dict[str, object]: Both loop reports, or a skipped concurrent/evicted update marker.
    """
    with _LOCK:
        state = ticket.state
        if _STATES.get(ticket.key) is not state or state.revision != ticket.revision:
            return {"refreshScheduled": ticket.scheduled, "updateSkipped": True}
        inner = state.inner.observe(cache_attempted=cache_attempted, refreshed=refreshed)
        accuracy = isinstance(state.outer, AccuracyTargetPID)
        outer = (
            state.outer.observe(lower, upper, ticket.target_relative_error)
            if isinstance(state.outer, AccuracyTargetPID)
            else state.outer.observe(duration, ticket.target_seconds)
        )
        state.inner.set_target(state.outer.target)
        state.revision += 1
        return {
            "refreshScheduled": ticket.scheduled,
            "updateSkipped": False,
            "feedback": "CertificateGap" if accuracy else "ComputationTime",
            "inner": inner,
            "outer": outer,
        }


def clear_refresh_cache() -> None:
    """
    Reset all process-local controller histories alongside the partition cache.

    Returns:
        None: Used for isolated benchmark trajectories and deterministic tests.
    """
    with _LOCK:
        _STATES.clear()
