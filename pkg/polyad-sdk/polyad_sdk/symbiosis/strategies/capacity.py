"""
Assess graph and container capacity and propose bounded application profiles.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import TYPE_CHECKING

from polyad_sdk.runtime.context import ContainerResources
from polyad_sdk.symbiosis.strategies.base import AdaptationStrategy, ConstraintAssessment, ConstraintStrategy

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Literal

    from polyad_sdk.symbiosis.models import Change, Environment


class ResourceBudgetStrategy(ConstraintStrategy):
    """
    Check whether a proposed change fits a configured graph resource budget.

    The check adds reserve, your estimate of the change's extra resource use,
    to the observed usage and compares the total with maximum. Include the cost
    of running old and replacement workers together during a rollout. Express
    all three values in the same unit. Missing or stale measurements produce
    an unknown result, so the application can wait for a usable measurement.

    Your scheduling code reserves capacity when it acts on a successful check,
    preventing concurrent changes from claiming the same remaining capacity.
    """

    def __init__(
        self,
        name: str,
        publish: Callable[[ConstraintAssessment], None],
        *,
        metric: str,
        maximum: float,
        reserve: float = 0,
    ) -> None:
        """
        Bind a measured graph resource and the headroom required by one protected action.

        Args:
            name (str): Constraint identity.
            publish (Callable[[ConstraintAssessment], None]): Application assessment sink.
            metric (str): Dot-separated path below Environment.resources.
            maximum (float): Inclusive resource ceiling in the metric's unit.
            reserve (float): Additional simultaneous use required while applying the mutation.

        Raises:
            ValueError: A metric path, ceiling or reserve is invalid.
        """
        super().__init__(name, publish)
        if not isinstance(metric, str) or not metric or any(not part for part in metric.split(".")):
            raise ValueError("metric must be a nonempty resource path")
        if any(not _nonnegative(value) for value in (maximum, reserve)) or reserve > maximum:
            raise ValueError("maximum and reserve must be finite nonnegative numbers with reserve <= maximum")
        self._metric, self._maximum, self._reserve = tuple(metric.split(".")), maximum, reserve

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Include the proposed operation's overlap before deciding whether capacity fits.

        Args:
            current (Environment): Fresh graph resource observations.

        Returns:
            ConstraintAssessment: Whether observed usage plus reserved headroom fits the configured maximum.
        """
        value: object = current.resources if current.available else None
        for part in self._metric:
            value = value.get(part) if isinstance(value, Mapping) else None
        if not _nonnegative(value):
            return ConstraintAssessment(self.name, "unknown", "Resource usage is missing, expired or invalid")
        assert isinstance(value, (int, float))
        fits = value <= self._maximum - self._reserve
        return ConstraintAssessment(
            self.name, "satisfied" if fits else "blocked", f"Usage: {value}; additional reserve: {self._reserve}; ceiling: {self._maximum}"
        )


class ContainerBudgetStrategy(ConstraintStrategy):
    """
    Check whether this container has enough CPU or memory for additional work.

    Use this before starting child processes or replacing workers. Supply a used
    callback that measures the application and its children, plus a reserve for
    the proposed change. By default, the budget comes from the container's
    Kubernetes resource request, supplied to the SDK through environment variables.
    A smaller positive resource limit caps that budget. Set maximum to choose
    an explicit application budget instead. CPU uses millicores; memory uses bytes.
    """

    def __init__(
        self,
        name: str,
        publish: Callable[[ConstraintAssessment], None],
        *,
        resource: Literal["cpu", "memory"],
        used: Callable[[], float | None],
        resources: ContainerResources | None = None,
        maximum: float | None = None,
        reserve: float = 0,
    ) -> None:
        """
        Configure a local capacity guard using the projected environment as a default.

        Args:
            name (str): Constraint identity.
            publish (Callable[[ConstraintAssessment], None]): Application assessment sink.
            resource (Literal['cpu', 'memory']): CPU in millicores or memory in bytes.
            used (Callable[[], float | None]): Bounded local measurement of total relevant usage.
            resources (ContainerResources | None): Explicit startup selectors; defaults to the projected environment.
            maximum (float | None): Explicit application budget, overriding the projected request.
            reserve (float): Additional overlap or work admission cost, in the same unit.

        Raises:
            ValueError: The resource or numeric capacity settings are invalid.
            TypeError: The usage callback is not callable.
        """
        super().__init__(name, publish)
        if resource not in {"cpu", "memory"}:
            raise ValueError("resource must be cpu or memory")
        if not callable(used):
            raise TypeError("used must be callable")
        if maximum is None:
            maximum = (resources if resources is not None else ContainerResources.from_environment()).budget(resource)
        if not _nonnegative(reserve) or (maximum is not None and (not _nonnegative(maximum) or reserve > maximum)):
            raise ValueError("capacity and reserve must be finite nonnegative numbers with reserve <= capacity")
        self._maximum, self._reserve, self._used, self._resource = maximum, reserve, used, resource

    def evaluate(self, current: Environment) -> ConstraintAssessment:
        """
        Check local aggregate usage and replacement overlap within the application's allowance.

        Args:
            current (Environment): Fresh topology context for considering new assignments.

        Returns:
            ConstraintAssessment: Whether the local change fits, or unknown without a budget or usable measurement.
        """
        if not current.available or self._maximum is None:
            return ConstraintAssessment(self.name, "unknown", "No usable topology or positive projected request; supply an explicit budget")
        used = self._used()
        if not _nonnegative(used):
            return ConstraintAssessment(self.name, "unknown", "Local resource usage is missing or invalid")
        assert isinstance(used, (int, float))
        fits = used <= self._maximum - self._reserve
        unit = "millicores" if self._resource == "cpu" else "bytes"
        return ConstraintAssessment(
            self.name,
            "satisfied" if fits else "blocked",
            f"Container usage: {used}; reserve: {self._reserve}; budget: {self._maximum} {unit}",
        )


