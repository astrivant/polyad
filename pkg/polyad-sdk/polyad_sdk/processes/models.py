"""
Describe approved subprocess profiles and their observable execution results.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.strategies.base import ConstraintStrategy

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from typing import Literal

    from polyad_sdk.processes.process import ManagedProcess

__all__ = (
    "PlanResult",
    "ProcessPlan",
    "ProcessSpec",
)


def _name(value: str) -> None:
    """
    Validate bounded names used as profile identities and telemetry dimensions.

    Args:
        value (str): Application-owned process or profile name.

    Returns:
        None: Invalid names raise before any process starts.
    """
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise ValueError("names require 1-64 letters, digits, dots, underscores or hyphens")


@dataclass(frozen=True)
class ProcessSpec:
    """
    Construct one worker using an argument vector and explicit lifecycle callbacks.

    Attributes:
        name (str): Stable role within a plan; unchanged specifications reuse a live worker.
        argv (tuple[str, ...]): Executable and arguments, passed without a shell.
        ready (Callable[[ManagedProcess], bool]): Bounded check of application readiness.
        drain (Callable[[ManagedProcess], bool]): Idempotently request draining; True acknowledges settled work.
        environment (Mapping[str, str]): Immutable overrides on the supervisor's environment snapshot.
        cwd (Path | None): Optional working directory.
    """

    name: str
    argv: tuple[str, ...] = field(repr=False)
    ready: Callable[[ManagedProcess], bool] = field(repr=False)
    drain: Callable[[ManagedProcess], bool] = field(repr=False)
    environment: Mapping[str, str] = field(default_factory=dict, repr=False)
    cwd: Path | None = None

    def __post_init__(self) -> None:
        """
        Freeze arguments and environment before a plan can be submitted.

        Returns:
            None: Invalid specifications fail before workers or export threads start.
        """
        _name(self.name)
        if isinstance(self.argv, str) or not self.argv or any(not isinstance(arg, str) or "\0" in arg for arg in self.argv):
            raise ValueError("argv must be a nonempty sequence of strings without NUL bytes")
        if not self.argv[0]:
            raise ValueError("argv requires an executable")
        if not callable(self.ready) or not callable(self.drain):
            raise TypeError("ready and drain must be callable application lifecycle checks")
        environment = dict(self.environment)
        if any(
            not isinstance(key, str) or not key or "=" in key or "\0" in key or not isinstance(value, str) or "\0" in value
            for key, value in environment.items()
        ):
            raise ValueError("environment must contain valid string names and values")

        # Freeze nested inputs too: a frozen dataclass alone would still allow
        # callers to mutate a supplied list or environment after plan approval.
        object.__setattr__(self, "argv", tuple(self.argv))
        object.__setattr__(self, "environment", MappingProxyType(environment))

    @classmethod
    def python(
        cls,
        name: str,
        module: str,
        *arguments: str,
        ready: Callable[[ManagedProcess], bool],
        drain: Callable[[ManagedProcess], bool],
        environment: Mapping[str, str] | None = None,
        cwd: Path | None = None,
    ) -> ProcessSpec:
        """
        Run a Python module with the current interpreter and an explicit argument vector.

        Args:
            name (str): Stable worker role.
            module (str): Importable module executed through python -m.
            *arguments (str): Application command arguments.
            ready (Callable[[ManagedProcess], bool]): Application readiness check.
            drain (Callable[[ManagedProcess], bool]): Application draining handshake.
            environment (Mapping[str, str] | None): Environment overrides.
            cwd (Path | None): Optional working directory.

        Returns:
            ProcessSpec: Validated construction data; no process starts yet.
        """
        if not isinstance(module, str) or not module or any(not component.isidentifier() for component in module.split(".")):
            raise ValueError("module must be a dotted Python module name")
        return cls(name, (sys.executable, "-m", module, *arguments), ready, drain, environment or {}, cwd)


@dataclass(frozen=True)
class ProcessPlan:
    """
    Name an approved worker composition and the guards required to activate it.

    Attributes:
        name (str): Profile selected by an adaptation strategy.
        processes (tuple[ProcessSpec, ...]): Desired roles; an empty plan drains all current workers.
        guards (tuple[ConstraintStrategy, ...]): All must be satisfied against a refreshed service view.
    """

    name: str
    processes: tuple[ProcessSpec, ...]
    guards: tuple[ConstraintStrategy, ...] = ()

    def __post_init__(self) -> None:
        """
        Validate fixed plan membership and independent constraint names.

        Returns:
            None: Plans contain only valid, uniquely named process roles and guards.
        """
        _name(self.name)

        # Unique role and guard names give reconciliation an unambiguous mapping
        # from each approved identity to one specification or constraint.
        processes, guards = tuple(self.processes), tuple(self.guards)
        if any(not isinstance(spec, ProcessSpec) for spec in processes):
            raise TypeError("processes must contain ProcessSpec instances")
        if len({spec.name for spec in processes}) != len(processes):
            raise ValueError("process roles must be unique within a plan")
        if any(not isinstance(guard, ConstraintStrategy) for guard in guards):
            raise TypeError("guards must implement ConstraintStrategy")
        if len({guard.name for guard in guards}) != len(guards):
            raise ValueError("plan guard names must be unique")
        object.__setattr__(self, "processes", processes)
        object.__setattr__(self, "guards", guards)


@dataclass(frozen=True)
class PlanResult:
    """
    Report one reconciliation attempt independently of the committed profile.

    Attributes:
        profile (str | None): Profile examined by this attempt.
        state (Literal['Idle', 'Unchanged', 'Applied', 'Blocked', 'Superseded', 'Failed']): Attempt outcome.
        reason (str): Human-readable result without child commands or environment values.
    """

    profile: str | None
    state: Literal["Idle", "Unchanged", "Applied", "Blocked", "Superseded", "Failed"]
    reason: str
