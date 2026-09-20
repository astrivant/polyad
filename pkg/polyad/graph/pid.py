"""
Control spectral refresh intervals and cache targets from causal observations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ("AccuracyTargetPID", "CacheTargetConfig", "CacheTargetPID", "RefreshConfig", "RefreshPID", "certificate_gap")


@dataclass(frozen=True)
class RefreshConfig:
    """
    Define bounded, discrete refresh feedback shared by production and studies.

    Attributes:
        enabled (bool): Include the additional comparator in the churn study.
        proportionalGain (float): Immediate interval reduction per unit error.
        integralGain (float): Interval reduction per accumulated error observation.
        derivativeGain (float): Interval reduction per change in error.
        targetCacheRate (float): Desired fraction of observations attempting cache reuse.
        initialInterval (float): Initial and bias refresh interval, in observations.
        minInterval (float): Shortest permitted refresh interval.
        maxInterval (float): Longest permitted refresh interval.
    """

    enabled: bool = True
    proportionalGain: float = 0.6
    integralGain: float = 0.4
    derivativeGain: float = 0.2
    targetCacheRate: float = 0.0
    initialInterval: float = 4.0
    minInterval: float = 1.0
    maxInterval: float = 8.0

    def __post_init__(self) -> None:
        """
        Reject invalid gains, rates and observation intervals before measurement.

        Returns:
            None: Keep every controller state finite and bounded.
        """
        values = (
            self.proportionalGain,
            self.integralGain,
            self.derivativeGain,
            self.targetCacheRate,
            self.initialInterval,
            self.minInterval,
            self.maxInterval,
        )
        if not isinstance(self.enabled, bool) or any(isinstance(value, bool) or not math.isfinite(value) for value in values):
            raise ValueError("PID controls require a boolean enabled flag and finite numeric settings")
        if min(self.proportionalGain, self.integralGain, self.derivativeGain) < 0:
            raise ValueError("PID gains must be nonnegative")
        if not 0 <= self.targetCacheRate <= 1:
            raise ValueError("PID target cache rate must be between zero and one")
        if not 1 <= self.minInterval <= self.initialInterval <= self.maxInterval:
            raise ValueError("PID intervals must satisfy 1 <= minimum <= initial <= maximum")


class RefreshPID:
    """
    Shorten the refresh interval when a query falls back to the cached partition.

    One controller tick is one graph observation, not elapsed wall time. A cache
    lookup is a controller failure even when its certificate is valid. This is a
    scheduling objective, not a claim that cached results are mathematically unsafe.

    Attributes:
        config (RefreshConfig): Immutable gains, target and actuator bounds.
        interval (float): Refresh interval selected for the next observation.
        age (int): Observations since the latest completed spectral refresh.
        target_cache_rate (float): Current setpoint, fixed unless the outer controller changes it.
    """

    config: RefreshConfig
    interval: float
    age: int
    target_cache_rate: float

    def __init__(self, config: RefreshConfig) -> None:
        """
        Start one independent trajectory immediately after baseline cache priming.

        Args:
            config (RefreshConfig): Validated refresh controls.
        """
        self.config = config
        self.interval = config.initialInterval
        self.age = 0
        self.target_cache_rate = config.targetCacheRate
        self._integral = 0.0
        self._previous_error: float | None = None

    def refresh_due(self) -> bool:
        """
        Decide the current action using only the previous observations.

        Returns:
            bool: Whether the next observation has reached the selected interval.
        """
        return self.age + 1 >= self.interval

    def set_target(self, target: float) -> None:
        """
        Apply the outer loop's setpoint to future observations without derivative kick.

        Args:
            target (float): Desired cache-attempt fraction between zero and one.

        Returns:
            None: Preserve integral history while shifting the derivative reference.
        """
        if isinstance(target, bool) or not math.isfinite(target) or not 0 <= target <= 1:
            raise ValueError("PID target cache rate must be finite and between zero and one")

        # Adjust the previous error by the same setpoint shift. The derivative
        # then responds to the cache event, not an artificial target-change spike.
        if self._previous_error is not None:
            self._previous_error += self.target_cache_rate - target
        self.target_cache_rate = target

    def observe(self, *, cache_attempted: bool, refreshed: bool) -> dict[str, float | bool | int]:
        """
        Update the next interval without consulting graph truth or future events.

        Args:
            cache_attempted (bool): Whether this query entered the cache tier, hit or miss.
            refreshed (bool): Whether spectral work actually rebuilt the partition.

        Returns:
            dict[str, float | bool | int]: Error, PID state and bounded actuator telemetry.
        """
        config = self.config
        error = float(cache_attempted) - self.target_cache_rate

        # Unit-spaced observations make the integral a sum and derivative a
        # first difference. Suppress the derivative kick at initialization.
        derivative = 0.0 if self._previous_error is None else error - self._previous_error
        candidate_integral = self._integral + error if config.integralGain else 0.0
        candidate = (
            config.initialInterval
            - config.proportionalGain * error
            - config.integralGain * candidate_integral
            - config.derivativeGain * derivative
        )

        # Conditional integration prevents an unreachable target from winding
        # up the integral while the refresh interval is already saturated.
        windup = (candidate < config.minInterval and error > 0) or (candidate > config.maxInterval and error < 0)
        if not windup:
            self._integral = candidate_integral
        requested = (
            config.initialInterval
            - config.proportionalGain * error
            - config.integralGain * self._integral
            - config.derivativeGain * derivative
        )
        previous_interval, previous_age = self.interval, self.age
        self.interval = min(config.maxInterval, max(config.minInterval, requested))
        self.age = 0 if refreshed else self.age + 1
        self._previous_error = error
        return {
            "failure": cache_attempted,
            "targetCacheRate": self.target_cache_rate,
            "error": error,
            "integral": self._integral,
            "derivative": derivative,
            "antiWindup": windup,
            "intervalBefore": previous_interval,
            "intervalAfter": self.interval,
            "ageBefore": previous_age,
            "ageAfter": self.age,
            "refreshed": refreshed,
        }


@dataclass(frozen=True)
class CacheTargetConfig:
    """
    Bound a slower outer PID that adjusts cache reuse from time or certificate feedback.

    Attributes:
        updateEvery (int): Inner observations averaged before one outer update.
        proportionalGain (float): Immediate cache-target change per normalized objective error.
        integralGain (float): Cache-target change per accumulated window error.
        derivativeGain (float): Cache-target change per change in window error.
        initialCacheRate (float): Initial and bias cache-attempt target.
        minCacheRate (float): Lowest permitted cache-attempt target.
        maxCacheRate (float): Highest permitted cache-attempt target.
    """

    updateEvery: int = 4
    proportionalGain: float = 0.2
    integralGain: float = 0.05
    derivativeGain: float = 0.02
    initialCacheRate: float = 0.25
    minCacheRate: float = 0.0
    maxCacheRate: float = 0.8

    def __post_init__(self) -> None:
        """
        Validate bounded feedback before collecting time or certificate measurements.

        Returns:
            None: Reject invalid rates, gains and update cadences.
        """
        if type(self.updateEvery) is not int or not 2 <= self.updateEvery <= 128:
            raise ValueError("outer PID updateEvery must be an integer between 2 and 128")
        gains = (self.proportionalGain, self.integralGain, self.derivativeGain)
        rates = (self.initialCacheRate, self.minCacheRate, self.maxCacheRate)
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in (*gains, *rates)):
            raise ValueError("outer PID controls must be finite numbers")
        if min(gains) < 0 or not 0 <= self.minCacheRate <= self.initialCacheRate <= self.maxCacheRate <= 1:
            raise ValueError("outer PID requires nonnegative gains and ordered cache rates within [0, 1]")


class CacheTargetPID:
    """
    Increase the cache target when measured computation exceeds its configured goal.

    Attributes:
        config (CacheTargetConfig): Immutable outer gains, bounds and cadence.
        target (float): Cache-attempt target to apply to subsequent inner observations.
    """

    config: CacheTargetConfig
    target: float

    def __init__(self, config: CacheTargetConfig) -> None:
        """
        Start an independent, empty measurement window and bounded actuator.

        Args:
            config (CacheTargetConfig): Validated feedback settings.
        """
        self.config = config
        self.target = config.initialCacheRate
        self._durations: list[float] = []
        self._time_target: float | None = None
        self._integral = 0.0
        self._previous_error: float | None = None

    def observe(self, duration: float, time_target: float) -> dict[str, float | int | bool | None]:
        """
        Update from past measured computation only, never from the exact graph oracle.

        Args:
            duration (float): Completed inner calculation time, excluding oracle and outer bookkeeping.
            time_target (float): Administrator-style experimental mean-time goal, in seconds.

        Returns:
            dict[str, float | int | bool | None]: Recorded signal, window update and next cache target.
        """
        if not math.isfinite(duration) or duration < 0 or not math.isfinite(time_target) or time_target <= 0:
            raise ValueError("outer PID requires a nonnegative finite duration and positive finite time target")
        config = self.config

        # Do not average samples from different latency objectives. Drop a
        # partial old window and suppress derivative kick at a setpoint change.
        reset = self._time_target != time_target
        if reset:
            self._durations.clear()
            self._previous_error = None
        self._time_target = time_target
        self._durations.append(duration)
        report: dict[str, float | int | bool | None] = {
            "observedDurationSeconds": duration,
            "timeTargetSeconds": time_target,
            "targetBefore": self.target,
            "targetAfter": self.target,
            "updated": False,
            "windowReset": reset,
            "samples": len(self._durations),
            "meanDurationSeconds": None,
            "normalizedError": None,
            "integral": self._integral,
            "derivative": None,
            "antiWindup": False,
        }
        if len(self._durations) < config.updateEvery:
            return report

        # One outer tick spans a complete window. Positive error means the
        # computation is too expensive, so the actuator requests more cache use.
        mean = sum(self._durations) / len(self._durations)
        self._durations.clear()
        error = mean / time_target - 1
        derivative = 0.0 if self._previous_error is None else error - self._previous_error
        candidate_integral = self._integral + error if config.integralGain else 0.0
        candidate = (
            config.initialCacheRate
            + config.proportionalGain * error
            + config.integralGain * candidate_integral
            + config.derivativeGain * derivative
        )

        # Both loops have anti-windup. The outer one cannot ask for an unlimited
        # cache rate just because an unattainable time target remains unmet.
        windup = (candidate > config.maxCacheRate and error > 0) or (candidate < config.minCacheRate and error < 0)
        if not windup:
            self._integral = candidate_integral
        requested = (
            config.initialCacheRate
            + config.proportionalGain * error
            + config.integralGain * self._integral
            + config.derivativeGain * derivative
        )
        self.target = min(config.maxCacheRate, max(config.minCacheRate, requested))
        self._previous_error = error
        return {
            **report,
            "updated": True,
            "meanDurationSeconds": mean,
            "normalizedError": error,
            "integral": self._integral,
            "derivative": derivative,
            "antiWindup": windup,
            "targetAfter": self.target,
        }


def certificate_gap(lower: float, upper: float | None) -> float:
    """
    Normalize certified uncertainty without using an exact reference or dividing by zero.

    Args:
        lower (float): Finite nonnegative lower bound on the current graph's constant.
        upper (float | None): Finite nonnegative upper witness, or missing when work is incomplete.

    Returns:
        float: Width divided by the upper bound, in [0, 1]; missing upper bounds mean full uncertainty.
    """
    if not math.isfinite(lower) or lower < 0:
        raise ValueError("certificate lower bound must be finite and nonnegative")
    if upper is None:
        return 1.0
    if not math.isfinite(upper) or upper < 0 or lower > upper + 1e-9 * max(1, lower, upper):
        raise ValueError("certificate upper bound must be finite, nonnegative and at least the lower bound")

    # A certified zero constant has zero uncertainty, unlike a positive witness
    # with a zero lower bound, which has no finite relative-error guarantee.
    return max(0.0, (upper - lower) / upper) if upper else 0.0


class AccuracyTargetPID:
    """
    Reduce cache reuse when certified uncertainty exceeds an accuracy objective.

    The signal is not actual estimation error. For h in [L, U], a gap
    g = (U-L)/U bounds (U-h)/h by g/(1-g), provided g < 1. Therefore a
    desired relative-error bound r corresponds to g <= r/(1+r).

    Attributes:
        config (CacheTargetConfig): Outer gains, cache-rate bounds and observation window.
        target (float): Cache-attempt target for future queries; zero requests fresh work every query.
    """

    config: CacheTargetConfig
    target: float

    def __init__(self, config: CacheTargetConfig) -> None:
        """
        Start an independent accuracy controller without any oracle observations.

        Args:
            config (CacheTargetConfig): Validated gains, cache bounds and update cadence.
        """
        self.config = config
        self.target = config.initialCacheRate
        self._gaps: list[float] = []
        self._relative_target: float | None = None
        self._integral = 0.0
        self._previous_error: float | None = None

    def observe(self, lower: float, upper: float | None, relative_target: float) -> dict[str, float | int | bool | None]:
        """
        Learn from the reduced certificate before exact fallback can conceal its uncertainty.

        Args:
            lower (float): Lower bound of the last reduced certificate on current edges.
            upper (float | None): Upper witness of that certificate, not an independent oracle.
            relative_target (float): Positive finite soft relative-error objective, such as 0.25 for 25 percent.

        Returns:
            dict[str, float | int | bool | None]: Observed gap, bound, objective and causal PID update.
        """
        if isinstance(relative_target, bool) or not math.isfinite(relative_target) or relative_target <= 0:
            raise ValueError("accuracy PID requires a positive finite relative-error target")
        gap = certificate_gap(lower, upper)
        gap_target = relative_target / (1 + relative_target)
        reset = self._relative_target != relative_target
        if reset:
            self._gaps.clear()
            self._previous_error = None
        self._relative_target = relative_target
        self._gaps.append(gap)
        report: dict[str, float | int | bool | None] = {
            "observedGap": gap,
            "relativeErrorBound": gap / (1 - gap) if gap < 1 else None,
            "relativeErrorTarget": relative_target,
            "gapTarget": gap_target,
            "objectiveMet": gap <= gap_target,
            "targetBefore": self.target,
            "targetAfter": self.target,
            "updated": False,
            "windowReset": reset,
            "samples": len(self._gaps),
            "meanGap": None,
            "normalizedError": None,
            "integral": self._integral,
            "derivative": None,
            "antiWindup": False,
        }
        config = self.config
        if len(self._gaps) < config.updateEvery:
            return report

        # Reverse the time controller's direction: excessive uncertainty must
        # reduce reuse, not reward the faster but poorly certified cached cut.
        mean = sum(self._gaps) / len(self._gaps)
        self._gaps.clear()
        error = 1 - mean / gap_target
        derivative = 0.0 if self._previous_error is None else error - self._previous_error
        candidate_integral = self._integral + error if config.integralGain else 0.0
        requested = (
            config.initialCacheRate
            + config.proportionalGain * error
            + config.integralGain * candidate_integral
            + config.derivativeGain * derivative
        )

        # Clamp the requested actuator while freezing outward integral growth.
        # Persistent bad certificates can request zero reuse; this schedules
        # fresh work, but cannot promise that fresh spectral bounds will improve.
        windup = (requested < config.minCacheRate and error < 0) or (requested > config.maxCacheRate and error > 0)
        if not windup:
            self._integral = candidate_integral
        self.target = min(config.maxCacheRate, max(config.minCacheRate, requested))
        self._previous_error = error
        return {
            **report,
            "updated": True,
            "meanGap": mean,
            "normalizedError": error,
            "integral": self._integral,
            "derivative": derivative,
            "antiWindup": windup,
            "targetAfter": self.target,
        }