class ThresholdStrategy(AdaptationStrategy):
    """
    Suggest switching between two named ways of running your application.

    Each name identifies a profile your application defines, such as normal
    concurrency or reduced concurrency. When the selected graph resource metric
    reaches high, propose the busy profile. When it falls to low, propose idle.
    Between the thresholds, keep the current profile. This gap, called hysteresis,
    prevents small measurement changes from repeatedly switching profiles.

    The active callback reports the profile actually in use; propose asks your
    scheduling code to change it. That code checks capacity, starts replacements
    and lets old workers finish accepted work before stopping them. Missing,
    stale or nonnumeric measurements produce no proposal. Add cooldowns or a
    requirement for sustained demand in your scheduling code or a custom strategy.
    """

    def __init__(
        self,
        metric: str,
        *,
        low: float,
        high: float,
        idle: str,
        busy: str,
        active: Callable[[], str],
        propose: Callable[[str, Change, Environment], None],
    ) -> None:
        """
        Bind a resource metric, two profile names and application-owned state/intent callbacks.

        Args:
            metric (str): Dot-separated path below current.resources, such as backlog.
            low (float): At or below this value, request the idle profile from busy.
            high (float): At or above this value, request the busy profile from idle.
            idle (str): Approved profile used under low demand.
            busy (str): Approved profile used under high demand.
            active (Callable[[], str]): Read the committed application profile.
            propose (Callable[[str, Change, Environment], None]): Request a target through the application's admission path.

        Raises:
            ValueError: The metric, thresholds or profile names are invalid.
            TypeError: The state or proposal callback is not callable.
        """
        if not isinstance(metric, str) or not metric or any(not part for part in metric.split(".")):
            raise ValueError("metric must be a nonempty resource path")
        try:
            invalid = any(type(value) not in (int, float) or not math.isfinite(value) for value in (low, high)) or low >= high
        except OverflowError:
            invalid = True
        if invalid:
            raise ValueError("thresholds must be finite numbers with low < high")
        if any(not isinstance(name, str) or not name.strip() for name in (idle, busy)) or idle == busy:
            raise ValueError("idle and busy must be distinct nonempty profile names")
        if not callable(active) or not callable(propose):
            raise TypeError("active and propose must be callable")
        self._metric, self._low, self._high = tuple(metric.split(".")), low, high
        self._idle, self._busy, self._active, self._propose = idle, busy, active, propose

    def adapt(self, change: Change, current: Environment) -> None:
        """
        Evaluate fresh resource evidence and propose only a change from the committed profile.

        Args:
            change (Change): Baseline or delta whose resources may affect the selected metric.
            current (Environment): Fresh neighborhood and resource observations for this evaluation.

        Returns:
            None: A threshold crossing requests a profile; unknown data and the hysteresis interval hold it.

        Raises:
            ValueError: The application's committed profile is outside the configured pair.
        """
        if not current.available or not (change.baseline or change.matching("resources") or change.matching("available")):
            return
        value: object = current.resources
        for part in self._metric:
            value = value.get(part) if isinstance(value, Mapping) else None
        if type(value) not in (int, float):
            return
        assert isinstance(value, (int, float))
        try:
            finite = math.isfinite(value)
        except OverflowError:
            return
        if not finite:
            return
        active = self._active()
        if active not in (self._idle, self._busy):
            raise ValueError("active profile is outside this strategy's approved pair")
        if active == self._idle and value >= self._high:
            self._propose(self._busy, change, current)
        elif active == self._busy and value <= self._low:
            self._propose(self._idle, change, current)


def _nonnegative(value: object) -> bool:
    """
    Recognize usable capacity numbers without interpreting missing evidence as zero.

    Args:
        value (object): Untrusted metric or configuration value.

    Returns:
        bool: Whether the value is a finite, nonnegative integer or float, excluding booleans.
    """
    if type(value) not in (int, float):
        return False
    assert isinstance(value, (int, float))
    try:
        return math.isfinite(value) and value >= 0
    except OverflowError:
        return False
