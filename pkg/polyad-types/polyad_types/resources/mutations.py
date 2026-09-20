"""
Describe mutation effects, optimistic preconditions and auditable execution plans.
"""

from __future__ import annotations

from attrs import frozen


@frozen
class Scope:
    """
    Address an object, field or shared invariant using hierarchical path segments.

    Attributes:
        path (tuple[str, ...]): Canonical segments; a prefix covers every descendant.
    """

    path: tuple[str, ...]

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous or mutable scope addresses.

        Returns:
            None: No return value.
        """
        if not isinstance(self.path, tuple) or not self.path or any(not isinstance(part, str) or not part for part in self.path):
            raise ValueError("scope paths require a nonempty tuple of nonempty strings")

    def overlaps(self, other: Scope) -> bool:
        """
        Match the same scope or an ancestor in either direction.

        Args:
            other (Scope): Address to compare.

        Returns:
            bool: Whether the addressed areas intersect.
        """

        # A write to an ancestor conflicts with reads or writes anywhere in its subtree.
        length = min(len(self.path), len(other.path))
        return self.path[:length] == other.path[:length]


@frozen
class Precondition:
    """
    Require an exact refreshed observation before dispatch.

    Attributes:
        scope (Scope): Observation address, implicitly read by the mutation.
        expected (str | None): Expected value; None explicitly requires absence.
    """

    scope: Scope
    expected: str | None


@frozen
class BudgetDelta:
    """
    Declare an integer change to a shared capacity counter.

    Attributes:
        budget (str): Shared counter identity.
        amount (int): Signed change after successful completion.
    """

    budget: str
    amount: int


@frozen
class Budget:
    """
    Bound a shared counter at every possible order within an execution batch.

    Attributes:
        name (str): Shared counter identity.
        current (int): Observed initial usage.
        minimum (int): Inclusive lower bound.
        maximum (int): Inclusive upper bound.
    """

    name: str
    current: int
    minimum: int
    maximum: int


@frozen
class Mutation:
    """
    Describe an operation without embedding executable callbacks or credentials.

    Attributes:
        name (str): Unique operation identity within a plan.
        reads (tuple[Scope, ...]): State and invariant dependencies.
        writes (tuple[Scope, ...]): Changed state, including transport conflict scopes.
        preconditions (tuple[Precondition, ...]): Required refreshed observations.
        after (tuple[str, ...]): Explicit predecessor operation names.
        deltas (tuple[BudgetDelta, ...]): Changes to declared shared counters.
        effects_complete (bool): Compiler adapter asserts all relevant effects are modeled.
    """

    name: str
    reads: tuple[Scope, ...] = ()
    writes: tuple[Scope, ...] = ()
    preconditions: tuple[Precondition, ...] = ()
    after: tuple[str, ...] = ()
    deltas: tuple[BudgetDelta, ...] = ()
    effects_complete: bool = False


@frozen
class Ordering:
    """
    Explain why two operations must execute in sequence.

    Attributes:
        before (str): Earlier operation.
        after (str): Later operation.
        reasons (tuple[str, ...]): Explicit dependencies or conservative conflict explanations.
    """

    before: str
    after: str
    reasons: tuple[str, ...]


@frozen
class Independence:
    """
    Record why two operations may share a batch under declared assumptions.

    Attributes:
        left (str): First operation identity.
        right (str): Second operation identity.
        assumptions (tuple[str, ...]): Conditions supporting modeled independence, not runtime equivalence proofs.
    """

    left: str
    right: str
    assumptions: tuple[str, ...]


@frozen
class MutationPlan:
    """
    Retain source effects, ordering evidence and bounded execution batches.

    Attributes:
        mutations (tuple[Mutation, ...]): Operations in deterministic dependency order.
        budgets (tuple[Budget, ...]): Initial shared capacity observations.
        orderings (tuple[Ordering, ...]): Required ordering with explanations.
        independences (tuple[Independence, ...]): Evidence for each pair admitted to the same batch.
        batches (tuple[tuple[str, ...], ...]): Concurrent groups separated by completion barriers.
        max_parallelism (int): Upper bound on in-flight callbacks.
    """

    mutations: tuple[Mutation, ...]
    budgets: tuple[Budget, ...]
    orderings: tuple[Ordering, ...]
    independences: tuple[Independence, ...]
    batches: tuple[tuple[str, ...], ...]
    max_parallelism: int
