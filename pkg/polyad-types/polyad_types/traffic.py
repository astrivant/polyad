"""
Configure percentage routing separately from unweighted graph connectivity.
"""

from __future__ import annotations

import re

from attrs import field, frozen


@frozen
class TrafficDestination:
    """
    Select a downstream node subtree and bound its share of requests.

    Attributes:
        target (str): Downstream node path, such as workers/replica-0/entrypoint, whose pods form a subset.
        weight (int): Configured percentage of new HTTP requests.
        minWeight (int): Smallest percentage allowed by automatic adjustment.
        maxWeight (int): Largest percentage allowed by automatic adjustment.
    """

    target: str = field(metadata={"schema": {"minLength": 1, "maxLength": 2047, "pattern": "^[a-z0-9-]+(/[a-z0-9-]+)*$"}})
    weight: int = field(metadata={"schema": {"minimum": 0, "maximum": 100}})
    minWeight: int = field(default=0, metadata={"schema": {"minimum": 0, "maximum": 100}})
    maxWeight: int = field(default=100, metadata={"schema": {"minimum": 0, "maximum": 100}})

    def __attrs_post_init__(self) -> None:
        """
        Require integer percentages within the declared range.

        Returns:
            None: No return value.
        """
        if any(type(value) is not int for value in (self.weight, self.minWeight, self.maxWeight)):
            raise ValueError("traffic weights must be integer percentages")
        if not 0 <= self.minWeight <= self.weight <= self.maxWeight <= 100:
            raise ValueError("traffic weights must respect their bounds between zero and 100")
        if len(self.target.split("/")) > 32 or any(
            not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", part) for part in self.target.split("/")
        ):
            raise ValueError("traffic targets require one through 32 local node path segments")


@frozen
class TrafficRoute:
    """
    Split mesh HTTP requests to one Service among downstream node subtrees.

    Attributes:
        name (str): Unique route name within this graph.
        source (str): Local caller node; selects its descendant mesh pods.
        service (str): Existing Service in the graph namespace covering all destination pods.
        port (int): Service port carrying HTTP, HTTP/2 or gRPC traffic.
        destinations (tuple[TrafficDestination, ...]): Distinct downstream subsets whose percentages sum to 100.
    """

    name: str = field(metadata={"schema": {"maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}})
    source: str
    service: str = field(metadata={"schema": {"maxLength": 63, "pattern": "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"}})
    port: int = field(metadata={"schema": {"minimum": 1, "maximum": 65535}})
    destinations: tuple[TrafficDestination, ...] = field(metadata={"schema": {"minItems": 2, "maxItems": 16}})

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous routes and incomplete percentage allocations.

        Returns:
            None: No return value.
        """
        if any(not re.fullmatch(r"[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?", value) for value in (self.name, self.service)):
            raise ValueError("traffic route and service names must be DNS labels")
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise ValueError("traffic service port must be between 1 and 65535")
        targets = [destination.target for destination in self.destinations]
        if not 2 <= len(targets) <= 16 or len(set(targets)) != len(targets):
            raise ValueError("traffic routes require two through 16 distinct destinations")
        if any(right.startswith(left + "/") for left in targets for right in targets if left != right):
            raise ValueError("traffic destination subtrees cannot overlap")
        if sum(destination.weight for destination in self.destinations) != 100:
            raise ValueError("traffic percentages must sum to 100")


@frozen
class TrafficWeights:
    """
    Approve a calibrated traffic distribution for one application demand tier.

    Attributes:
        route (str): Name of the configured traffic route.
        weights (dict[str, int]): Complete target-to-percentage mapping for that route.
    """

    route: str
    weights: dict[str, int] = field(
        metadata={
            "schema": {"minProperties": 2, "maxProperties": 16, "additionalProperties": {"type": "integer", "minimum": 0, "maximum": 100}}
        }
    )

    def __attrs_post_init__(self) -> None:
        """
        Reject fractions, negative weights and incomplete allocations.

        Returns:
            None: No return value.
        """
        if not 2 <= len(self.weights) <= 16 or any(type(value) is not int or not 0 <= value <= 100 for value in self.weights.values()):
            raise ValueError("traffic targets require two through 16 integer percentages")
        if sum(self.weights.values()) != 100:
            raise ValueError("traffic target percentages must sum to 100")
