"""
Compose Boolean routing expressions without treating missing observations as false.
"""

from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Gate:
    """
    Describe a serializable Boolean expression over named observations.

    Attributes:
        operator (str): Signal, AND, OR, NOT, XOR, or NXOR.
        operands (tuple[Gate, ...]): Child expressions.
        name (str): Observation name for a signal.
    """

    operator: str
    operands: tuple["Gate", ...] = ()
    name: str = ""

    def __post_init__(self) -> None:
        """
        Validate expression shape.

        Returns:
            None: Invalid operators or operand counts raise immediately.
        """
        if self.operator == "signal":
            if not self.name or self.operands:
                raise ValueError("a signal needs a name and no operands")
        elif self.operator == "not":
            if len(self.operands) != 1:
                raise ValueError("NOT needs exactly one operand")
        elif self.operator in {"and", "or", "xor", "nxor"}:
            if len(self.operands) < 2:
                raise ValueError("logic gates need at least two operands")
        else:
            raise ValueError("unknown gate operator")

    def evaluate(self, facts: Mapping[str, bool]) -> bool | None:
        """
        Evaluate with unknown propagation and decisive AND/OR short circuits.

        Args:
            facts (Mapping[str, bool]): Available observations; absent names are unknown.

        Returns:
            bool | None: True, False, or an unresolved result.
        """
        if self.operator == "signal":
            value = facts.get(self.name)
            if value is not None and not isinstance(value, bool):
                raise TypeError("routing observations must be Boolean")
            return value
        values = [operand.evaluate(facts) for operand in self.operands]
        if self.operator == "and" and False in values:
            return False
        if self.operator == "or" and True in values:
            return True
        if None in values:
            return None
        if self.operator == "not":
            return not values[0]
        if self.operator == "and":
            return True
        if self.operator == "or":
            return False
        parity = sum(value is True for value in values) % 2 == 1
        return not parity if self.operator == "nxor" else parity


def Signal(name: str) -> Gate:
    """
    Reference a named Boolean observation.

    Args:
        name (str): Observation key.

    Returns:
        Gate: Signal expression.
    """
    return Gate("signal", name=name)


def AND(*operands: Gate) -> Gate:
    """
    Require all operands to be true.

    Args:
        *operands (Gate): Two or more expressions.

    Returns:
        Gate: Conjunction.
    """
    return Gate("and", operands)


def OR(*operands: Gate) -> Gate:
    """
    Require at least one true operand.

    Args:
        *operands (Gate): Two or more expressions.

    Returns:
        Gate: Disjunction.
    """
    return Gate("or", operands)


def NOT(operand: Gate) -> Gate:
    """
    Negate a known operand while preserving unknown.

    Args:
        operand (Gate): Expression to negate.

    Returns:
        Gate: Negation.
    """
    return Gate("not", (operand,))


def XOR(*operands: Gate) -> Gate:
    """
    Require an odd number of true operands.

    Args:
        *operands (Gate): Two or more expressions.

    Returns:
        Gate: Odd parity.
    """
    return Gate("xor", operands)


def NXOR(*operands: Gate) -> Gate:
    """
    Require an even number of true operands, also called XNOR.

    Args:
        *operands (Gate): Two or more expressions.

    Returns:
        Gate: Even parity.
    """
    return Gate("nxor", operands)
