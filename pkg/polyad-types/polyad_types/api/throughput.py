"""
Report application demand and completed work against one graph incarnation and revision.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from attrs import field, frozen
from cattrs.gen import make_dict_structure_fn, override

from polyad_types.serialization import converter


@frozen
class DemandSource:
    """
    Identify an administrator-selected application signal for profile thresholds.

    Attributes:
        name (str): Exact signal name the authorized reporter must supply.
        unit (str): Exact measurement unit, such as jobs, sessions or bytes-per-second.
    """

    name: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})
    unit: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})

    def __attrs_post_init__(self) -> None:
        """
        Require bounded explicit signal identities.

        Returns:
            None: No return value.
        """
        if any(not isinstance(value, str) or not value.strip() or len(value) > 64 for value in (self.name, self.unit)):
            raise ValueError("demand signal name and unit must be nonempty strings of at most 64 characters")


@frozen
class DemandSample:
    """
    Report the selected signal under the enclosing throughput report's identity and timestamp.

    Attributes:
        name (str): Signal name matching the administrator's demand source.
        unit (str): Measurement unit matching that source.
        value (float): Finite nonnegative measured demand; zero never activates a profile.
    """

    name: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})
    unit: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})
    value: float = field(metadata={"schema": {"minimum": 0}})

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous units and invalid measurement values.

        Returns:
            None: No return value.
        """
        DemandSource(self.name, self.unit)
        if type(self.value) not in (int, float) or not math.isfinite(self.value) or self.value < 0:
            raise ValueError("demand value must be a finite nonnegative number")


converter.register_structure_hook(
    DemandSample,
    make_dict_structure_fn(
        DemandSample,
        converter,
        name=override(struct_hook=lambda value, _: value),
        unit=override(struct_hook=lambda value, _: value),
        value=override(struct_hook=lambda value, _: value),
    ),
)
converter.register_structure_hook(
    DemandSource,
    make_dict_structure_fn(
        DemandSource,
        converter,
        name=override(struct_hook=lambda value, _: value),
        unit=override(struct_hook=lambda value, _: value),
    ),
)


@frozen
class TrafficSample:
    """
    Report completed work and spare sustainable capacity for one routing destination.

    Attributes:
        route (str): Configured traffic route name.
        target (str): Configured downstream path, including a graph replica ordinal where applicable.
        targetUid (str): UID of the execution resource at that path, never the reusable definition.
        generation (int): Generation of that execution resource when measured.
        completedPerSecond (float): Successfully completed work over the aggregate report's window and unit.
        headroomPerSecond (float): Estimated additional sustainable work per second, in that same unit.
    """

    route: str
    target: str
    targetUid: str
    generation: int
    completedPerSecond: float
    headroomPerSecond: float

    def __attrs_post_init__(self) -> None:
        """
        Require an exact execution revision and finite nonnegative rates.

        Returns:
            None: No return value.
        """
        if not self.route or not self.target or not self.targetUid or type(self.generation) is not int or self.generation < 1:
            raise ValueError("traffic reports require route, target execution UID and positive generation")
        if any(
            isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in (self.completedPerSecond, self.headroomPerSecond)
        ):
            raise ValueError("traffic throughput and headroom must be finite and nonnegative")
        if not math.isfinite(self.completedPerSecond + self.headroomPerSecond):
            raise ValueError("traffic sustainable capacity must be finite")


@frozen
class ThroughputSample:
    """
    Supply one aggregate measurement from a graph's designated application reporter.

    Attributes:
        graph (str): Graph name in the API listener's namespace.
        graphUid (str): Exact target incarnation, preventing name reuse.
        generation (int): Topology revision whose throughput was measured.
        observedAt (str): Timezone-aware end of the application's measurement window.
        unit (str): Work unit matching the graph's throughput policy.
        offeredPerSecond (float): Aggregate arrival or demanded work rate over that window.
        completedPerSecond (float): Successfully completed work rate over the same window.
        kind (Literal['Graph', 'PolyGraph']): Target boundary kind.
        traffic (tuple[TrafficSample, ...]): Per-destination measurements from the same window for Headroom routing.
        demand (DemandSample | None): Optional configured signal, measured under this report's clock and graph revision.
    """

    graph: str
    graphUid: str
    generation: int
    observedAt: str
    unit: str
    offeredPerSecond: float
    completedPerSecond: float
    kind: Literal["Graph", "PolyGraph"] = "Graph"
    traffic: tuple[TrafficSample, ...] = field(default=(), metadata={"schema": {"maxItems": 256}})
    demand: DemandSample | None = None

    def __attrs_post_init__(self) -> None:
        """
        Reject invalid rates, missing identities and ambiguous timestamps.

        Returns:
            None: No return value.
        """
        if not self.graph or not self.graphUid or self.kind not in {"Graph", "PolyGraph"} or not self.unit:
            raise ValueError("throughput samples require a graph identity and work unit")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("throughput samples require a positive graph generation")
        if datetime.fromisoformat(self.observedAt.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("throughput observations require a timezone")
        for value in (self.offeredPerSecond, self.completedPerSecond):
            if isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("throughput rates must be finite and nonnegative")
        if len(self.traffic) > 256 or len({(item.route, item.target) for item in self.traffic}) != len(self.traffic):
            raise ValueError("traffic samples require at most 256 unique route and target pairs")
