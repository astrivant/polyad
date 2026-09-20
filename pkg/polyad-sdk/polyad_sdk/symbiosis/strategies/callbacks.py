"""
Compose observation logging and application callbacks selected by delta family.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from polyad_sdk.symbiosis.strategies.base import AdaptationStrategy

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from polyad_sdk.symbiosis.models import Change, Environment

__all__ = (
    "CallbackStrategy",
    "DecisionStrategy",
    "ObserveStrategy",
    "ResourceStrategy",
    "TopologyStrategy",
)


class ObserveStrategy(AdaptationStrategy):
    """
    Log the initial SDK snapshot and the names of fields that change afterward.

    Use this to see what your service receives before adding a response. Logs
    include availability, candidate counts and changed field names; application
    data stays out of the log messages.

    Attributes:
        logger (logging.Logger): Application-configured logger; no handlers are installed.
    """

    logger: logging.Logger

    def __init__(self, logger: logging.Logger | None = None) -> None:
        """
        Select the application's logger or the SDK adaptation logger.

        Args:
            logger (logging.Logger | None): Logger receiving informational observations.
        """
        self.logger = logger if logger is not None else logging.getLogger("polyad_sdk.symbiosis")

    def adapt(self, change: Change, current: Environment) -> None:
        """
        Report the baseline or changed field paths without logging entire payloads.

        Args:
            change (Change): Authorized baseline or meaningful changes.
            current (Environment): Fresh availability and candidate-neighbor view.

        Returns:
            None: Observation records have been handed to the configured logger.
        """
        if change.baseline:
            self.logger.info("Adaptation baseline: available=%s candidates=%s", current.available, len(current.candidates))
        for delta in change.deltas:
            self.logger.info("Adaptation %s: %s; available=%s", delta.kind, ".".join(delta.path), current.available)


class CallbackStrategy(AdaptationStrategy):
    """
    Run an application callback when selected parts of the SDK's view change.

    The first snapshot, called the baseline, always reaches the callback so it
    can initialize application state. Updates about whether the view is usable
    also always reach it. Use paths to select other fields, such as resources
    or topology.outgoing; an empty sequence selects every delivered change.

    Attributes:
        callback (Callable[[Change, Environment], None]): Bounded application behavior.
        paths (tuple[str, ...]): Selected delta prefixes; empty selects all changes.
    """

    callback: Callable[[Change, Environment], None]
    paths: tuple[str, ...]

    def __init__(self, callback: Callable[[Change, Environment], None], *, paths: Sequence[str] = ()) -> None:
        """
        Bind behavior and copy its path selection before the service starts.

        Args:
            callback (Callable[[Change, Environment], None]): Application-owned component callback.
            paths (Sequence[str]): Dot-separated prefixes, such as resources or topology.outgoing.

        Raises:
            TypeError: The callback is not callable or paths is a bare string.
            ValueError: A path contains empty components.
        """
        if not callable(callback) or isinstance(paths, str):
            raise TypeError("provide a callable and a sequence of delta prefixes")
        selected = tuple(paths)
        if any(not isinstance(path, str) or not path or any(not part for part in path.split(".")) for path in selected):
            raise ValueError("delta prefixes must contain nonempty path components")
        self.callback, self.paths = callback, selected

    def adapt(self, change: Change, current: Environment) -> None:
        """
        Deliver matching evidence and freshness changes to the application component.

        Args:
            change (Change): Baseline or meaningful delta, retaining parent additions/removals.
            current (Environment): Fresh view for admission, withdrawal or unknown-data handling.

        Returns:
            None: A selected callback completed, or this component had no relevant change.
        """
        if (
            not self.paths
            or change.baseline
            or change.matching("available")
            or change.matching("reason")
            or (current.available, current.reason) != (change.after.available, change.after.reason)
            or any(change.matching(path) for path in self.paths)
        ):
            self.callback(change, current)


class TopologyStrategy(CallbackStrategy):
    """
    Update an application's neighborhood behavior on topology and connection changes.

    The callback can revise routing candidates or connection intent. Use the
    fresh environment to check availability and active grants before new work.
    During Pod rollout, eviction or scale-down, stop new assignments to retiring
    executions and drain accepted work. A new execution still needs application
    readiness checks. Timeouts without a membership change require local health
    checks and do not themselves trigger this topology callback.
    """

    def __init__(self, callback: Callable[[Change, Environment], None]) -> None:
        """
        Bind the component responsible for neighbors and connection lifetimes.

        Args:
            callback (Callable[[Change, Environment], None]): Application routing/connection handler.
        """
        super().__init__(callback, paths=("topology", "connections"))


class ResourceStrategy(CallbackStrategy):
    """
    Update application capacity or admission intent from resource measurements.

    Missing or expired measurements remain unknown. The callback owns any
    threshold, cooldown or worker-profile policy applied to that evidence.
    While new Pods wait for node capacity, use available measurements to limit
    admission or propose an approved local profile. This component does not
    collect cgroup usage or Kubernetes scheduling Events; supply local pressure
    through the application's own observation and admission loop.
    """

    def __init__(self, callback: Callable[[Change, Environment], None]) -> None:
        """
        Bind the component responsible for resource and pressure observations.

        Args:
            callback (Callable[[Change, Environment], None]): Application capacity/admission handler.
        """
        super().__init__(callback, paths=("resources",))


class DecisionStrategy(CallbackStrategy):
    """
    Interpret the containing graph's observed Soul searching decisions.

    The callback distinguishes proposals, committed decisions and readiness
    before updating application behavior. An observed proposal grants no action.
    A pending capacity-preparation or traffic decision may require continued
    backpressure while Kubernetes provisions resources. Check actual peer
    readiness after the applied phase before increasing application admission.
    """

    def __init__(self, callback: Callable[[Change, Environment], None]) -> None:
        """
        Bind the component responsible for operator decision observations.

        Args:
            callback (Callable[[Change, Environment], None]): Application policy-decision handler.
        """
        super().__init__(callback, paths=("decision",))
