"""
Define structural policy admission failures.
"""

from __future__ import annotations

__all__ = ("RuleViolation",)


class RuleViolation(ValueError):
    """
    Reject a graph family that violates an engineer-defined structural rule.
    """
