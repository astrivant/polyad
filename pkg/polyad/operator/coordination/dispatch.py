"""
Dispatch a bounded dependency graph using explicit planner evidence for parallel writes.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

import networkx as nx

from polyad.compiler.passes.mutations import conflicts
from polyad.compiler.registry import RESOURCE_TYPES
from polyad.operator.observability.decisions import decision

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from polyad.operator.coordination.validation import Validation
    from polyad_types.resources.mutations import Mutation


@dataclass(eq=False)
class Batch:
    """
    Identify a freshly checked planner batch within its caller's ownership boundary.

    Attributes:
        active (bool): False after callbacks finish, preventing reuse by escaped tasks.
    """

    active: bool = True


@dataclass(frozen=True)
class Admission:
    """
    Carry one mutation's approved effects into its concrete Kubernetes writes.

    Attributes:
        batch (Batch): Shared evidence that budgets and preconditions were checked together.
        mutation (Mutation): Complete effect declaration checked by the planner.
    """

    batch: Batch
    mutation: Mutation

    def covers(self, receipt: Validation) -> bool:
        """
        Require a declared scope covering the entire concrete target object.

        Args:
            receipt (Validation): Write whose transport conflict scope must be modeled.

        Returns:
            bool: Field-only or unknown effects cannot authorize concurrent object writes.
        """
        if not self.batch.active or not self.mutation.effects_complete or receipt.intent is None:
            return False
        kind, namespace, name = receipt.intent.target
        descriptor = RESOURCE_TYPES.get(kind)
        if descriptor is None:
            return False
        version = descriptor.prefix.removeprefix("/apis/").removeprefix("/api/")
        path = ("kubernetes", version, kind, namespace, name)
        return any(path[: len(scope.path)] == scope.path for scope in self.mutation.writes)


admission: ContextVar[Admission | None] = ContextVar("write_admission", default=None)


def independent(left: Validation | None, a: Admission | None, right: Validation | None, b: Admission | None) -> bool:
    """
    Combine planner evidence with actual observed dependencies before omitting an ordering edge.

    Args:
        left (Validation | None): Older candidate, absent for an exclusive barrier.
        a (Admission | None): Older candidate's approved planner effects.
        right (Validation | None): New candidate.
        b (Admission | None): New candidate's approved planner effects.

    Returns:
        bool: Only separate approved siblings without concrete read/write overlap commute.
    """
    if left is None or right is None or a is None or b is None or a.batch is not b.batch:
        return False
    if a.mutation.name == b.mutation.name or not a.covers(left) or not b.covers(right) or conflicts(a.mutation, b.mutation):
        return False
    assert left.intent is not None and right.intent is not None
    return not left.depends_on(right.api, right.intent.target) and not right.depends_on(left.api, left.intent.target)


class DispatchGraph:
    """
    Keep dependent writes ordered while dispatching the ready independent frontier.
    """

    def __init__(self, limit: int = 1) -> None:
        """
        Bound slots held through validation, authorization and acknowledged transport.

        Args:
            limit (int): Maximum concurrently held writer slots, between one and 32.
        """
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("write concurrency must be an integer between 1 and 32")
        self.limit = limit
        self.graph: nx.DiGraph[object] = nx.DiGraph()
        self.active: set[object] = set()
        self.wake = asyncio.Event()
        self.barriers: dict[asyncio.Task[object] | None, object] = {}

    def locked(self) -> bool:
        """
        Report any admitted predecessor for target revalidation after queue delay.

        Returns:
            bool: Whether another write or exclusive barrier is present.
        """
        return bool(self.graph)

    def add(self, token: object, receipt: Validation | None, proof: Admission | None) -> None:
        """
        Add edges from existing dependent work; never infer precedence from patch contents.

        Args:
            token (object): Unique candidate identity.
            receipt (Validation | None): Frozen dependency contract or an exclusive barrier.
            proof (Admission | None): Explicit planner evidence, absent for ordinary writes.

        Returns:
            None: Insertion order is retained only where independence is unproven.
        """
        predecessors = [old for old, data in self.graph.nodes(data=True) if not independent(data["receipt"], data["proof"], receipt, proof)]
        self.graph.add_node(token, receipt=receipt, proof=proof)
        self.graph.add_edges_from((old, token) for old in predecessors)
        self.wake.set()

    async def enter(self, token: object) -> None:
        """
        Consume a ready slot without blocking other independent ready candidates.

        Args:
            token (object): Previously registered candidate.

        Returns:
            None: The slot remains held until explicit removal after all side effects join.
        """
        while True:
            self.wake.clear()
            if self.graph.in_degree(token) == 0 and len(self.active) < self.limit:
                self.active.add(token)
                return
            await self.wake.wait()

    def remove(self, token: object) -> None:
        """
        Release a finished or cancelled node and expose newly ready dependencies.

        Args:
            token (object): Candidate to forget.

        Returns:
            None: Successors must still validate; completion alone never proves their assumptions.
        """
        self.active.discard(token)
        if token in self.graph:
            self.graph.remove_node(token)
        self.wake.set()

    @asynccontextmanager
    async def hold(self, token: object, receipt: Validation) -> AsyncIterator[None]:
        """
        Hold one concrete write node until its transport has acknowledged or joined.

        Args:
            token (object): Backlog identity.
            receipt (Validation): Target and observed dependency contract.

        Yields:
            None: Control while the dependency graph owns this writer slot.
        """
        self.add(token, receipt, admission.get())
        try:
            await self.enter(token)
            if len(self.active) > 1:
                decision(
                    "polyad.kubernetes.write_parallel",
                    "Independent planned changes are using concurrent writer slots.",
                    key=receipt.intent.target if receipt.intent else None,
                    outcome="admitted",
                    reason="planner_and_dependency_checks_passed",
                    attributes={"polyad.write.in_flight_slots": len(self.active)},
                )
            yield
        finally:
            self.remove(token)

    async def __aenter__(self) -> None:
        """
        Acquire an exclusive barrier for callers that deliberately suspend dispatch.

        Returns:
            None: All preceding writes completed and subsequent writes must wait.
        """
        token = object()
        self.add(token, None, None)
        try:
            await self.enter(token)
        except BaseException:
            self.remove(token)
            raise
        self.barriers[asyncio.current_task()] = token

    async def __aexit__(self, *_: object) -> None:
        """
        Release the current task's exclusive barrier.

        Args:
            *_ (object): Context-manager exception details.

        Returns:
            None: Exceptions propagate unchanged.
        """
        self.remove(self.barriers.pop(asyncio.current_task()))
